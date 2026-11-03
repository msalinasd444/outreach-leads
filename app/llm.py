"""Llamadas a Groq para generar el correo de un lead.

GeneradorDeCorreos.generar() hace todo lo que toca al LLM para un lead: respeta
el ritmo máximo de llamadas, reintenta los errores transitorios respetando
Retry-After, repite una vez si el contenido no pasa la validación y devuelve un
Resultado que el worker traduce a un cambio de estado en la base.

Nunca se registra la clave, el prompt, la respuesta del modelo ni str() de una
excepción del SDK: ese texto incluye el cuerpo de la respuesta, que en un 400
json_validate_failed trae lo que generó el modelo.
"""

import email.utils
import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import groq
import httpx

from app.config import Settings
from app.correo import CorreoGenerado, CorreoInvalido, validar_correo
from app.prompt import DatosDelLead, armar_mensajes

log = logging.getLogger(__name__)

# Motivos que quedan en la base y se muestran en GET /leads: fijos, sin detalles internos.
MOTIVO_CONTENIDO_INVALIDO = "El modelo no generó un correo válido."
MOTIVO_RECHAZADO = "El servicio de generación rechazó la solicitud."
MOTIVO_NO_DISPONIBLE = "El servicio de generación no respondió; se agotaron los intentos."

# Con gpt-oss los tokens de razonamiento cuentan aquí. En una prueba real la
# respuesta usó unos 130 tokens: 1000 dejan margen de sobra.
MAX_TOKENS_RESPUESTA = 1000
# Más baja que el valor por defecto (1): el correo debe seguir reglas, no sorprender.
TEMPERATURA = 0.5
CONEXION_MAX_S = 5.0

# Por lead: hasta 2 reintentos por errores transitorios y 1 por contenido
# inválido, compartidos. Como mucho 4 llamadas por lead.
MAX_REINTENTOS_TRANSITORIOS = 2
MAX_REINTENTOS_CONTENIDO = 1
# Espera antes de cada reintento transitorio cuando no hay Retry-After (con ±25 %).
ESPERAS_BASE_S = (1.0, 2.0)
# Una espera más larga no se hace con el lead tomado: se devuelve y se pausa el worker.
MAX_ESPERA_S = 60.0
PAUSA_TRAS_TRANSITORIOS_S = 30.0
PAUSA_CONFIGURACION_S = 300.0
PAUSA_MAXIMA_S = 24 * 3600.0

_ESTADOS_TRANSITORIOS = {408, 429}  # y todos los 5xx
_ESTADOS_DE_CONFIGURACION = {401, 403, 404}  # clave inválida, sin permiso o modelo inexistente
# Por si llegan con otro status (p. ej. un 400 por modelo retirado): también son de configuración.
_CODIGOS_DE_CONFIGURACION = {"model_not_found", "model_decommissioned", "invalid_api_key"}


# --- Resultados que recibe el worker -------------------------------------------

@dataclass(frozen=True)
class Generado:
    correo: CorreoGenerado


@dataclass(frozen=True)
class Fallido:
    """Error definitivo del lead: queda en "error" con este motivo."""

    motivo: str


@dataclass(frozen=True)
class NoDisponible:
    """Se agotaron los reintentos transitorios. Cuenta como toma: el worker lo
    devuelve a "pendiente" o, si era la última toma, lo deja en "error" con el motivo."""

    motivo: str
    pausa_s: float


@dataclass(frozen=True)
class Devuelto:
    """No es culpa del lead (límite de uso, configuración, apagado): vuelve a
    "pendiente" sin contar la toma y el worker se pausa pausa_s segundos."""

    razon: str
    pausa_s: float


Resultado = Generado | Fallido | NoDisponible | Devuelto


# --- Cliente --------------------------------------------------------------------

def silenciar_loggers_del_sdk() -> None:
    """Fija estos loggers en WARNING, sea cual sea LOG_LEVEL.

    En DEBUG, "groq" registra las opciones de cada request, mensajes incluidos (el
    prompt con los datos del lead). httpx y httpcore no aportan nada que no esté
    en nuestra línea por llamada.
    """
    for nombre in ("groq", "httpx", "httpcore"):
        logging.getLogger(nombre).setLevel(logging.WARNING)


def crear_cliente(settings: Settings, *, http_client: httpx.Client | None = None) -> groq.Groq:
    """Cliente sin reintentos propios: los hace GeneradorDeCorreos, que respeta todo
    Retry-After (el SDK ignora los de más de 60 s) y registra cada intento."""
    if settings.groq_api_key is None:
        raise ValueError("falta GROQ_API_KEY")
    silenciar_loggers_del_sdk()
    return groq.Groq(
        api_key=settings.groq_api_key.get_secret_value(),
        max_retries=0,
        timeout=httpx.Timeout(settings.groq_timeout, connect=min(CONEXION_MAX_S, settings.groq_timeout)),
        http_client=http_client,
    )


