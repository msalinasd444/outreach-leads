"""Configuración leída de variables de entorno. Falla al arrancar si falta algo obligatorio."""

from functools import lru_cache

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Sin env_file: las variables llegan del entorno (compose o Render), nunca de un archivo en la imagen.
    model_config = SettingsConfigDict(extra="ignore")

    # SecretStr: si la config termina en un log o un traceback, la URL (que lleva
    # la contraseña) aparece como '**********'.
    database_url: SecretStr
    db_pool_min: int = Field(default=1, ge=1)
    db_pool_max: int = Field(default=5, ge=1)
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")

    @model_validator(mode="after")
    def _pool_coherente(self) -> "Settings":
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("DB_POOL_MIN no puede ser mayor que DB_POOL_MAX")
        return self


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # El error de pydantic incluye los valores recibidos; aquí solo nombramos las
        # variables problemáticas. "from None" evita que el traceback original
        # (con esos valores) se imprima encadenado.
        errores = exc.errors()
        campos = sorted({str(e["loc"][0]).upper() for e in errores if e["loc"]})
        # Los errores sin campo vienen de nuestros validadores; su texto lo escribimos
        # nosotros y no contiene valores, así que se puede mostrar.
        generales = [e["msg"] for e in errores if not e["loc"]]

        partes = []
        if campos:
            partes.append(", ".join(campos))
        partes.extend(generales)

        raise RuntimeError("Configuración inválida o incompleta: " + "; ".join(partes)) from None
