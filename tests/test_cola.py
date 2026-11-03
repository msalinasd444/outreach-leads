"""La tabla como cola: tomar, cerrar con la toma vigente, devolver y rescatar, contra un Postgres real."""

import dataclasses
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import dict_row

from app import repositorio as repo
from app.config import Settings
from app.llm import peor_caso_por_lead_s
from app.repositorio import LEAD_MAX_TOMAS, LEAD_TRABADO_TRAS, MOTIVO_TRABADO, LeadTomado
from tests.datos_de_prueba import ASUNTO_VALIDO, CUERPO_VALIDO

AHORA = datetime.now(timezone.utc)


def conectar(url: str) -> psycopg.Connection:
    """Conexión propia (sin autocommit) con statement_timeout: si una consulta queda
    bloqueada esperando a otra transacción, el test falla en vez de colgarse."""
    return psycopg.connect(url, row_factory=dict_row, options="-c statement_timeout=5000")


def fila(conn, lead_id: int) -> dict:
    return conn.execute("SELECT * FROM leads WHERE id = %s", [lead_id]).fetchone()


def envejecer(conn, lead_id: int, minutos: float) -> None:
    """Simula que el lead se tomó hace `minutos` (con el reloj de la base)."""
    conn.execute(
        "UPDATE leads SET tomado_en = now() - make_interval(secs => %s) WHERE id = %s",
        [minutos * 60, lead_id],
    )


def toma_vigente(conn, lead_id: int) -> LeadTomado:
    f = fila(conn, lead_id)
    return LeadTomado(f["id"], f["nombre_empresa"], f["cargo_contacto"], f["intentos"], f["tomado_en"])


def procesando(insertar, *, minutos: float, tomas: int, conn) -> int:
    lead_id = insertar(estado="procesando", tomado_en=AHORA, intentos=tomas)["id"]
    envejecer(conn, lead_id, minutos)
    return lead_id


# --- Tomar ------------------------------------------------------------------------

def test_sin_pendientes_no_toma_nada(conn, insertar):
    insertar(estado="procesando", tomado_en=AHORA)
    insertar(estado="procesado", asunto=ASUNTO_VALIDO, cuerpo=CUERPO_VALIDO)
    insertar(estado="error", motivo_error="motivo")
    assert repo.tomar_siguiente(conn) is None


def test_toma_los_pendientes_del_mas_antiguo_al_mas_nuevo(conn, insertar):
    nuevo = insertar(creado_en=AHORA - timedelta(minutes=1))["id"]
    viejo = insertar(creado_en=AHORA - timedelta(minutes=3))["id"]
    # Mismo instante: desempata el id.
    medio_a = insertar(creado_en=AHORA - timedelta(minutes=2))["id"]
    medio_b = insertar(creado_en=AHORA - timedelta(minutes=2))["id"]
    tomados = [repo.tomar_siguiente(conn).id for _ in range(4)]
    assert tomados == [viejo, medio_a, medio_b, nuevo]
    assert repo.tomar_siguiente(conn) is None


def test_tomar_pasa_a_procesando_con_hora_y_cuenta_la_toma(conn, insertar):
    lead_id = insertar(nombre_empresa="Acme Corp", cargo_contacto="CTO")["id"]
    lead = repo.tomar_siguiente(conn)
    assert (lead.id, lead.nombre_empresa, lead.cargo_contacto, lead.toma) == (lead_id, "Acme Corp", "CTO", 1)
    f = fila(conn, lead_id)
    assert f["estado"] == "procesando"
    assert f["intentos"] == 1
    assert f["tomado_en"] == lead.tomado_en


def test_el_lead_tomado_no_trae_el_dominio():
    assert "dominio" not in {campo.name for campo in dataclasses.fields(LeadTomado)}


def test_una_toma_sin_commit_no_bloquea_ni_se_repite(test_db_url, conn, insertar):
    primero = insertar(creado_en=AHORA - timedelta(minutes=2))["id"]
    segundo = insertar(creado_en=AHORA - timedelta(minutes=1))["id"]
    with conectar(test_db_url) as c1, conectar(test_db_url) as c2, conectar(test_db_url) as c3:
        # c1 y c2 toman y aún no hacen COMMIT: sus filas siguen bloqueadas.
        assert repo.tomar_siguiente(c1).id == primero
        assert repo.tomar_siguiente(c2).id == segundo
        # c3 salta las dos filas bloqueadas sin esperar (si esperara, el statement_timeout
        # de conectar() haría fallar el test).
        assert repo.tomar_siguiente(c3) is None
        c1.commit()
        c2.commit()
    assert {fila(conn, i)["estado"] for i in (primero, segundo)} == {"procesando"}


