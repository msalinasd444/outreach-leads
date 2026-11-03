"""Worker que genera los correos en segundo plano, en un hilo del mismo proceso que la API.

Vive aquí y no como servicio aparte porque en el plan gratuito de Render los
background workers no son gratis. Usa la tabla leads como cola (repositorio.py).

Cada vuelta: rescata los leads trabados (cada INTERVALO_INACTIVO_S), toma un
pendiente, genera el correo SIN conexión ni transacción abiertas y cierra el lead
según el resultado. Si no hay pendientes, duerme hasta que el POST avise o pase
INTERVALO_INACTIVO_S.
"""

import logging
import threading
import time
import traceback
from collections.abc import Callable

import httpx
from psycopg import Connection
from psycopg_pool import ConnectionPool

from app import repositorio as repo
from app.config import Settings
from app.llm import Devuelto, Fallido, GeneradorDeCorreos, Generado, NoDisponible, crear_cliente
from app.prompt import DatosDelLeadInvalidos, preparar_datos
from app.repositorio import LEAD_MAX_TOMAS, MOTIVO_TRABADO, LeadTomado

log = logging.getLogger(__name__)

MOTIVO_DATOS_INVALIDOS = "Los datos del lead no son válidos para generar el correo."

# Sin avisos del POST, la cola se revisa (y se rescatan trabados) cada 10 minutos.
# Más seguido mantendría despierta la base de Neon sin necesidad: los leads nuevos
# despiertan al worker con avisar().
INTERVALO_INACTIVO_S = 600.0
# Tras un error inesperado (la base no responde, un bug), antes de volver a intentar.
PAUSA_TRAS_ERROR_S = 30.0
# Al apagar, cuánto se espera al hilo. Si hay una llamada al LLM en curso puede no
# alcanzar: el lead queda en "procesando" y el rescate lo retoma más tarde.
ESPERA_AL_DETENER_S = 10.0


