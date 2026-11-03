"""Respuestas de error: nunca repiten lo que envió el cliente ni exponen detalles internos."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas import TIPO_VALOR_INVALIDO

log = logging.getLogger(__name__)

# Mensajes fijos por tipo de error. No usamos el "msg" de pydantic: así decidimos
# exactamente qué ve el cliente, aunque una versión futura cambie esos textos.
_MENSAJES = {
    "missing": "es obligatorio",
    "string_type": "debe ser texto",
    "int_parsing": "debe ser un número entero",
    "int_type": "debe ser un número entero",
    "greater_than_equal": "está fuera del rango permitido",
    "less_than_equal": "está fuera del rango permitido",
    "extra_forbidden": "el cuerpo contiene campos no permitidos",
    "json_invalid": "el cuerpo no es JSON válido",
    "model_attributes_type": "el cuerpo debe ser un objeto JSON",
}
_MENSAJE_GENERICO = "valor inválido"

# Tipos cuyo "loc" no mostramos: en extra_forbidden el nombre del campo lo eligió
# el cliente; en json_invalid es una posición dentro del texto que envió.
_SIN_CAMPO = {"extra_forbidden", "json_invalid"}


def _describir(error: dict) -> dict:
    tipo = error["type"]
    loc = error.get("loc", ())
    campo = None
    if tipo not in _SIN_CAMPO and len(loc) >= 2 and isinstance(loc[-1], str):
        campo = loc[-1]

    if tipo == TIPO_VALOR_INVALIDO:
        mensaje = error["msg"]  # escrito por nosotros en normalizacion.py
    elif tipo == "missing" and campo is None:
        mensaje = "falta el cuerpo de la solicitud"
    else:
        mensaje = _MENSAJES.get(tipo, _MENSAJE_GENERICO)
    return {"campo": campo, "error": mensaje}


async def _manejar_validacion(request: Request, exc: RequestValidationError) -> JSONResponse:
    errores = []
    for error in exc.errors():
        descripcion = _describir(error)
        if descripcion not in errores:  # p. ej. varios campos extra dan el mismo mensaje
            errores.append(descripcion)
    return JSONResponse(
        status_code=422,
        content={"detail": "Datos de entrada inválidos", "errores": errores},
    )


async def _manejar_error_inesperado(request: Request, exc: Exception) -> JSONResponse:
    # Starlette vuelve a lanzar la excepción después de este handler y uvicorn la
    # registra con su traceback. Aquí solo anotamos qué ruta falló (sin query ni cuerpo).
    log.error("Error no controlado en %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Error interno"})


def registrar_manejadores(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _manejar_validacion)
    app.add_exception_handler(Exception, _manejar_error_inesperado)
