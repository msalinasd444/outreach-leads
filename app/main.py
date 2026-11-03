"""Punto de entrada: arma la app, abre el pool y aplica el esquema al arrancar."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import aplicar_esquema, crear_pool
from app.errores import registrar_manejadores
from app.rutas import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Bloquea el arranque hasta tener base de datos y esquema: si falla, la app no
    # queda levantada a medias (Render la marca como caída y lo vemos en el log).
    pool = crear_pool(settings)
    try:
        aplicar_esquema(pool)
        app.state.pool = pool
        # Aquí se arrancará el hilo del worker.
        yield
        # Aquí se le avisará al worker que pare y se esperará a que termine.
    finally:
        pool.close()


app = FastAPI(title="Outreach de leads", lifespan=lifespan)
registrar_manejadores(app)
app.include_router(router)
