"""Todo el SQL sobre la tabla leads.

Los valores siempre viajan como parámetros (%(nombre)s), que psycopg envía aparte
de la consulta: ningún dato del usuario se concatena en el texto SQL.
"""

from psycopg import Connection


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
