"""Fixtures compartidas. Los tests usan una BD real (TEST_DATABASE_URL), nunca la de la app."""

import os

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from app.config import Settings, get_settings
from app.db import aplicar_esquema, crear_pool
from app.main import app

BASE_LEAD = {"nombre_empresa": "Acme", "dominio": "acme.com", "cargo_contacto": "CTO"}

# Variables que compose le pasa al contenedor desde tu .env (puede traer la clave real).
VARIABLES_DEL_WORKER = (
    "WORKER_ACTIVO",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "GROQ_REASONING_EFFORT",
    "GROQ_TIMEOUT",
    "GROQ_RPM",
)


@pytest.fixture(scope="session", autouse=True)
def sin_configuracion_real_del_worker():
    """Ningún test lee la clave ni la config de Groq del entorno: cada test arma la suya."""
    with pytest.MonkeyPatch.context() as mp:
        for nombre in VARIABLES_DEL_WORKER:
            mp.delenv(nombre, raising=False)
        yield


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
    """Pool creado con el mismo código que usa la app, con el esquema ya aplicado.

    La tabla se borra antes: CREATE TABLE IF NOT EXISTS no cambia una tabla que ya
    existe, y así los tests siempre corren contra el esquema actual. Es la base de
    tests: test_db_url ya comprobó que no es la de la app.
    """
    pool = crear_pool(Settings(database_url=test_db_url, worker_activo=False))
    with pool.connection() as c:
        c.execute("DROP TABLE IF EXISTS leads")
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


@pytest.fixture
def insertar(conn):
    """Inserta una fila directo en la BD (saltándose la API), con valores válidos por defecto."""

    def _insertar(**cambios):
        fila = {**BASE_LEAD, **cambios}
        query = sql.SQL("INSERT INTO leads ({cols}) VALUES ({vals}) RETURNING *").format(
            cols=sql.SQL(", ").join(map(sql.Identifier, fila)),
            vals=sql.SQL(", ").join([sql.Placeholder()] * len(fila)),
        )
        return conn.execute(query, list(fila.values())).fetchone()

    return _insertar


@pytest.fixture
def cliente(conn, test_db_url, monkeypatch):
    """La app real, con su lifespan, apuntando a la BD de test."""
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    # Los tests de la API no generan correos; el worker se prueba aparte.
    monkeypatch.setenv("WORKER_ACTIVO", "false")
    get_settings.cache_clear()
    with TestClient(app) as cliente:  # el "with" ejecuta el lifespan (pool + esquema)
        yield cliente
    get_settings.cache_clear()
