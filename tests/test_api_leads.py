"""POST y GET /leads contra la app real y un Postgres real."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.datos_de_prueba import ASUNTO_VALIDO, CUERPO_VALIDO

LEAD = {"nombre_empresa": "Acme", "dominio": "acme.com", "cargo_contacto": "CTO"}


def contar(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM leads").fetchone()["n"]


# --- POST /leads -------------------------------------------------------------

def test_post_guarda_normalizado_en_pendiente_y_responde_202(cliente, conn):
    r = cliente.post("/leads", json={
        "nombre_empresa": "  Acme   Corp ",
        "dominio": "https://www.ACME.com/",
        "cargo_contacto": "Jefe de Ventas",
    })
    assert r.status_code == 202
    cuerpo = r.json()
    assert set(cuerpo) == {"id", "estado", "creado_en"}
    assert cuerpo["estado"] == "pendiente"

    # `conn` es otra conexión: si ve la fila, el COMMIT ocurrió antes de responder.
    fila = conn.execute("SELECT * FROM leads WHERE id = %s", [cuerpo["id"]]).fetchone()
    assert fila["nombre_empresa"] == "Acme Corp"
    assert fila["dominio"] == "acme.com"
    assert fila["estado"] == "pendiente"
    assert fila["intentos"] == 0
    assert fila["asunto"] is None and fila["cuerpo"] is None and fila["tomado_en"] is None


def test_post_invalido_responde_422_y_no_guarda_nada(cliente, conn):
    r = cliente.post("/leads", json={**LEAD, "dominio": "acme.com/precios"})
    assert r.status_code == 422
    assert contar(conn) == 0


def test_post_duplicado_se_permite(cliente, conn):
    ids = {cliente.post("/leads", json=LEAD).json()["id"] for _ in range(2)}
    assert len(ids) == 2
    assert contar(conn) == 2


def test_post_texto_con_sintaxis_sql_se_guarda_tal_cual(cliente, conn):
    nombre = "Acme'); DROP TABLE leads; --"
    r = cliente.post("/leads", json={**LEAD, "nombre_empresa": nombre})
    assert r.status_code == 202
    assert conn.execute("SELECT nombre_empresa FROM leads").fetchone()["nombre_empresa"] == nombre


# --- GET /leads --------------------------------------------------------------

def test_get_sin_leads(cliente):
    r = cliente.get("/leads")
    assert r.status_code == 200
    assert r.json() == {"leads": [], "limit": 50, "offset": 0}


def test_get_muestra_cada_lead_con_su_estado_correo_y_fecha(cliente, insertar):
    ahora = datetime.now(timezone.utc)
    insertar(estado="procesado", asunto=ASUNTO_VALIDO, cuerpo=CUERPO_VALIDO, creado_en=ahora - timedelta(minutes=3))
    insertar(estado="error", motivo_error="No se pudo generar el correo", creado_en=ahora - timedelta(minutes=2))
    insertar(estado="procesando", tomado_en=ahora, creado_en=ahora - timedelta(minutes=1))
    pendiente = insertar(creado_en=ahora)

    leads = cliente.get("/leads").json()["leads"]
    assert [l["estado"] for l in leads] == ["pendiente", "procesando", "error", "procesado"]

    # Forma exacta de un lead en la respuesta.
    assert leads[0] == {
        "id": pendiente["id"],
        "nombre_empresa": "Acme",
        "dominio": "acme.com",
        "cargo_contacto": "CTO",
        "estado": "pendiente",
        "correo": None,
        "motivo_error": None,
        "creado_en": leads[0]["creado_en"],
    }
    assert datetime.fromisoformat(leads[0]["creado_en"]) == pendiente["creado_en"]
    assert leads[1]["correo"] is None and leads[1]["motivo_error"] is None
    assert leads[2]["correo"] is None and leads[2]["motivo_error"] == "No se pudo generar el correo"
    assert leads[3]["correo"] == {"asunto": ASUNTO_VALIDO, "cuerpo": CUERPO_VALIDO} and leads[3]["motivo_error"] is None


def test_get_ordena_por_fecha_de_creacion_y_desempata_por_id(cliente, insertar):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Ids en orden distinto a las fechas, para comprobar que manda creado_en.
    a = insertar(creado_en=base + timedelta(days=2))
    b = insertar(creado_en=base)
    c = insertar(creado_en=base + timedelta(days=2))  # misma fecha que `a`
    ids = [l["id"] for l in cliente.get("/leads").json()["leads"]]
    assert ids == [c["id"], a["id"], b["id"]]


def test_get_pagina_con_limit_y_offset(cliente, insertar):
    ids = [insertar()["id"] for _ in range(5)]
    recientes = list(reversed(ids))

    def pagina(limit, offset):
        r = cliente.get("/leads", params={"limit": limit, "offset": offset})
        assert r.status_code == 200
        assert (r.json()["limit"], r.json()["offset"]) == (limit, offset)
        return [l["id"] for l in r.json()["leads"]]

    assert pagina(2, 0) == recientes[0:2]
    assert pagina(2, 2) == recientes[2:4]
    assert pagina(2, 4) == recientes[4:5]
    assert pagina(2, 10) == []


def test_get_limit_por_defecto_es_50(cliente, conn):
    conn.execute(
        "INSERT INTO leads (nombre_empresa, dominio, cargo_contacto) "
        "SELECT 'Acme', 'acme.com', 'CTO' FROM generate_series(1, 51)"
    )
    assert len(cliente.get("/leads").json()["leads"]) == 50


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"offset": 1_000_001}],
    ids=["limit-0", "limit-201", "offset-negativo", "offset-enorme"],
)
def test_get_paginacion_fuera_de_rango_responde_422(cliente, params):
    r = cliente.get("/leads", params=params)
    assert r.status_code == 422
    assert r.json()["errores"][0]["campo"] == next(iter(params))


# --- Base de datos caída (E) -------------------------------------------------

@pytest.mark.parametrize(
    ("metodo", "kwargs"),
    [("post", {"json": LEAD}), ("get", {})],
    ids=["post", "get"],
)
def test_base_no_disponible_responde_503_sin_detalles(cliente, metodo, kwargs):
    # Pool cerrado: el siguiente pool.connection() lanza PoolClosed (un OperationalError).
    cliente.app.state.pool.close()
    r = getattr(cliente, metodo)("/leads", **kwargs)
    assert r.status_code == 503
    assert r.json() == {"detail": "Servicio no disponible temporalmente"}


# --- /health -----------------------------------------------------------------

def test_health_ok(cliente):
    r = cliente.get("/health")
    assert r.status_code == 200
    assert r.json() == {"estado": "ok"}


def test_health_sin_base_de_datos_responde_503(cliente):
    cliente.app.state.pool.close()  # simula la base caída; el lifespan lo cierra de nuevo sin problema
    r = cliente.get("/health")
    assert r.status_code == 503
    assert r.json() == {"estado": "sin base de datos"}


# --- / -----------------------------------------------------------------------

def test_raiz_redirige_a_docs_y_no_aparece_en_openapi(cliente):
    r = cliente.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/docs"
    # El destino existe: si alguien desactiva /docs, la redirección llevaría a un 404.
    assert cliente.get("/").status_code == 200
    assert "/" not in cliente.app.openapi()["paths"]
