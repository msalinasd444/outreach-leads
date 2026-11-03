# Tres etapas. La última, "produccion", es la que se construye por defecto (la que
# usa Render): solo la app y sus dependencias. "desarrollo" agrega pytest y los
# tests; la usa docker compose (target: desarrollo), en local y en el CI.

# --- Base común: Python, usuario sin privilegios y dependencias de la app ---------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Usuario sin privilegios: si la app se compromete, no es root en el contenedor.
RUN useradd --create-home --uid 10001 app

# Dependencias primero: esta capa se cachea mientras no cambien los requirements.
COPY requirements.txt ./
RUN pip install -r requirements.txt

EXPOSE 8000

# Render define PORT; en local usamos 8000. "exec" hace que uvicorn reciba
# SIGTERM directamente y se apague limpio (así también se detendrá el worker).
# Un solo proceso uvicorn: el worker vive dentro de él.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

# --- Desarrollo: + pytest, httpx2 y los tests (docker compose y CI) ---------------
FROM base AS desarrollo

COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY app ./app
COPY migraciones ./migraciones
COPY tests ./tests
COPY pytest.ini ./

USER app

# --- Producción (por defecto, por ser la última): solo lo que la app necesita ------
FROM base AS produccion

COPY app ./app
COPY migraciones ./migraciones

USER app
