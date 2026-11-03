"""De punta a punta: la app real con su lifespan, el worker en su hilo, un Postgres real
y el SDK real de Groq con un transporte HTTP simulado (sin red, sin clave real)."""

import functools
import json
import logging
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm
from app import main
from app import repositorio
from app import worker as modulo_worker
from app.config import Settings, get_settings
from app.llm import MOTIVO_CONTENIDO_INVALIDO, MOTIVO_NO_DISPONIBLE
from app.repositorio import LEAD_MAX_TOMAS
from app.worker import MOTIVO_DATOS_INVALIDOS, Worker

CLAVE = "gsk_clave_de_prueba_no_real"
MODELO = "openai/gpt-oss-20b"
AHORA = datetime.now(timezone.utc)
LEAD = {"nombre_empresa": "Acme", "dominio": "acme.com", "cargo_contacto": "CTO"}
SECRETO_EN_EXCEPCION = "SECRETO_XYZ"

CORREO = {
    "asunto": "Una idea para el equipo de Acme",
    "cuerpo": (
        "Hola:\n\nLes escribo porque trabajo con equipos de tecnología que buscan ordenar "
        "sus prioridades sin sumar herramientas complejas.\n\n"
        "¿Les parece si conversamos unos minutos la próxima semana?\n\nSaludos cordiales"
    ),
}
CONTENIDO_OK = json.dumps(CORREO, ensure_ascii=False)
CONTENIDO_CON_ENLACE = json.dumps({**CORREO, "cuerpo": CORREO["cuerpo"] + "\n\nMás en ejemplo.com"})


# --- Dobles de prueba ---------------------------------------------------------

def completion(contenido=CONTENIDO_OK):
    return httpx.Response(200, json={
        "id": "chatcmpl-prueba", "object": "chat.completion", "created": 0, "model": MODELO,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": contenido}}],
        "usage": {"prompt_tokens": 310, "completion_tokens": 95, "total_tokens": 405},
    })


def error_http(status, *, headers=None):
    return httpx.Response(status, headers=headers, json={"error": {"message": "simulado", "type": "x", "code": None}})


class ServidorFalso:
    """Groq simulado. Responde en orden lo que se le da y, cuando se acaba, un correo
    válido. Seguro entre hilos: lo llama el hilo del worker."""

    def __init__(self, *respuestas):
        self._lock = threading.Lock()
        self._pendientes = list(respuestas)
        self.requests: list[httpx.Request] = []
        self.recibido = threading.Event()  # llegó al menos un request
        self.liberar = threading.Event()  # mientras no esté puesto, las respuestas se retienen
        self.liberar.set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            respuesta = self._pendientes.pop(0) if self._pendientes else completion()
        self.recibido.set()
        self.liberar.wait(10)
        return respuesta

    def cantidad(self) -> int:
        with self._lock:
            return len(self.requests)


@pytest.fixture
def levantar(conn, test_db_url, monkeypatch):
    """levantar(servidor) devuelve un TestClient de la app real con el worker activo,
    hablando con el servidor falso. Se usa con "with": entrar arranca el worker y
    salir lo detiene (el lifespan)."""
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("WORKER_ACTIVO", "true")
    monkeypatch.setenv("GROQ_API_KEY", CLAVE)
    monkeypatch.setenv("GROQ_MODEL", MODELO)
    monkeypatch.setenv("GROQ_RPM", "1000")  # sin esperas de ritmo en estos tests
    get_settings.cache_clear()
    # Esperas cortas: aquí se prueba el worker, no los tiempos (eso está en test_llm).
    monkeypatch.setattr(llm, "ESPERAS_BASE_S", (0.01, 0.01))
    monkeypatch.setattr(modulo_worker, "PAUSA_TRAS_ERROR_S", 0.05)

    def _levantar(servidor: ServidorFalso) -> TestClient:
        http = httpx.Client(transport=httpx.MockTransport(servidor))
        monkeypatch.setattr(main, "Worker", functools.partial(Worker, http_client=http))
        return TestClient(main.app)

    yield _levantar
    get_settings.cache_clear()


def fila(conn, lead_id: int) -> dict:
    return conn.execute("SELECT * FROM leads WHERE id = %s", [lead_id]).fetchone()


def esperar(condicion, segundos=5.0, mensaje="la condición no se cumplió"):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return
        time.sleep(0.02)
    raise AssertionError(mensaje)


def esperar_estado(conn, lead_id: int, estado: str) -> dict:
    esperar(lambda: fila(conn, lead_id)["estado"] == estado,
            mensaje=f"el lead {lead_id} no llegó a {estado}: está en {fila(conn, lead_id)['estado']}")
    return fila(conn, lead_id)


def hilos_del_worker() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "worker-leads" and t.is_alive()]


# --- Flujo completo --------------------------------------------------------------

