"""Llamadas al LLM con el SDK real de Groq y un transporte HTTP simulado (sin red, sin clave real)."""

import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.config import Settings
from app.correo import REGLAS, CorreoGenerado
from app.llm import (
    MAX_TOKENS_RESPUESTA,
    MOTIVO_CONTENIDO_INVALIDO,
    MOTIVO_NO_DISPONIBLE,
    MOTIVO_RECHAZADO,
    PAUSA_CONFIGURACION_S,
    PAUSA_TRAS_TRANSITORIOS_S,
    Devuelto,
    Fallido,
    GeneradorDeCorreos,
    Generado,
    NoDisponible,
    crear_cliente,
    leer_retry_after,
    peor_caso_por_lead_s,
)
from app.prompt import NOTA_REINTENTO, SISTEMA, DatosDelLead, armar_mensajes

CLAVE = "gsk_clave_de_prueba_no_real"
MODELO = "openai/gpt-oss-20b"
AHORA = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
DATOS = DatosDelLead(nombre_empresa="Acme", cargo_contacto="CTO")

CORREO = {
    "asunto": "Una idea para el equipo de Acme",
    "cuerpo": (
        "Hola:\n\nLes escribo porque trabajo con equipos de tecnología que buscan ordenar "
        "sus prioridades sin sumar herramientas complejas.\n\n"
        "¿Les parece si conversamos unos minutos la próxima semana?\n\nSaludos cordiales"
    ),
}
CONTENIDO_OK = json.dumps(CORREO, ensure_ascii=False)
CONTENIDO_CON_ENLACE = json.dumps({**CORREO, "cuerpo": CORREO["cuerpo"] + "\n\nMás en acme.com"}, ensure_ascii=False)


# --- Dobles de prueba ---------------------------------------------------------

class Reloj:
    """Reloj monotónico falso. esperar() avanza el tiempo sin dormir y registra cada espera."""

    def __init__(self):
        self.ahora = 1000.0
        self.esperas: list[float] = []
        self.apagar = False  # True: la próxima espera se corta como si el worker se apagara

    def __call__(self) -> float:
        return self.ahora

    def esperar(self, segundos: float) -> bool:
        self.esperas.append(segundos)
        if self.apagar:
            return True
        self.ahora += segundos
        return False


class ServidorFalso:
    """Responde en orden lo que se le da. Cada elemento es una httpx.Response o una
    función que recibe el request (para lanzar errores de red)."""

    def __init__(self, reloj: Reloj, *respuestas, latencia_s: float = 0.0):
        self.reloj = reloj
        self.pendientes = list(respuestas)
        self.latencia_s = latencia_s
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.reloj.ahora += self.latencia_s
        respuesta = self.pendientes.pop(0)
        return respuesta(request) if callable(respuesta) else respuesta

    @property
    def cuerpos(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def completion(contenido=CONTENIDO_OK, *, finish_reason="stop", prompt_tokens=310, completion_tokens=95, choices=None):
    if choices is None:
        choices = [{"index": 0, "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": contenido}}]
    return httpx.Response(200, json={
        "id": "chatcmpl-prueba", "object": "chat.completion", "created": 0, "model": MODELO,
        "choices": choices,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    })


def error_http(status, *, code=None, headers=None, mensaje="error simulado", **extra):
    return httpx.Response(status, headers=headers, json={
        "error": {"message": mensaje, "type": "invalid_request_error", "code": code, **extra},
    })


def lanzar(tipo):
    def _lanzar(request):
        raise tipo("simulado", request=request)
    return _lanzar


def armar(*respuestas, latencia_s=0.0, aleatorio=0.5, **config):
    """Generador con el SDK real y el servidor falso. aleatorio=0.5 deja el backoff sin jitter.

    Por defecto, ritmo de 1000 llamadas por minuto (0,06 s entre llamadas): así no se
    mezcla con las esperas que se prueban. Los tests de ritmo lo fijan explícitamente.
    """
    config.setdefault("groq_rpm", 1000)
    settings = Settings(
        database_url="postgresql://u:p@db/x", groq_api_key=CLAVE, groq_model=MODELO, **config,
    )
    reloj = Reloj()
    servidor = ServidorFalso(reloj, *respuestas, latencia_s=latencia_s)
    cliente = crear_cliente(settings, http_client=httpx.Client(transport=httpx.MockTransport(servidor)))
    generador = GeneradorDeCorreos(
        cliente, settings, esperar=reloj.esperar, reloj=reloj, ahora=lambda: AHORA, aleatorio=lambda: aleatorio,
    )
    return generador, servidor, reloj