def peor_caso_por_lead_s(settings: Settings) -> float:
    """Cota superior del tiempo que generar() puede tener tomado un lead, sin contar
    una respuesta que llega a goteo (los timeouts de httpx son por operación, no
    totales). El umbral para dar por trabado un lead debe superarla."""
    llamadas = 1 + MAX_REINTENTOS_TRANSITORIOS + MAX_REINTENTOS_CONTENIDO
    ritmo_s = 60.0 / settings.groq_rpm
    return llamadas * (settings.groq_timeout + ritmo_s) + MAX_REINTENTOS_TRANSITORIOS * MAX_ESPERA_S


# --- Retry-After y clasificación de errores ---------------------------------------

def leer_retry_after(headers: httpx.Headers, *, ahora: datetime) -> float | None:
    """Segundos a esperar según retry-after-ms o Retry-After (segundos o fecha HTTP)."""
    ms = headers.get("retry-after-ms")
    if ms is not None:
        try:
            segundos = float(ms) / 1000
        except ValueError:
            pass
        else:
            if math.isfinite(segundos):
                return max(0.0, segundos)

    valor = headers.get("retry-after")
    if valor is None:
        return None
    try:
        segundos = float(valor)
    except ValueError:
        try:
            fecha = email.utils.parsedate_to_datetime(valor)
        except (TypeError, ValueError):
            return None
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        segundos = (fecha - ahora).total_seconds()
    return max(0.0, segundos) if math.isfinite(segundos) else None


@dataclass(frozen=True)
class _Falla:
    tipo: str  # "transitoria", "contenido", "configuracion" o "rechazo"
    codigo: str  # para el log: "timeout", "conexion", "http_503"... siempre texto nuestro
    status: int | None = None
    retry_after_s: float | None = None


def _codigo_de_error(cuerpo: object) -> str | None:
    """error.code del cuerpo de un 4xx de Groq, p. ej. "json_validate_failed"."""
    if isinstance(cuerpo, dict) and isinstance(error := cuerpo.get("error"), dict):
        codigo = error.get("code")
        return codigo if isinstance(codigo, str) else None
    return None


def _clasificar(exc: groq.GroqError, *, ahora: datetime) -> _Falla:
    # APITimeoutError hereda de APIConnectionError: va primero.
    if isinstance(exc, groq.APITimeoutError):
        return _Falla("transitoria", "timeout")
    if isinstance(exc, groq.APIConnectionError):
        return _Falla("transitoria", "conexion")
    if isinstance(exc, groq.APIStatusError):
        status = exc.status_code
        codigo = f"http_{status}"
        if status in _ESTADOS_TRANSITORIOS or status >= 500:
            return _Falla("transitoria", codigo, status, leer_retry_after(exc.response.headers, ahora=ahora))
        codigo_de_error = _codigo_de_error(exc.body)
        if status == 400 and codigo_de_error == "json_validate_failed":
            return _Falla("contenido", "json_validate_failed", status)
        if status in _ESTADOS_DE_CONFIGURACION or codigo_de_error in _CODIGOS_DE_CONFIGURACION:
            return _Falla("configuracion", codigo, status)
        return _Falla("rechazo", codigo, status)
    return _Falla("rechazo", type(exc).__name__)


# --- Generador ----------------------------------------------------------------------

