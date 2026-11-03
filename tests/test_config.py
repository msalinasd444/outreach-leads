"""Config del worker: qué es obligatorio, cómo se leen los vacíos y que los errores no muestren valores."""

import pytest

from app.config import get_settings

CLAVE = "gsk_clave_de_prueba_123"
URL_BD = "postgresql://usuario:clave-de-la-bd@db:5432/outreach"


@pytest.fixture
def entorno(monkeypatch):
    """Entorno mínimo válido con el worker activo; cada test cambia lo que necesita."""
    monkeypatch.setenv("DATABASE_URL", URL_BD)
    monkeypatch.setenv("GROQ_API_KEY", CLAVE)
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def error_de_config() -> str:
    with pytest.raises(RuntimeError) as exc:
        get_settings()
    mensaje = str(exc.value)
    # Ningún mensaje de error de config muestra secretos.
    assert CLAVE not in mensaje
    assert "clave-de-la-bd" not in mensaje
    return mensaje


def test_valores_por_defecto(entorno):
    s = get_settings()
    assert s.worker_activo is True
    assert s.groq_api_key.get_secret_value() == CLAVE
    assert s.groq_model == "openai/gpt-oss-20b"
    assert s.groq_reasoning_effort is None
    assert s.groq_timeout == 20
    assert s.groq_rpm == 15


def test_con_worker_apagado_no_hace_falta_groq(entorno):
    entorno.setenv("WORKER_ACTIVO", "false")
    entorno.delenv("GROQ_API_KEY")
    entorno.delenv("GROQ_MODEL")
    s = get_settings()
    assert s.worker_activo is False
    assert s.groq_api_key is None


@pytest.mark.parametrize("faltante", ["GROQ_API_KEY", "GROQ_MODEL"])
def test_con_worker_activo_falta_una_variable_obligatoria(entorno, faltante):
    entorno.delenv(faltante)
    mensaje = error_de_config()
    assert f"faltan {faltante}" in mensaje


def test_faltan_las_dos_y_se_nombran_ambas(entorno):
    entorno.delenv("GROQ_API_KEY")
    entorno.delenv("GROQ_MODEL")
    assert "faltan GROQ_API_KEY, GROQ_MODEL" in error_de_config()


@pytest.mark.parametrize("vacio", ["", "   ", "\n"])
def test_vacio_o_solo_espacios_cuenta_como_ausente(entorno, vacio):
    # Así llega una variable que compose pasa como ${GROQ_API_KEY:-} sin valor en .env.
    entorno.setenv("GROQ_API_KEY", vacio)
    assert "faltan GROQ_API_KEY" in error_de_config()


def test_la_clave_se_recorta_y_no_aparece_en_repr(entorno):
    entorno.setenv("GROQ_API_KEY", f"  {CLAVE}\n")
    s = get_settings()
    assert s.groq_api_key.get_secret_value() == CLAVE
    assert CLAVE not in repr(s)
    assert CLAVE not in str(s)


@pytest.mark.parametrize("valor, esperado", [
    ("", None),
    ("  ", None),
    ("low", "low"),
    ("LOW", "low"),
    (" medium ", "medium"),
    ("high", "high"),
])
def test_reasoning_effort(entorno, valor, esperado):
    entorno.setenv("GROQ_REASONING_EFFORT", valor)
    assert get_settings().groq_reasoning_effort == esperado


# "none" y "default" los acepta el SDK, pero son de Qwen: con gpt-oss darían 400 en cada lead.
@pytest.mark.parametrize("valor", ["none", "default", "turbo"])
def test_reasoning_effort_invalido_se_nombra_sin_repetir_el_valor(entorno, valor):
    entorno.setenv("GROQ_REASONING_EFFORT", valor)
    mensaje = error_de_config()
    assert "GROQ_REASONING_EFFORT" in mensaje
    assert valor not in mensaje.removeprefix("Configuración inválida o incompleta: ")


@pytest.mark.parametrize("modelo", [
    "openai/gpt oss",        # espacio
    "modelo\nINFO falso",    # salto de línea: falsearía una línea de log
    "x" * 101,
])
def test_modelo_con_caracteres_no_permitidos(entorno, modelo):
    entorno.setenv("GROQ_MODEL", modelo)
    mensaje = error_de_config()
    assert "GROQ_MODEL" in mensaje
    assert "INFO falso" not in mensaje


@pytest.mark.parametrize("variable, valor", [
    ("GROQ_TIMEOUT", "0"),
    ("GROQ_TIMEOUT", "121"),
    ("GROQ_TIMEOUT", "abc"),
    ("GROQ_RPM", "0"),
    ("GROQ_RPM", "1001"),
    ("WORKER_ACTIVO", "quizás"),
])
def test_valores_fuera_de_rango(entorno, variable, valor):
    entorno.setenv(variable, valor)
    mensaje = error_de_config()
    assert variable in mensaje
    assert valor not in mensaje.removeprefix("Configuración inválida o incompleta: ")


def test_un_error_ajeno_no_muestra_la_clave(entorno):
    # La clave es válida, pero otro campo falla: el mensaje no debe arrastrarla.
    entorno.setenv("DB_POOL_MIN", "nada")
    assert "DB_POOL_MIN" in error_de_config()
