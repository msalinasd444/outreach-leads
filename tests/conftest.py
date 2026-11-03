"""Fixtures compartidas. Los tests usan una BD real (TEST_DATABASE_URL), nunca la de la app."""

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from app.config import Settings
from app.db import aplicar_esquema, crear_pool


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.exit("Falta TEST_DATABASE_URL. Corre: docker compose run --rm --build api pytest", returncode=2)
    if url == os.environ.get("DATABASE_URL"):
        pytest.exit("TEST_DATABASE_URL apunta a la BD de la app; los tests borran datos.", returncode=2)
    return url


@pytest.fixture(scope="session")
def pool(test_db_url):
    """Pool creado con el mismo código que usa la app, con el esquema ya aplicado."""
    pool = crear_pool(Settings(database_url=test_db_url))
    aplicar_esquema(pool)
    yield pool
    pool.close()


@pytest.fixture
def conn(pool, test_db_url):
    """Conexión en autocommit con la tabla vacía.

    Autocommit: cada sentencia es su propia transacción, así un INSERT que la BD
    rechaza no deja la conexión en estado de error para el resto del test.
    """
    with psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE leads RESTART IDENTITY")
        yield conn