class GeneradorDeCorreos:
    """Uno por worker. No es seguro para varios hilos: lleva la hora de la última llamada.

    esperar(segundos) debe devolver True si hay que dejar de esperar porque el
    worker se está apagando (como threading.Event.wait).
    """

    def __init__(
        self,
        cliente: groq.Groq,
        settings: Settings,
        *,
        esperar: Callable[[float], bool],
        reloj: Callable[[], float] = time.monotonic,
        ahora: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        aleatorio: Callable[[], float] = random.random,
    ) -> None:
        if settings.groq_model is None:
            raise ValueError("falta GROQ_MODEL")
        self._cliente = cliente
        self._modelo = settings.groq_model
        self._esfuerzo = settings.groq_reasoning_effort
        self._intervalo_s = 60.0 / settings.groq_rpm
        self._esperar = esperar
        self._reloj = reloj
        self._ahora = ahora
        self._aleatorio = aleatorio
        self._ultima_llamada: float | None = None

    def generar(self, datos: DatosDelLead, *, lead_id: int, toma: int) -> Resultado:
        regla_fallida: str | None = None
        reintentos_transitorios = 0
        reintentos_contenido = 0
        intento = 0
        # Hora (del reloj monotónico) antes de la cual no se reintenta: la fija un
        # Retry-After o el backoff. Se combina con el ritmo en una sola espera.
        no_antes_de: float | None = None

        while True:
            if self._esperar_turno(no_antes_de):
                return Devuelto("apagado", 0.0)
            intento += 1
            mensajes = armar_mensajes(datos, regla_fallida=regla_fallida)
            inicio = self._reloj()
            try:
                respuesta = self._cliente.chat.completions.create(**self._parametros(mensajes))
            except groq.GroqError as exc:
                falla = _clasificar(exc, ahora=self._ahora())
                self._registrar(lead_id, toma, intento, inicio, None, falla.codigo, _NIVEL_POR_TIPO[falla.tipo])

                if falla.tipo == "configuracion":
                    log.error("Groq rechazó la clave o el modelo (%s); el worker se pausa", falla.codigo)
                    return Devuelto("configuracion", PAUSA_CONFIGURACION_S)
                if falla.tipo == "rechazo":
                    return Fallido(MOTIVO_RECHAZADO)
                if falla.tipo == "transitoria":
                    if falla.retry_after_s is not None:
                        espera, motivo_espera = falla.retry_after_s, "retry_after"
                    else:
                        espera, motivo_espera = self._backoff(reintentos_transitorios), "backoff"
                    if reintentos_transitorios >= MAX_REINTENTOS_TRANSITORIOS or espera > MAX_ESPERA_S:
                        pausa = min(max(espera, PAUSA_TRAS_TRANSITORIOS_S), PAUSA_MAXIMA_S)
                        if falla.status == 429:
                            # Límite de uso de la cuenta: no es culpa del lead.
                            return Devuelto("limite_de_uso", pausa)
                        return NoDisponible(MOTIVO_NO_DISPONIBLE, pausa)
                    reintentos_transitorios += 1
                    log.info("espera_llm lead_id=%d motivo=%s segundos=%.1f", lead_id, motivo_espera, espera)
                    no_antes_de = self._reloj() + espera
                    continue
                # "contenido": el modelo no produjo JSON válido y Groq lo rechazó.
                regla = "json"
            else:
                try:
                    correo = self._validar(respuesta, datos)
                except CorreoInvalido as exc:
                    regla = exc.regla
                    self._registrar(lead_id, toma, intento, inicio, respuesta.usage,
                                    f"contenido_invalido:{regla}", logging.WARNING)
                else:
                    self._registrar(lead_id, toma, intento, inicio, respuesta.usage, "ok", logging.INFO)
                    return Generado(correo)

            if reintentos_contenido >= MAX_REINTENTOS_CONTENIDO:
                return Fallido(MOTIVO_CONTENIDO_INVALIDO)
            reintentos_contenido += 1
            regla_fallida = regla

    def _esperar_turno(self, no_antes_de: float | None) -> bool:
        """Espera una sola vez lo que exija lo más restrictivo: el reintento pendiente
        (no_antes_de) o el ritmo de 60/GROQ_RPM segundos entre el inicio de dos llamadas.
        Devuelve True si la espera se cortó porque el worker se apaga."""
        objetivo = no_antes_de if no_antes_de is not None else -math.inf
        if self._ultima_llamada is not None:
            objetivo = max(objetivo, self._ultima_llamada + self._intervalo_s)
        falta = objetivo - self._reloj()
        if falta > 0:
            log.debug("espera_llm segundos=%.1f", falta)
            if self._esperar(falta):
                return True
        self._ultima_llamada = self._reloj()
        return False

    def _backoff(self, reintento: int) -> float:
        base = ESPERAS_BASE_S[min(reintento, len(ESPERAS_BASE_S) - 1)]
        return base * (0.75 + 0.5 * self._aleatorio())

    def _parametros(self, mensajes: list[dict[str, str]]) -> dict:
        parametros = {
            "model": self._modelo,
            "messages": mensajes,
            "response_format": {"type": "json_object"},
            "max_completion_tokens": MAX_TOKENS_RESPUESTA,
            "temperature": TEMPERATURA,
        }
        if self._esfuerzo is not None:
            parametros["reasoning_effort"] = self._esfuerzo
        return parametros

    @staticmethod
    def _validar(respuesta, datos: DatosDelLead) -> CorreoGenerado:
        if not respuesta.choices:
            raise CorreoInvalido("json")
        opcion = respuesta.choices[0]
        # Cortada por max_completion_tokens: aunque el JSON cerrara, no está completa.
        if opcion.finish_reason == "length":
            raise CorreoInvalido("respuesta_larga")
        return validar_correo(opcion.message.content, nombre_empresa=datos.nombre_empresa)

    def _registrar(self, lead_id, toma, intento, inicio, uso, resultado, nivel) -> None:
        latencia_ms = round((self._reloj() - inicio) * 1000)
        log.log(
            nivel,
            "llamada_llm lead_id=%d toma=%d intento=%d modelo=%s latencia_ms=%d "
            "tokens_prompt=%s tokens_completion=%s resultado=%s",
            lead_id, toma, intento, self._modelo, latencia_ms,
            uso.prompt_tokens if uso else "-", uso.completion_tokens if uso else "-", resultado,
        )


_NIVEL_POR_TIPO = {
    "transitoria": logging.WARNING,
    "contenido": logging.WARNING,
    "configuracion": logging.ERROR,
    "rechazo": logging.ERROR,
}
