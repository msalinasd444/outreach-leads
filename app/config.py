"""Configuración leída de variables de entorno. Falla al arrancar si falta algo obligatorio."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Los que acepta gpt-oss. El SDK también permite "none" y "default", pero son de
# los modelos Qwen: con gpt-oss cada llamada respondería 400, así que mejor no arrancar.
EsfuerzoRazonamiento = Literal["low", "medium", "high"]


class Settings(BaseSettings):
    # Sin env_file: las variables llegan del entorno (compose o Render), nunca de un archivo en la imagen.
    model_config = SettingsConfigDict(extra="ignore")

    # SecretStr: si la config termina en un log o un traceback, la URL (que lleva
    # la contraseña) aparece como '**********'.
    database_url: SecretStr
    db_pool_min: int = Field(default=1, ge=1)
    db_pool_max: int = Field(default=5, ge=1)
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")

    # Worker y LLM. Con el worker apagado (tests de la API) no hace falta nada de Groq.
    worker_activo: bool = True
    groq_api_key: SecretStr | None = None
    # El nombre del modelo va en cada línea de log: solo caracteres que no puedan
    # partir ni falsear una línea.
    groq_model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._/:-]{1,100}$")
    # Sin valor: el parámetro no se envía y el modelo usa su comportamiento por defecto.
    groq_reasoning_effort: EsfuerzoRazonamiento | None = None
    # Segundos por llamada. El tope mantiene el peor caso de un lead (3 llamadas
    # más las esperas) por debajo del umbral para darlo por trabado.
    groq_timeout: float = Field(default=20, gt=0, le=120)
    # Llamadas por minuto como máximo. El free tier de gpt-oss-20b da unos 8.000
    # tokens por minuto y, medido en una prueba real, cada correo gasta unos 700
    # (unos 567 de prompt y 130 de respuesta): caben ~11, dejamos margen.
    groq_rpm: int = Field(default=10, ge=1, le=1000)

    @field_validator("groq_api_key", "groq_model", "groq_reasoning_effort", mode="before")
    @classmethod
    def _vacio_es_ausente(cls, valor: object) -> object:
        # compose pasa "" cuando la variable no está en .env; un espacio o salto de
        # línea pegado por error tampoco debe contar como valor.
        if isinstance(valor, str):
            valor = valor.strip()
            return valor or None
        return valor

    @field_validator("groq_reasoning_effort", mode="before")
    @classmethod
    def _esfuerzo_en_minusculas(cls, valor: object) -> object:
        return valor.lower() if isinstance(valor, str) else valor

    @model_validator(mode="after")
    def _pool_coherente(self) -> "Settings":
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("DB_POOL_MIN no puede ser mayor que DB_POOL_MAX")
        return self

    @model_validator(mode="after")
    def _worker_configurado(self) -> "Settings":
        if self.worker_activo:
            faltan = [
                nombre
                for nombre, valor in (("GROQ_API_KEY", self.groq_api_key), ("GROQ_MODEL", self.groq_model))
                if valor is None
            ]
            if faltan:
                raise ValueError(f"con WORKER_ACTIVO=true faltan {', '.join(faltan)}")
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