class Worker:
    def __init__(
        self,
        pool: ConnectionPool,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        intervalo_inactivo_s: float = INTERVALO_INACTIVO_S,
    ) -> None:
        self._pool = pool
        self._intervalo_inactivo_s = intervalo_inactivo_s
        # _parada: hay que terminar. Corta cualquier espera del generador o de una pausa.
        # _despertar: hay trabajo nuevo (o hay que terminar). Corta la espera inactiva.
        self._parada = threading.Event()
        self._despertar = threading.Event()
        self._cliente = crear_cliente(settings, http_client=http_client)
        self._generador = GeneradorDeCorreos(self._cliente, settings, esperar=self._parada.wait)
        self._hilo = threading.Thread(target=self._bucle, name="worker-leads", daemon=True)

    def iniciar(self) -> None:
        self._hilo.start()

    def avisar(self) -> None:
        """Hay un lead nuevo. No bloquea: solo despierta al worker si estaba esperando."""
        self._despertar.set()

    def detener(self) -> None:
        self._parada.set()
        self._despertar.set()
        self._hilo.join(ESPERA_AL_DETENER_S)
        if self._hilo.is_alive():
            # Daemon: no impide que el proceso termine. No cerramos el cliente HTTP
            # mientras otro hilo lo está usando.
            log.warning("worker_no_termino: una llamada al LLM seguía en curso; el rescate retomará su lead")
            return
        self._cliente.close()
        log.info("worker_detenido")

    # --- Bucle --------------------------------------------------------------------

    def _bucle(self) -> None:
        log.info("worker_iniciado")
        proximo_rescate = time.monotonic()
        while not self._parada.is_set():
            try:
                if time.monotonic() >= proximo_rescate:
                    self._rescatar()
                    proximo_rescate = time.monotonic() + self._intervalo_inactivo_s
                pausa = self._procesar_uno()
            except Exception as exc:
                # Sin str(exc): un error de psycopg puede incluir la fila (datos del lead
                # o el correo). El tipo y las líneas del traceback bastan para depurar.
                log.error(
                    "worker_error tipo=%s; se reintenta en %.0f s\n%s",
                    type(exc).__name__, PAUSA_TRAS_ERROR_S, "".join(traceback.format_tb(exc.__traceback__)),
                )
                pausa = PAUSA_TRAS_ERROR_S

            # Revisar la parada aquí, después del clear() de _procesar_uno: si detener()
            # llegó antes de ese clear(), el aviso de _despertar se borró, pero _parada
            # ya está puesta (detener la pone primero). Si llegó después, _despertar
            # sigue puesto y la espera vuelve de inmediato.
            if self._parada.is_set():
                break
            if pausa is None:
                self._despertar.wait(self._intervalo_inactivo_s)
            elif pausa > 0:
                log.info("worker_pausa segundos=%.0f", pausa)
                self._parada.wait(pausa)
        log.info("worker_fin_del_bucle")

    def _rescatar(self) -> None:
        with self._pool.connection() as conn:
            a_error, a_pendiente = repo.rescatar_trabados(conn)
        if a_error or a_pendiente:
            log.warning("rescate_trabados a_error=%d a_pendiente=%d", a_error, a_pendiente)

    def _procesar_uno(self) -> float | None:
        """Procesa un lead. Devuelve None si no había pendientes, o los segundos de
        pausa antes de seguir (0: seguir de inmediato)."""
        # Se limpia ANTES de mirar la cola: un aviso que llegue después de esta
        # consulta deja el evento puesto y la espera inactiva no se lo pierde.
        self._despertar.clear()
        with self._pool.connection() as conn:  # COMMIT al salir: la toma queda registrada
            lead = repo.tomar_siguiente(conn)
        if lead is None:
            return None
        log.info("lead_tomado lead_id=%d toma=%d", lead.id, lead.toma)

        try:
            datos = preparar_datos(nombre_empresa=lead.nombre_empresa, cargo_contacto=lead.cargo_contacto)
        except DatosDelLeadInvalidos:
            self._cerrar(lead, "error", lambda c: repo.marcar_error(c, lead, motivo=MOTIVO_DATOS_INVALIDOS))
            return 0.0

        # Sin conexión abierta: la llamada puede tardar y no debe retener nada de la base.
        try:
            resultado = self._generador.generar(datos, lead_id=lead.id, toma=lead.toma)
        except Exception:
            # Un error que no es del LLM (un bug): la toma cuenta, para que un lead que
            # siempre lo provoca termine en "error" y no se reintente para siempre.
            self._devolver_o_fallar(lead, MOTIVO_TRABADO)
            raise

        match resultado:
            case Generado(correo=correo):
                self._cerrar(lead, "procesado", lambda c: repo.marcar_procesado(
                    c, lead, asunto=correo.asunto, cuerpo=correo.cuerpo))
                return 0.0
            case Fallido(motivo=motivo):
                self._cerrar(lead, "error", lambda c: repo.marcar_error(c, lead, motivo=motivo))
                return 0.0
            case NoDisponible(motivo=motivo, pausa_s=pausa):
                self._devolver_o_fallar(lead, motivo)
                return pausa
            case Devuelto(razon=razon, pausa_s=pausa):
                # No fue culpa del lead: la toma no cuenta.
                self._cerrar(lead, "pendiente", lambda c: repo.devolver_a_pendiente(c, lead, descontar_toma=True),
                             razon=razon)
                return pausa
        raise AssertionError("resultado desconocido")

    def _devolver_o_fallar(self, lead: LeadTomado, motivo: str) -> None:
        """La toma cuenta: si era la última, el lead queda en "error" con el motivo."""
        if lead.toma >= LEAD_MAX_TOMAS:
            self._cerrar(lead, "error", lambda c: repo.marcar_error(c, lead, motivo=motivo))
        else:
            self._cerrar(lead, "pendiente", lambda c: repo.devolver_a_pendiente(c, lead, descontar_toma=False))

    def _cerrar(
        self, lead: LeadTomado, estado: str, operacion: Callable[[Connection], bool], *, razon: str | None = None,
    ) -> None:
        with self._pool.connection() as conn:
            vigente = operacion(conn)
        if vigente:
            log.info("lead_cerrado lead_id=%d toma=%d estado=%s razon=%s", lead.id, lead.toma, estado, razon or "-")
        else:
            # Se tardó más que el umbral, el rescate lo devolvió y otro worker lo tomó.
            log.warning("resultado_descartado lead_id=%d toma=%d: el lead ya no está en esta toma", lead.id, lead.toma)