def generar(generador, datos=DATOS):
    return generador.generar(datos, lead_id=7, toma=1)


# --- Llamada exitosa y lo que se envía ----------------------------------------

def test_genera_el_correo_en_una_llamada():
    generador, servidor, reloj = armar(completion())
    assert generar(generador) == Generado(CorreoGenerado(**CORREO))
    assert len(servidor.requests) == 1
    assert reloj.esperas == []


def test_el_request_lleva_lo_esperado():
    generador, servidor, _ = armar(completion())
    generar(generador)
    request = servidor.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/openai/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {CLAVE}"
    cuerpo = servidor.cuerpos[0]
    assert cuerpo == {
        "model": MODELO,
        "messages": armar_mensajes(DATOS),
        "response_format": {"type": "json_object"},
        "max_completion_tokens": MAX_TOKENS_RESPUESTA,
        "temperature": 0.5,
    }  # sin reasoning_effort: no está configurado


def test_reasoning_effort_se_envia_si_esta_configurado():
    generador, servidor, _ = armar(completion(), groq_reasoning_effort="low")
    generar(generador)
    assert servidor.cuerpos[0]["reasoning_effort"] == "low"


@pytest.mark.parametrize("timeout, esperado", [
    (20, {"connect": 5.0, "read": 20.0, "write": 20.0, "pool": 20.0}),
    (3, {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}),
])
def test_cada_request_lleva_el_timeout(timeout, esperado):
    generador, servidor, _ = armar(completion(), groq_timeout=timeout)
    generar(generador)
    assert servidor.requests[0].extensions["timeout"] == esperado


# --- Contenido inválido: un único reintento con la nota fija ------------------

def test_contenido_invalido_y_luego_valido():
    generador, servidor, _ = armar(completion(CONTENIDO_CON_ENLACE), completion())
    assert generar(generador) == Generado(CorreoGenerado(**CORREO))
    primero, segundo = servidor.cuerpos
    assert primero["messages"][0]["content"] == SISTEMA
    assert segundo["messages"][0]["content"] == SISTEMA + NOTA_REINTENTO.format(regla=REGLAS["enlace"])
    assert segundo["messages"][1] == primero["messages"][1]
    # Lo que generó el modelo no vuelve a enviarse.
    assert "acme.com" not in servidor.requests[1].content.decode()


def test_contenido_invalido_dos_veces_es_error_del_lead():
    generador, servidor, _ = armar(completion(CONTENIDO_CON_ENLACE), completion(CONTENIDO_CON_ENLACE))
    assert generar(generador) == Fallido(MOTIVO_CONTENIDO_INVALIDO)
    assert len(servidor.requests) == 2


def test_json_validate_failed_cuenta_como_contenido_invalido():
    generador, servidor, _ = armar(
        error_http(400, code="json_validate_failed", failed_generation='{"asunto": "TEXTO GENERADO"'),
        completion(),
    )
    assert generar(generador) == Generado(CorreoGenerado(**CORREO))
    assert servidor.cuerpos[1]["messages"][0]["content"].endswith(NOTA_REINTENTO.format(regla=REGLAS["json"]))
    assert "TEXTO GENERADO" not in servidor.requests[1].content.decode()


def test_json_validate_failed_dos_veces():
    generador, servidor, _ = armar(
        error_http(400, code="json_validate_failed"), error_http(400, code="json_validate_failed"),
    )
    assert generar(generador) == Fallido(MOTIVO_CONTENIDO_INVALIDO)
    assert len(servidor.requests) == 2