def test_pendiente_a_procesado_y_el_dominio_no_viaja_al_llm(levantar, conn):
    servidor = ServidorFalso()
    with levantar(servidor) as cliente:
        r = cliente.post("/leads", json={
            "nombre_empresa": "Acme", "dominio": "dominio-unico-xyz.com", "cargo_contacto": "Jefa de Ventas",
        })
        assert r.status_code == 202
        lead_id = r.json()["id"]
        # El worker dormía (sin avisos revisa cada 10 min): si llega a "procesado" en
        # segundos es porque el POST lo despertó.
        f = esperar_estado(conn, lead_id, "procesado")
        lead = cliente.get("/leads").json()["leads"][0]

    assert lead["estado"] == "procesado"
    assert lead["correo"] == CORREO
    assert lead["motivo_error"] is None
    assert (f["intentos"], f["tomado_en"]) == (1, None)

    assert servidor.cantidad() == 1
    enviado = servidor.requests[0].content.decode()
    assert "Acme" in enviado and "Jefa de Ventas" in enviado
    assert "dominio-unico-xyz" not in enviado  # el dominio nunca llega al prompt


def test_contenido_invalido_dos_veces_deja_el_lead_en_error(levantar, conn):
    servidor = ServidorFalso(completion(CONTENIDO_CON_ENLACE), completion(CONTENIDO_CON_ENLACE))
    with levantar(servidor) as cliente:
        lead_id = cliente.post("/leads", json=LEAD).json()["id"]
        esperar_estado(conn, lead_id, "error")
        lead = cliente.get("/leads").json()["leads"][0]
    assert lead["motivo_error"] == MOTIVO_CONTENIDO_INVALIDO
    assert lead["correo"] is None
    assert servidor.cantidad() == 2


def test_el_post_no_espera_al_llm(levantar, conn):
    servidor = ServidorFalso()
    servidor.liberar.clear()  # el LLM "tarda" hasta que el test lo libere
    with levantar(servidor) as cliente:
        inicio = time.monotonic()
        r = cliente.post("/leads", json=LEAD)
        assert r.status_code == 202
        assert time.monotonic() - inicio < 1.0
        primero = r.json()["id"]

        # El worker ya está dentro de la llamada al LLM, que sigue sin responder...
        assert servidor.recibido.wait(5)
        assert fila(conn, primero)["estado"] == "procesando"
        # ...y la API sigue atendiendo sin esperar.
        inicio = time.monotonic()
        r = cliente.post("/leads", json={**LEAD, "nombre_empresa": "Globex"})
        listado = cliente.get("/leads")
        assert time.monotonic() - inicio < 1.0
        assert r.status_code == 202 and listado.status_code == 200
        segundo = r.json()["id"]
        assert {l["id"]: l["estado"] for l in listado.json()["leads"]} == {primero: "procesando", segundo: "pendiente"}

        servidor.liberar.set()
        esperar_estado(conn, primero, "procesado")
        esperar_estado(conn, segundo, "procesado")


def test_al_arrancar_procesa_los_pendientes_que_ya_habia(levantar, conn, insertar):
    ids = [insertar(nombre_empresa=f"Empresa {i}")["id"] for i in range(3)]
    servidor = ServidorFalso()
    with levantar(servidor):
        for lead_id in ids:
            esperar_estado(conn, lead_id, "procesado")
    assert servidor.cantidad() == 3


def test_al_arrancar_rescata_un_lead_trabado_y_lo_procesa(levantar, conn, insertar):
    lead_id = insertar(estado="procesando", tomado_en=AHORA, intentos=1)["id"]
    conn.execute("UPDATE leads SET tomado_en = now() - interval '16 minutes' WHERE id = %s", [lead_id])
    with levantar(ServidorFalso()):
        f = esperar_estado(conn, lead_id, "procesado")
    assert f["intentos"] == 2


def test_datos_guardados_invalidos_quedan_en_error_sin_llamar_al_llm(levantar, conn, insertar):
    # La API no deja guardar "<", pero la base sí: puede llegar por un INSERT manual.
    lead_id = insertar(nombre_empresa="Acme <script>")["id"]
    servidor = ServidorFalso()
    with levantar(servidor):
        f = esperar_estado(conn, lead_id, "error")
    assert f["motivo_error"] == MOTIVO_DATOS_INVALIDOS
    assert servidor.cantidad() == 0


# --- Errores del LLM y pausas -------------------------------------------------------

def test_error_de_configuracion_devuelve_el_lead_sin_contar_la_toma_y_pausa(levantar, conn, insertar):
    lead_id = insertar()["id"]
    servidor = ServidorFalso(error_http(401))
    with levantar(servidor) as cliente:
        assert servidor.recibido.wait(5)
        f = esperar_estado(conn, lead_id, "pendiente")
        assert f["intentos"] == 0
        # Pausado: un lead nuevo no se procesa todavía.
        cliente.post("/leads", json=LEAD)
        time.sleep(0.3)
        assert servidor.cantidad() == 1
        inicio = time.monotonic()
    # Apagar corta la pausa de 5 minutos.
    assert time.monotonic() - inicio < 3.0