def test_muchos_hilos_nunca_toman_el_mismo_lead(test_db_url, conn, insertar):
    ids = [insertar()["id"] for _ in range(60)]
    hilos, tomados, errores = 8, [], []
    salida = threading.Barrier(hilos, timeout=10)  # si un hilo no llega, los demás no se cuelgan

    def trabajar():
        try:
            with conectar(test_db_url) as c:
                salida.wait()  # todos empiezan a la vez
                while True:
                    lead = repo.tomar_siguiente(c)
                    c.commit()
                    if lead is None:
                        return
                    tomados.append(lead.id)
        except Exception as exc:  # que un error en un hilo haga fallar el test
            errores.append(exc)

    trabajadores = [threading.Thread(target=trabajar) for _ in range(hilos)]
    for t in trabajadores:
        t.start()
    for t in trabajadores:
        t.join(timeout=30)
    assert errores == []
    assert len(tomados) == len(set(tomados)) == len(ids)
    assert sorted(tomados) == sorted(ids)


# --- Cerrar y devolver ---------------------------------------------------------------

def test_marcar_procesado(conn, insertar):
    insertar()
    lead = repo.tomar_siguiente(conn)
    assert repo.marcar_procesado(conn, lead, asunto=ASUNTO_VALIDO, cuerpo=CUERPO_VALIDO) is True
    f = fila(conn, lead.id)
    assert (f["estado"], f["asunto"], f["cuerpo"]) == ("procesado", ASUNTO_VALIDO, CUERPO_VALIDO)
    assert f["tomado_en"] is None and f["motivo_error"] is None
    assert f["intentos"] == 1


def test_marcar_error(conn, insertar):
    insertar()
    lead = repo.tomar_siguiente(conn)
    assert repo.marcar_error(conn, lead, motivo="El modelo no generó un correo válido.") is True
    f = fila(conn, lead.id)
    assert (f["estado"], f["motivo_error"]) == ("error", "El modelo no generó un correo válido.")
    assert f["asunto"] is None and f["cuerpo"] is None and f["tomado_en"] is None


@pytest.mark.parametrize("descontar_toma, tomas", [(True, 0), (False, 1)])
def test_devolver_a_pendiente(conn, insertar, descontar_toma, tomas):
    insertar()
    lead = repo.tomar_siguiente(conn)
    assert repo.devolver_a_pendiente(conn, lead, descontar_toma=descontar_toma) is True
    f = fila(conn, lead.id)
    assert (f["estado"], f["intentos"], f["tomado_en"]) == ("pendiente", tomas, None)


def test_la_base_no_guarda_un_correo_fuera_de_rango(conn, insertar):
    insertar()
    lead = repo.tomar_siguiente(conn)
    with pytest.raises(errors.CheckViolation):
        repo.marcar_procesado(conn, lead, asunto="Hola", cuerpo=CUERPO_VALIDO)


# --- La toma vigente: un resultado tardío se descarta ----------------------------------

CIERRES = {
    "procesado": lambda c, lead: repo.marcar_procesado(c, lead, asunto=ASUNTO_VALIDO, cuerpo=CUERPO_VALIDO),
    "error": lambda c, lead: repo.marcar_error(c, lead, motivo="motivo"),
    "pendiente": lambda c, lead: repo.devolver_a_pendiente(c, lead, descontar_toma=True),
}


@pytest.mark.parametrize("cierre", CIERRES)
def test_resultado_tardio_tras_rescate_y_nueva_toma_se_descarta(conn, insertar, cierre):
    insertar()
    viejo = repo.tomar_siguiente(conn)
    envejecer(conn, viejo.id, 16)
    assert repo.rescatar_trabados(conn) == (0, 1)
    nuevo = repo.tomar_siguiente(conn)
    assert (nuevo.id, nuevo.toma) == (viejo.id, 2)

    assert CIERRES[cierre](conn, viejo) is False
    f = fila(conn, viejo.id)
    assert (f["estado"], f["intentos"], f["tomado_en"]) == ("procesando", 2, nuevo.tomado_en)
    # El worker que lo tomó después sí puede cerrarlo.
    assert CIERRES[cierre](conn, nuevo) is True


