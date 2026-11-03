"""Endpoints HTTP. Solo traducen entre HTTP y el repositorio; las reglas viven en schemas y normalizacion."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from app import repositorio
from app.schemas import (
    LIMIT_MAXIMO,
    LIMIT_POR_DEFECTO,
    OFFSET_MAXIMO,
    LeadCreado,
    LeadEntrada,
    LeadSalida,
    ListaLeads,
)
from app.worker import Worker

log = logging.getLogger(__name__)

router = APIRouter()


def obtener_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


Pool = Annotated[ConnectionPool, Depends(obtener_pool)]


def obtener_worker(request: Request) -> Worker | None:
    # None con WORKER_ACTIVO=false.
    return getattr(request.app.state, "worker", None)


WorkerOpcional = Annotated[Worker | None, Depends(obtener_worker)]


@router.post("/leads", status_code=status.HTTP_202_ACCEPTED)
def crear_lead(lead: LeadEntrada, pool: Pool, worker: WorkerOpcional) -> LeadCreado:
    # 202: el lead quedó guardado, pero el correo se genera después.
    # El bloque hace COMMIT al salir, antes del return: si responde 202, el lead está guardado.
    with pool.connection() as conn:
        fila = repositorio.insertar_lead(
            conn,
            nombre_empresa=lead.nombre_empresa,
            dominio=lead.dominio,
            cargo_contacto=lead.cargo_contacto,
        )
    # Después del COMMIT, para que el worker ya pueda ver el lead. No espera al LLM:
    # avisar() solo despierta al hilo.
    if worker is not None:
        worker.avisar()
    return LeadCreado(**fila)


@router.get("/leads")
def listar_leads(
    pool: Pool,
    limit: Annotated[int, Query(ge=1, le=LIMIT_MAXIMO)] = LIMIT_POR_DEFECTO,
    offset: Annotated[int, Query(ge=0, le=OFFSET_MAXIMO)] = 0,
) -> ListaLeads:
    with pool.connection() as conn:
        filas = repositorio.listar_leads(conn, limit=limit, offset=offset)
    return ListaLeads(leads=[LeadSalida.desde_fila(f) for f in filas], limit=limit, offset=offset)


@router.get("/health")
def health(pool: Pool) -> JSONResponse:
    """Para Render: 200 si la base responde, 503 si no."""
    try:
        with pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
    except Exception:
        log.warning("Health check: la base de datos no responde", exc_info=True)
        return JSONResponse(status_code=503, content={"estado": "sin base de datos"})
    return JSONResponse(content={"estado": "ok"})
