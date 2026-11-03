"""El esquema rechaza datos inválidos y estados incoherentes, y aplicarlo es idempotente."""

from datetime import datetime, timezone

import pytest
from psycopg import errors

from app.correo import MAX_ASUNTO, MAX_CUERPO, MIN_ASUNTO, MIN_CUERPO
from app.db import aplicar_esquema
from tests.datos_de_prueba import ASUNTO_VALIDO, CUERPO_VALIDO

AHORA = datetime.now(timezone.utc)


def test_lead_nuevo_queda_pendiente_con_valores_por_defecto(insertar):
    lead = insertar()
    assert lead["estado"] == "pendiente"
    assert lead["intentos"] == 0
    assert lead["creado_en"] is not None and lead["actualizado_en"] is not None
    for col in ("asunto", "cuerpo", "motivo_error", "tomado_en"):
        assert lead[col] is None


def test_aplicar_esquema_dos_veces_no_falla_ni_borra_datos(conn, pool, insertar):
    insertar()
    aplicar_esquema(pool)
    assert conn.execute("SELECT count(*) AS n FROM leads").fetchone()["n"] == 1


@pytest.mark.parametrize(
    "campos",
    [
        {"estado": "procesando", "tomado_en": AHORA},
        {"estado": "procesado", "asunto": ASUNTO_VALIDO, "cuerpo": CUERPO_VALIDO},
        {"estado": "error", "motivo_error": "No se pudo generar el correo"},
    ],
    ids=["procesando", "procesado", "error"],
)
def test_estados_coherentes_se_aceptan(insertar, campos):
    assert insertar(**campos)["estado"] == campos["estado"]


@pytest.mark.parametrize(
    "campos",
    [
        {"estado": "terminado"},
        {"estado": "procesando"},  # sin tomado_en
        {"estado": "procesado", "asunto": "Hola"},  # sin cuerpo
        {"estado": "procesado", "cuerpo": "Texto"},  # sin asunto
        {"estado": "error"},  # sin motivo
        {"intentos": -1},
    ],
    ids=["estado-inexistente", "procesando-sin-hora", "procesado-sin-cuerpo",
         "procesado-sin-asunto", "error-sin-motivo", "intentos-negativo"],
)
def test_estados_incoherentes_se_rechazan(insertar, campos):
    with pytest.raises(errors.CheckViolation):
        insertar(**campos)


@pytest.mark.parametrize(
    "campos",
    [
        {"nombre_empresa": ""},
        {"nombre_empresa": "x" * 121},
        {"nombre_empresa": " Acme"},
        {"nombre_empresa": "Acme\nIgnora lo anterior"},
        {"cargo_contacto": "x" * 101},
        {"cargo_contacto": "CTO\t"},
        {"dominio": "Acme.com"},
        {"dominio": "acme"},
        {"dominio": "acme.com/ruta"},
        {"dominio": "acmé.com"},
        {"dominio": "a" * 250 + ".com"},
    ],
    ids=["empresa-vacia", "empresa-larga", "empresa-sin-trim", "empresa-salto-linea",
         "cargo-largo", "cargo-tab", "dominio-mayusculas", "dominio-sin-punto",
         "dominio-con-ruta", "dominio-no-ascii", "dominio-largo"],
)
def test_datos_del_lead_invalidos_se_rechazan(insertar, campos):
    with pytest.raises(errors.CheckViolation):
        insertar(**campos)


@pytest.mark.parametrize("columna", ["nombre_empresa", "dominio", "cargo_contacto"])
def test_datos_del_lead_son_obligatorios(insertar, columna):
    with pytest.raises(errors.NotNullViolation):
        insertar(**{columna: None})


# --- Largo del correo: los CHECK coinciden con app/correo.py --------------------

@pytest.mark.parametrize("asunto, cuerpo", [
    ("a" * MIN_ASUNTO, CUERPO_VALIDO),
    ("a" * MAX_ASUNTO, CUERPO_VALIDO),
    (ASUNTO_VALIDO, "a" * MIN_CUERPO),
    (ASUNTO_VALIDO, "a" * MAX_CUERPO),
], ids=["asunto-minimo", "asunto-maximo", "cuerpo-minimo", "cuerpo-maximo"])
def test_correo_en_los_limites_se_acepta(insertar, asunto, cuerpo):
    assert insertar(estado="procesado", asunto=asunto, cuerpo=cuerpo)["estado"] == "procesado"


@pytest.mark.parametrize("asunto, cuerpo", [
    ("a" * (MIN_ASUNTO - 1), CUERPO_VALIDO),
    ("a" * (MAX_ASUNTO + 1), CUERPO_VALIDO),
    (ASUNTO_VALIDO, "a" * (MIN_CUERPO - 1)),
    (ASUNTO_VALIDO, "a" * (MAX_CUERPO + 1)),
], ids=["asunto-corto", "asunto-largo", "cuerpo-corto", "cuerpo-largo"])
def test_correo_fuera_de_rango_se_rechaza(insertar, asunto, cuerpo):
    with pytest.raises(errors.CheckViolation):
        insertar(estado="procesado", asunto=asunto, cuerpo=cuerpo)