@pytest.mark.parametrize("respuesta, regla", [
    (completion(finish_reason="length"), "respuesta_larga"),
    (completion(None), "json"),
    (completion(choices=[]), "json"),
    (completion("no es json"), "json"),
])
def test_respuestas_incompletas_se_reintentan_con_su_regla(respuesta, regla):
    generador, servidor, _ = armar(respuesta, completion())
    assert generar(generador) == Generado(CorreoGenerado(**CORREO))
    assert servidor.cuerpos[1]["messages"][0]["content"].endswith(NOTA_REINTENTO.format(regla=REGLAS[regla]))


# --- Errores transitorios ------------------------------------------------------

def test_errores_5xx_se_reintentan_con_backoff():
    generador, servidor, reloj = armar(error_http(500), error_http(503), completion())
    assert generar(generador) == Generado(CorreoGenerado(**CORREO))
    assert len(servidor.requests) == 3
    assert reloj.esperas == [1.0, 2.0]


def test_el_backoff_tiene_jitter_acotado():
    generador, _, reloj = armar(error_http(500), completion(), aleatorio=0.0)
    generar(generador)
    assert reloj.esperas == [0.75]
    generador, _, reloj = armar(error_http(500), completion(), aleatorio=0.999)
    generar(generador)
    assert 1.24 < reloj.esperas[0] < 1.25


@pytest.mark.parametrize("error", [lanzar(httpx.ReadTimeout), lanzar(httpx.ConnectTimeout), lanzar(httpx.ConnectError)])
def test_timeout_y_error_de_conexion_se_reintentan(error):
    generador, servidor, _ = armar(error, completion())
    assert generar(generador) == Generado(CorreoGenerado(**CORREO))
    assert len(servidor.requests) == 2


def test_408_se_reintenta():
    generador, servidor, _ = armar(error_http(408), completion())
    assert isinstance(generar(generador), Generado)
    assert len(servidor.requests) == 2


def test_transitorios_agotados_devuelven_no_disponible():
    generador, servidor, reloj = armar(error_http(502), error_http(503), error_http(500))
    assert generar(generador) == NoDisponible(MOTIVO_NO_DISPONIBLE, PAUSA_TRAS_TRANSITORIOS_S)
    assert len(servidor.requests) == 3  # el SDK no reintenta por su cuenta: max_retries=0
    assert reloj.esperas == [1.0, 2.0]


def test_los_reintentos_transitorios_y_de_contenido_se_comparten():
    # 1 transitorio + 1 contenido inválido + 1 transitorio + éxito = 4 llamadas, el máximo.
    generador, servidor, _ = armar(
        error_http(503), completion(CONTENIDO_CON_ENLACE), error_http(503), completion(),
    )
    assert isinstance(generar(generador), Generado)
    assert len(servidor.requests) == 4


# --- Retry-After ------------------------------------------------------------------

@pytest.mark.parametrize("headers, espera", [
    ({"retry-after": "7"}, 7.0),
    ({"retry-after": "2.5"}, 2.5),
    ({"retry-after-ms": "1500"}, 1.5),
    ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),  # el de milisegundos es más preciso
    ({"retry-after": format_datetime(AHORA + timedelta(seconds=12), usegmt=True)}, 12.0),
])
def test_429_espera_exactamente_lo_que_pide_retry_after(headers, espera):
    generador, servidor, reloj = armar(error_http(429, headers=headers), completion())
    assert isinstance(generar(generador), Generado)
    assert reloj.esperas == [espera]
    assert len(servidor.requests) == 2


@pytest.mark.parametrize("retry_after, espera", [("2.5", 4.0), ("7", 7.0)])
def test_retry_after_y_ritmo_se_combinan_en_una_sola_espera(retry_after, espera):
    # Con 15 llamadas por minuto hay 4 s entre llamadas: se espera lo mayor, una sola vez.
    generador, _, reloj = armar(error_http(429, headers={"retry-after": retry_after}), completion(), groq_rpm=15)
    generar(generador)
    assert reloj.esperas == [espera]


def test_retry_after_tambien_se_respeta_en_un_503():
    generador, _, reloj = armar(error_http(503, headers={"retry-after": "5"}), completion())
    generar(generador)
    assert reloj.esperas == [5.0]


def test_retry_after_largo_devuelve_el_lead_y_pausa_sin_otra_llamada():
    generador, servidor, reloj = armar(error_http(429, headers={"retry-after": "600"}))
    assert generar(generador) == Devuelto("limite_de_uso", 600.0)
    assert len(servidor.requests) == 1
    assert reloj.esperas == []


