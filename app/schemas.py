"""Modelos de la API. La entrada pasa por normalizacion.py antes de llegar al resto del código."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict
from pydantic_core import PydanticCustomError

from app import normalizacion

# Tipo de error que errores.py reconoce como "mensaje nuestro, se puede mostrar".
TIPO_VALOR_INVALIDO = "valor_invalido"


def _validador(funcion: Callable[[str], str]) -> AfterValidator:
    """Adapta una función de normalizacion.py a validador de pydantic."""

    def validar(valor: str) -> str:
        try:
            return funcion(valor)
        except normalizacion.ErrorDeValidacion as exc:
            raise PydanticCustomError(TIPO_VALOR_INVALIDO, str(exc)) from None

    return AfterValidator(validar)


class LeadEntrada(BaseModel):
    # extra="forbid": un campo desconocido es un error, no se ignora en silencio.
    # strict=True: un número o un null no se convierten a texto.
    model_config = ConfigDict(extra="forbid", strict=True)

    nombre_empresa: Annotated[str, _validador(normalizacion.normalizar_nombre_empresa)]
    dominio: Annotated[str, _validador(normalizacion.normalizar_dominio)]
    cargo_contacto: Annotated[str, _validador(normalizacion.normalizar_cargo_contacto)]


# Límites de la paginación de GET /leads.
LIMIT_POR_DEFECTO = 50
LIMIT_MAXIMO = 200
# Tope para que un offset absurdo responda 422 en vez de un error de Postgres (500).
OFFSET_MAXIMO = 1_000_000

Estado = Literal["pendiente", "procesando", "procesado", "error"]


class LeadCreado(BaseModel):
    """Respuesta del POST: lo justo para ubicar el lead después en el GET."""

    id: int
    estado: Estado
    creado_en: datetime


class Correo(BaseModel):
    asunto: str
    cuerpo: str


class LeadSalida(BaseModel):
    id: int
    nombre_empresa: str
    dominio: str
    cargo_contacto: str
    estado: Estado
    correo: Correo | None
    motivo_error: str | None
    creado_en: datetime

    @classmethod
    def desde_fila(cls, fila: dict) -> "LeadSalida":
        # El correo y el motivo se muestran según el estado, no solo según si la
        # columna tiene algo: así el significado de la respuesta no depende de
        # cómo el worker de la fase 2 limpie las columnas al reintentar.
        procesado = fila["estado"] == "procesado"
        return cls(
            id=fila["id"],
            nombre_empresa=fila["nombre_empresa"],
            dominio=fila["dominio"],
            cargo_contacto=fila["cargo_contacto"],
            estado=fila["estado"],
            correo=Correo(asunto=fila["asunto"], cuerpo=fila["cuerpo"]) if procesado else None,
            motivo_error=fila["motivo_error"] if fila["estado"] == "error" else None,
            creado_en=fila["creado_en"],
        )


class ListaLeads(BaseModel):
    leads: list[LeadSalida]
    limit: int
    offset: int
