"""Modelos de la API. La entrada pasa por normalizacion.py antes de llegar al resto del código."""

from collections.abc import Callable
from typing import Annotated

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