def test_retry_after_largo_en_un_5xx_cuenta_como_no_disponible():
    generador, servidor, _ = armar(error_http(503, headers={"retry-after": "120"}))
    assert generar(generador) == NoDisponible(MOTIVO_NO_DISPONIBLE, 120.0)
    assert len(servidor.requests) == 1


def test_429_repetidos_devuelven_el_lead_sin_culparlo():
    generador, servidor, _ = armar(error_http(429), error_http(429), error_http(429))
    assert generar(generador) == Devuelto("limite_de_uso", PAUSA_TRAS_TRANSITORIOS_S)
    assert len(servidor.requests) == 3


@pytest.mark.parametrize("headers, esperado", [
    ({}, None),
    ({"retry-after": "7"}, 7.0),
    ({"retry-after": "-3"}, 0.0),
    ({"retry-after": "nan"}, None),
    ({"retry-after": "inf"}, None),
    ({"retry-after": "pronto"}, None),
    ({"retry-after-ms": "abc", "retry-after": "4"}, 4.0),
    ({"retry-after-ms": "250"}, 0.25),
    ({"retry-after": format_datetime(AHORA - timedelta(seconds=30), usegmt=True)}, 0.0),
    ({"retry-after": format_datetime(AHORA + timedelta(hours=2), usegmt=True)}, 7200.0),
])
def test_leer_retry_after(headers, esperado):
    assert leer_retry_after(httpx.Headers(headers), ahora=AHORA) == esperado


# --- Errores que no se reintentan ------------------------------------------------

@pytest.mark.parametrize("respuesta", [
    error_http(401),
    error_http(403),
    error_http(404, code="model_not_found"),  # lo que Groq respondió en una prueba real
    error_http(404),
    # Códigos de configuración aunque lleguen con otro status.
    error_http(400, code="model_decommissioned"),
    error_http(400, code="model_not_found"),
    error_http(400, code="invalid_api_key"),
])
def test_error_de_configuracion_devuelve_el_lead_y_pausa(respuesta):
    generador, servidor, _ = armar(respuesta)
    assert generar(generador) == Devuelto("configuracion", PAUSA_CONFIGURACION_S)
    assert len(servidor.requests) == 1


@pytest.mark.parametrize("respuesta", [
    error_http(400, code="context_length_exceeded"),
    error_http(400),
    error_http(413),
    error_http(422),
])
def test_otros_4xx_son_error_del_lead_sin_reintento(respuesta):
    generador, servidor, _ = armar(respuesta)
    assert generar(generador) == Fallido(MOTIVO_RECHAZADO)
    assert len(servidor.requests) == 1


# --- Ritmo de llamadas y apagado ----------------------------------------------------

def test_respeta_el_ritmo_maximo_entre_llamadas():
    generador, servidor, reloj = armar(completion(), completion(), completion(), groq_rpm=15)
    generar(generador)
    generar(generador)
    assert reloj.esperas == [4.0]  # 60 s / 15 llamadas
    reloj.ahora += 10  # pasó más que el intervalo: no hay que esperar
    generar(generador)
    assert reloj.esperas == [4.0]
    assert len(servidor.requests) == 3


def test_el_ritmo_descuenta_lo_que_ya_tardo_la_llamada():
    generador, _, reloj = armar(completion(), completion(), groq_rpm=15, latencia_s=1.5)
    generar(generador)
    generar(generador)
    assert reloj.esperas == [2.5]


def test_apagado_durante_una_espera_de_retry_after():
    generador, servidor, reloj = armar(error_http(429, headers={"retry-after": "30"}), completion())
    reloj.apagar = True
    assert generar(generador) == Devuelto("apagado", 0.0)
    assert len(servidor.requests) == 1


def test_apagado_durante_la_espera_de_ritmo():
    generador, servidor, reloj = armar(completion(), completion())
    generar(generador)
    reloj.apagar = True
    assert generar(generador) == Devuelto("apagado", 0.0)
    assert len(servidor.requests) == 1