@pytest.mark.parametrize("tomas_previas, estado, motivo", [
    (0, "pendiente", None),
    (LEAD_MAX_TOMAS - 1, "error", MOTIVO_NO_DISPONIBLE),
])
def test_servicio_no_disponible_cuenta_la_toma(levantar, conn, insertar, tomas_previas, estado, motivo):
    lead_id = insertar(intentos=tomas_previas)["id"]
    servidor = ServidorFalso(error_http(503), error_http(502), error_http(500))
    with levantar(servidor):
        esperar(lambda: servidor.cantidad() == 3)
        f = esperar_estado(conn, lead_id, estado)
    assert f["intentos"] == tomas_previas + 1
    assert f["motivo_error"] == motivo


def test_apagar_durante_un_retry_after_devuelve_el_lead_rapido(levantar, conn, insertar):
    lead_id = insertar()["id"]
    servidor = ServidorFalso(error_http(429, headers={"retry-after": "30"}))
    with levantar(servidor):
        assert servidor.recibido.wait(5)
        inicio = time.monotonic()
    assert time.monotonic() - inicio < 3.0
    f = fila(conn, lead_id)
    assert (f["estado"], f["intentos"], f["tomado_en"]) == ("pendiente", 0, None)
    assert servidor.cantidad() == 1


def test_un_error_inesperado_no_detiene_al_worker(levantar, conn, insertar, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    original = repositorio.tomar_siguiente
    llamadas = []

    def tomar_que_falla_una_vez(c):
        llamadas.append(1)
        if len(llamadas) == 1:
            # El texto va en una constante: el traceback del log muestra esta línea de
            # código, y no debe contener el secreto que se busca.
            raise RuntimeError(f"detalle interno {SECRETO_EN_EXCEPCION}")
        return original(c)

    monkeypatch.setattr(repositorio, "tomar_siguiente", tomar_que_falla_una_vez)
    lead_id = insertar()["id"]
    with levantar(ServidorFalso()):
        esperar_estado(conn, lead_id, "procesado")
    errores = [r.getMessage() for r in caplog.records if r.name == "app.worker" and r.levelno == logging.ERROR]
    assert len(errores) == 1 and errores[0].startswith("worker_error tipo=RuntimeError")
    assert "tomar_que_falla_una_vez" in errores[0]  # el traceback sí está, para depurar
    assert SECRETO_EN_EXCEPCION not in "\n".join(r.getMessage() for r in caplog.records)


# --- Ciclo de vida -------------------------------------------------------------------

def test_el_hilo_arranca_y_se_detiene_con_la_app(levantar):
    assert hilos_del_worker() == []
    with levantar(ServidorFalso()) as cliente:
        assert len(hilos_del_worker()) == 1
        assert cliente.app.state.worker is not None
    esperar(lambda: hilos_del_worker() == [], mensaje="el hilo del worker sigue vivo")


def test_detener_justo_despues_de_iniciar_no_se_cuelga(conn, pool):
    # Si detener() llega mientras el hilo recién empieza, su aviso no debe perderse:
    # el worker se quedaría dormido el intervalo inactivo completo (10 min).
    settings = Settings(database_url="postgresql://u:p@db/x", groq_api_key=CLAVE, groq_model=MODELO)
    for _ in range(30):
        w = Worker(pool, settings, http_client=httpx.Client(transport=httpx.MockTransport(ServidorFalso())))
        inicio = time.monotonic()
        w.iniciar()
        w.detener()
        assert time.monotonic() - inicio < 2.0
        assert hilos_del_worker() == []


def test_con_el_worker_apagado_no_hay_hilo_y_el_post_funciona(cliente):
    # La fixture "cliente" de conftest levanta la app con WORKER_ACTIVO=false.
    assert cliente.app.state.worker is None
    assert hilos_del_worker() == []
    assert cliente.post("/leads", json=LEAD).status_code == 202


# --- Logs --------------------------------------------------------------------------

def test_los_logs_de_punta_a_punta_no_muestran_datos_sensibles(levantar, conn, test_db_url, caplog):
    caplog.set_level(logging.DEBUG)
    asunto_generado = "Asunto generado unico XYZ"
    servidor = ServidorFalso(completion(json.dumps({**CORREO, "asunto": asunto_generado})))
    with levantar(servidor) as cliente:
        lead_id = cliente.post("/leads", json={
            "nombre_empresa": "EmpresaSecretaXYZ", "dominio": "dominio-secreto-xyz.com",
            "cargo_contacto": "CargoSecretoXYZ",
        }).json()["id"]
        esperar_estado(conn, lead_id, "procesado")

    mensajes = [r.getMessage() for r in caplog.records]
    todo = "\n".join(mensajes)
    # Hubo logs del recorrido completo: el test no pasa por no registrar nada.
    for esperado in (f"lead_tomado lead_id={lead_id} toma=1", "llamada_llm", f"lead_cerrado lead_id={lead_id}"):
        assert any(esperado in m for m in mensajes), esperado
    contrasena_bd = test_db_url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    for secreto in (
        CLAVE, "EmpresaSecretaXYZ", "dominio-secreto-xyz", "CargoSecretoXYZ", asunto_generado,
        "Les escribo porque", "postgresql://", contrasena_bd,
    ):
        assert secreto not in todo, secreto
