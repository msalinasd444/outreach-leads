FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Usuario sin privilegios: si la app se compromete, no es root en el contenedor.
RUN useradd --create-home --uid 10001 app

# Dependencias primero: esta capa se cachea mientras no cambien los requirements.
COPY requirements.txt requirements-dev.txt ./
ARG INSTALAR_DEV=false
RUN pip install -r requirements.txt \
 && if [ "$INSTALAR_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

COPY app ./app
COPY migraciones ./migraciones
COPY tests ./tests
COPY pytest.ini ./

USER app
EXPOSE 8000

# Render define PORT; en local usamos 8000. "exec" hace que uvicorn reciba
# SIGTERM directamente y se apague limpio (así también se detendrá el worker).
# Un solo proceso uvicorn: el worker vivirá dentro de él.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
