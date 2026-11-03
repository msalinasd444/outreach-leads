"""Pool de conexiones a PostgreSQL y aplicación del esquema al arrancar."""

import logging
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings

log = logging.getLogger(__name__)

RUTA_ESQUEMA = Path(__file__).resolve().parent.parent / "migraciones" / "esquema.sql"


def crear_pool(settings: Settings) -> ConnectionPool:
    """Crea y abre el pool. Falla al arrancar si la BD no responde, no en la primera request."""
    pool = ConnectionPool(
        conninfo=settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={
            # Filas como dict: el repositorio arma las respuestas por nombre de columna.
            "row_factory": dict_row,
            # Sin prepared statements: el endpoint "pooled" de Neon pasa por PgBouncer
            # en modo transacción, donde no funcionan. A este volumen no se nota.
            "prepare_threshold": None,
            # Neon suspende la BD inactiva y tarda unos segundos en despertarla.
            "connect_timeout": 10,
        },
        # Comprueba cada conexión antes de entregarla: Neon cierra las inactivas.
        check=ConnectionPool.check_connection,
        # Máximo de espera (segundos) por una conexión libre antes de fallar.
        timeout=10,
        open=False,
    )
    # Si no logra conectar en 30 s, el pool se cierra solo y lanza PoolTimeout.
    pool.open(wait=True, timeout=30)
    return pool


def aplicar_esquema(pool: ConnectionPool) -> None:
    """Ejecuta migraciones/esquema.sql. Es idempotente; se llama en cada arranque."""
    sql = RUTA_ESQUEMA.read_text(encoding="utf-8")
    # Al salir del bloque se hace COMMIT; si algo falla, ROLLBACK: el esquema
    # queda completo o no cambia.
    with pool.connection() as conn:
        conn.execute(sql)
    log.info("Esquema de base de datos aplicado")
