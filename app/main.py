"""Punto de entrada: arma la app, abre el pool, aplica el esquema y arranca el worker."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import aplicar_esquema, crear_pool
from app.errores import registrar_manejadores
from app.rutas import router
from app.worker import Worker


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
    worker = None
    try:
        aplicar_esquema(pool)
        app.state.pool = pool
        if settings.worker_activo:
            worker = Worker(pool, settings)
            worker.iniciar()
        app.state.worker = worker
        yield
    finally:
        # Primero el worker, que usa el pool; después el pool.
        if worker is not None:
            worker.detener()
        pool.close()


app = FastAPI(title="Outreach de leads", lifespan=lifespan)
registrar_manejadores(app)
app.include_router(router)