def test_misma_cuenta_de_tomas_pero_otra_toma_tambien_se_descarta(conn, insertar):
    # Devolver descontando la toma deja el contador igual que antes: la siguiente
    # toma repite el número. Lo que la distingue es tomado_en.
    insertar()
    viejo = repo.tomar_siguiente(conn)
    repo.devolver_a_pendiente(conn, viejo, descontar_toma=True)
    nuevo = repo.tomar_siguiente(conn)
    assert nuevo.toma == viejo.toma and nuevo.tomado_en != viejo.tomado_en
    assert repo.marcar_procesado(conn, viejo, asunto=ASUNTO_VALIDO, cuerpo=CUERPO_VALIDO) is False
    assert fila(conn, nuevo.id)["estado"] == "procesando"


@pytest.mark.parametrize("cierre", CIERRES)
def test_no_se_cierra_un_lead_que_ya_no_esta_procesando(conn, insertar, cierre):
    insertar()
    lead = repo.tomar_siguiente(conn)
    assert repo.marcar_error(conn, lead, motivo="motivo") is True
    assert CIERRES[cierre](conn, lead) is False
    assert fila(conn, lead.id)["estado"] == "error"


@pytest.mark.parametrize("cierre", CIERRES)
def test_no_se_cierra_un_lead_cambiado_a_mano_fuera_de_procesando(conn, insertar, cierre):
    # El código siempre limpia tomado_en al salir de "procesando", pero un UPDATE
    # manual podría no hacerlo: la condición de estado cubre ese caso.
    insertar()
    lead = repo.tomar_siguiente(conn)
    conn.execute("UPDATE leads SET estado = 'pendiente' WHERE id = %s", [lead.id])
    assert CIERRES[cierre](conn, lead) is False
    assert fila(conn, lead.id)["estado"] == "pendiente"


# --- Rescate de trabados ----------------------------------------------------------------

def test_rescate_de_trabados(conn, insertar):
    trabado = procesando(insertar, minutos=16, tomas=1, conn=conn)
    sin_tomas = procesando(insertar, minutos=16, tomas=LEAD_MAX_TOMAS, conn=conn)
    reciente = procesando(insertar, minutos=14, tomas=1, conn=conn)
    pendiente = insertar()["id"]
    procesado = insertar(estado="procesado", asunto=ASUNTO_VALIDO, cuerpo=CUERPO_VALIDO)["id"]

    assert repo.rescatar_trabados(conn) == (1, 1)

    f = fila(conn, trabado)
    assert (f["estado"], f["tomado_en"], f["intentos"]) == ("pendiente", None, 1)
    f = fila(conn, sin_tomas)
    assert (f["estado"], f["motivo_error"], f["tomado_en"]) == ("error", MOTIVO_TRABADO, None)
    assert fila(conn, reciente)["estado"] == "procesando"
    assert fila(conn, pendiente)["estado"] == "pendiente"
    assert fila(conn, procesado)["estado"] == "procesado"


def test_rescate_sin_trabados_no_cambia_nada(conn, insertar):
    procesando(insertar, minutos=1, tomas=1, conn=conn)
    assert repo.rescatar_trabados(conn) == (0, 0)


def test_el_rescate_espera_al_worker_que_esta_cerrando_y_no_pisa_su_resultado(test_db_url, conn, insertar):
    lead_id = procesando(insertar, minutos=16, tomas=1, conn=conn)
    resultado = {}
    with conectar(test_db_url) as worker, conectar(test_db_url) as rescate:
        # El worker cierra el lead pero todavía no hace COMMIT: la fila queda bloqueada.
        assert repo.marcar_procesado(worker, toma_vigente(conn, lead_id), asunto=ASUNTO_VALIDO, cuerpo=CUERPO_VALIDO)

        def rescatar():
            resultado["rescatados"] = repo.rescatar_trabados(rescate)
            rescate.commit()

        hilo = threading.Thread(target=rescatar)
        hilo.start()
        hilo.join(timeout=0.5)
        assert hilo.is_alive()  # el rescate está esperando el bloqueo del worker
        worker.commit()
        hilo.join(timeout=10)
    # Tras el COMMIT, el rescate volvió a evaluar la fila: ya no estaba en "procesando".
    assert resultado["rescatados"] == (0, 0)
    assert fila(conn, lead_id)["estado"] == "procesado"


def test_el_umbral_de_trabado_supera_el_peor_caso_del_llm():
    base = {"database_url": "postgresql://u:p@db/x", "worker_activo": False}
    assert LEAD_TRABADO_TRAS == timedelta(minutes=15)
    for config in ({}, {"groq_timeout": 120, "groq_rpm": 1}):
        assert LEAD_TRABADO_TRAS.total_seconds() > peor_caso_por_lead_s(Settings(**base, **config))