def test_peor_caso_por_lead():
    base = {"database_url": "postgresql://u:p@db/x", "groq_api_key": CLAVE, "groq_model": MODELO}
    # 4 llamadas × (20 s + 4 s de ritmo) + 2 esperas de hasta 60 s
    assert peor_caso_por_lead_s(Settings(**base)) == 216.0
    # Los máximos que permite la config: 4 × (120 + 60) + 120
    assert peor_caso_por_lead_s(Settings(**base, groq_timeout=120, groq_rpm=1)) == 840.0


# --- Logs ------------------------------------------------------------------------

def registros_llm(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "app.llm" and r.getMessage().startswith("llamada_llm")]


def test_una_linea_de_log_por_llamada(caplog):
    caplog.set_level(logging.INFO)
    generador, _, _ = armar(error_http(503), completion(CONTENIDO_CON_ENLACE), completion(), latencia_s=0.8)
    generador.generar(DATOS, lead_id=42, toma=2)
    comun = "llamada_llm lead_id=42 toma=2 intento={} modelo=openai/gpt-oss-20b latencia_ms=800 "
    assert registros_llm(caplog) == [
        comun.format(1) + "tokens_prompt=- tokens_completion=- resultado=http_503",
        comun.format(2) + "tokens_prompt=310 tokens_completion=95 resultado=contenido_invalido:enlace",
        comun.format(3) + "tokens_prompt=310 tokens_completion=95 resultado=ok",
    ]


@pytest.mark.parametrize("respuesta, resultado", [
    (lanzar(httpx.ReadTimeout), "timeout"),
    (lanzar(httpx.ConnectError), "conexion"),
    (error_http(429), "http_429"),
    (error_http(400, code="json_validate_failed"), "json_validate_failed"),
])
def test_el_resultado_del_log_identifica_el_error(caplog, respuesta, resultado):
    caplog.set_level(logging.INFO)
    generador, _, _ = armar(respuesta, completion())
    generar(generador)
    assert registros_llm(caplog)[0].endswith(f"resultado={resultado}")


def test_los_logs_no_filtran_clave_prompt_ni_contenido(caplog):
    # Todo en DEBUG, como con LOG_LEVEL=DEBUG.
    caplog.set_level(logging.DEBUG)
    datos = DatosDelLead(nombre_empresa="EmpresaSecretaXYZ", cargo_contacto="CargoSecretoXYZ")
    contenido_malo = json.dumps({**CORREO, "asunto": "AsuntoGeneradoSecreto en acme.com"})
    generador, servidor, _ = armar(
        error_http(503, mensaje="CuerpoDe503Secreto"),
        error_http(400, code="json_validate_failed", failed_generation="TextoGeneradoSecreto"),
        completion(contenido_malo),
    )
    assert generador.generar(datos, lead_id=1, toma=1) == Fallido(MOTIVO_CONTENIDO_INVALIDO)
    assert len(servidor.requests) == 3

    todo = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert len(registros_llm(caplog)) == 3  # hubo logs: el test no pasa por no registrar nada
    for secreto in (
        CLAVE, "EmpresaSecretaXYZ", "CargoSecretoXYZ", "CuerpoDe503Secreto",
        "TextoGeneradoSecreto", "AsuntoGeneradoSecreto", SISTEMA[:60],
    ):
        assert secreto not in todo
    assert not [r for r in caplog.records if r.name.startswith(("groq", "httpx", "httpcore"))]


def test_crear_cliente_fija_los_loggers_del_sdk_en_warning():
    for nombre in ("groq", "httpx", "httpcore"):
        logging.getLogger(nombre).setLevel(logging.DEBUG)
    armar(completion())
    for nombre in ("groq", "httpx", "httpcore"):
        assert logging.getLogger(nombre).level == logging.WARNING


def test_sin_clave_o_modelo_no_se_puede_armar():
    base = {"database_url": "postgresql://u:p@db/x", "worker_activo": False}
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        crear_cliente(Settings(**base, groq_model=MODELO))
    cliente = crear_cliente(Settings(**base, groq_api_key=CLAVE))
    with pytest.raises(ValueError, match="GROQ_MODEL"):
        GeneradorDeCorreos(cliente, Settings(**base, groq_api_key=CLAVE), esperar=lambda s: False)
