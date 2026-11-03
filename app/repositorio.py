"""Todo el SQL sobre la tabla leads.

Los valores siempre viajan como parámetros (%(nombre)s), que psycopg envía aparte
de la consulta: ningún dato del usuario se concatena en el texto SQL.

Ninguna función hace COMMIT: lo decide quien llama (en la app, al salir del
bloque "with pool.connection()").
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import Connection

# --- Política de la cola ------------------------------------------------------------

# Un lead en "procesando" más tiempo que esto se da por trabado (p. ej. la app se
# reinició a mitad de una llamada). Debe superar el peor caso de llm.generar(),
# que con los máximos de la config es de 840 s (test_cola lo comprueba).
LEAD_TRABADO_TRAS = timedelta(minutes=15)
# Tomas como máximo: un lead que se traba o agota los reintentos transitorios
# esta cantidad de veces queda en "error".
LEAD_MAX_TOMAS = 3
MOTIVO_TRABADO = "No se pudo procesar el lead tras varios intentos."


@dataclass(frozen=True)
class LeadTomado:
    """Lo que el worker necesita de un lead tomado. Sin dominio: no va al prompt.

    (toma, tomado_en) identifican esta toma: cerrar o devolver el lead solo
    funciona si siguen siendo los de la fila. Si el rescate lo devolvió y otro
    worker lo volvió a tomar, ambos cambiaron y el resultado tardío se descarta.
    """

    id: int
    nombre_empresa: str
    cargo_contacto: str
    toma: int
    tomado_en: datetime


# --- Consultas de la API ------------------------------------------------------------


def insertar_lead(conn: Connection, *, nombre_empresa: str, dominio: str, cargo_contacto: str) -> dict:
    """Guarda un lead nuevo en estado 'pendiente'. No hace COMMIT: lo decide quien llama."""
    return conn.execute(
        """
        INSERT INTO leads (nombre_empresa, dominio, cargo_contacto, estado)
        VALUES (%(nombre_empresa)s, %(dominio)s, %(cargo_contacto)s, 'pendiente')
        RETURNING id, estado, creado_en
        """,
        {"nombre_empresa": nombre_empresa, "dominio": dominio, "cargo_contacto": cargo_contacto},
    ).fetchone()


def listar_leads(conn: Connection, *, limit: int, offset: int) -> list[dict]:
    """Leads del más reciente al más antiguo; id desempata los creados en el mismo instante."""
    return conn.execute(
        """
        SELECT id, nombre_empresa, dominio, cargo_contacto, estado,
               asunto, cuerpo, motivo_error, creado_en
        FROM leads
        ORDER BY creado_en DESC, id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"limit": limit, "offset": offset},
    ).fetchall()


# --- Consultas del worker -----------------------------------------------------------

# Condición de toda transición desde "procesando": la fila sigue en la misma toma.
_MISMA_TOMA = "id = %(id)s AND estado = 'procesando' AND intentos = %(toma)s AND tomado_en = %(tomado_en)s"


def _toma(lead: LeadTomado) -> dict:
    return {"id": lead.id, "toma": lead.toma, "tomado_en": lead.tomado_en}


def tomar_siguiente(conn: Connection) -> LeadTomado | None:
    """Pasa el pendiente más antiguo a "procesando" y lo devuelve; None si no hay.

    FOR UPDATE SKIP LOCKED: si otro worker está tomando una fila, esta consulta la
    salta en vez de esperarla, así que dos workers nunca reciben el mismo lead.
    """
    fila = conn.execute(
        """
        WITH siguiente AS (
            SELECT id FROM leads
            WHERE estado = 'pendiente'
            ORDER BY creado_en, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE leads
        SET estado = 'procesando', tomado_en = now(), intentos = intentos + 1, actualizado_en = now()
        FROM siguiente
        WHERE leads.id = siguiente.id
        RETURNING leads.id, leads.nombre_empresa, leads.cargo_contacto,
                  leads.intentos AS toma, leads.tomado_en
        """
    ).fetchone()
    return LeadTomado(**fila) if fila else None


def marcar_procesado(conn: Connection, lead: LeadTomado, *, asunto: str, cuerpo: str) -> bool:
    """Guarda el correo. False si la toma ya no es la vigente (el resultado se descarta)."""
    cursor = conn.execute(
        f"""
        UPDATE leads
        SET estado = 'procesado', asunto = %(asunto)s, cuerpo = %(cuerpo)s,
            motivo_error = NULL, tomado_en = NULL, actualizado_en = now()
        WHERE {_MISMA_TOMA}
        """,
        {**_toma(lead), "asunto": asunto, "cuerpo": cuerpo},
    )
    return cursor.rowcount == 1


def marcar_error(conn: Connection, lead: LeadTomado, *, motivo: str) -> bool:
    """Deja el lead en "error" con un motivo fijo. False si la toma ya no es la vigente."""
    cursor = conn.execute(
        f"""
        UPDATE leads
        SET estado = 'error', motivo_error = %(motivo)s, asunto = NULL, cuerpo = NULL,
            tomado_en = NULL, actualizado_en = now()
        WHERE {_MISMA_TOMA}
        """,
        {**_toma(lead), "motivo": motivo},
    )
    return cursor.rowcount == 1


def devolver_a_pendiente(conn: Connection, lead: LeadTomado, *, descontar_toma: bool) -> bool:
    """Vuelve a "pendiente" para que se intente más tarde. False si la toma ya no es la vigente.

    descontar_toma=True cuando no fue culpa del lead (límite de uso, configuración,
    apagado): esa toma no cuenta para LEAD_MAX_TOMAS.
    """
    cursor = conn.execute(
        f"""
        UPDATE leads
        SET estado = 'pendiente', tomado_en = NULL,
            intentos = intentos - %(descuento)s, actualizado_en = now()
        WHERE {_MISMA_TOMA}
        """,
        {**_toma(lead), "descuento": 1 if descontar_toma else 0},
    )
    return cursor.rowcount == 1


def rescatar_trabados(
    conn: Connection,
    *,
    trabado_tras: timedelta = LEAD_TRABADO_TRAS,
    max_tomas: int = LEAD_MAX_TOMAS,
) -> tuple[int, int]:
    """Leads en "procesando" desde hace más de trabado_tras: los que ya usaron todas
    sus tomas pasan a "error"; el resto vuelve a "pendiente". Devuelve (a_error, a_pendiente).

    Si otro worker está cerrando una de esas filas, el UPDATE espera su COMMIT y
    vuelve a evaluar el WHERE: una fila que ya no está en "procesando" no se toca.
    """
    parametros = {"trabado_tras": trabado_tras, "max_tomas": max_tomas, "motivo": MOTIVO_TRABADO}
    a_error = conn.execute(
        """
        UPDATE leads
        SET estado = 'error', motivo_error = %(motivo)s, tomado_en = NULL, actualizado_en = now()
        WHERE estado = 'procesando' AND tomado_en < now() - %(trabado_tras)s
          AND intentos >= %(max_tomas)s
        """,
        parametros,
    ).rowcount
    a_pendiente = conn.execute(
        """
        UPDATE leads
        SET estado = 'pendiente', tomado_en = NULL, actualizado_en = now()
        WHERE estado = 'procesando' AND tomado_en < now() - %(trabado_tras)s
        """,
        parametros,
    ).rowcount
    return a_error, a_pendiente
