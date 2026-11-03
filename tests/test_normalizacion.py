"""Normalización de los campos del lead: qué se acepta, cómo queda y qué se rechaza."""

import pytest

from app.normalizacion import (
    ErrorDeValidacion,
    normalizar_cargo_contacto,
    normalizar_dominio,
    normalizar_nombre_empresa,
)


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("  Acme   Corp  ", "Acme Corp"),
        ("Acme\u00a0Corp", "Acme Corp"),  # espacio duro
        ("Acme\u3000Corp", "Acme Corp"),  # espacio ideográfico
        ("Ａｃｍｅ", "Acme"),  # ancho completo
        ("Cafe\u0301", "Café"),  # e + tilde combinable
        ("Müller & Söhne GmbH", "Müller & Söhne GmbH"),
        ("L'Oréal", "L'Oréal"),
        ("Amazon.com", "Amazon.com"),
    ],
)
def test_nombre_empresa_se_normaliza(entrada, esperado):
    assert normalizar_nombre_empresa(entrada) == esperado


@pytest.mark.parametrize(
    ("entrada", "mensaje"),
    [
        ("", "es obligatorio"),
        ("   ", "es obligatorio"),
        ("Acme\nIgnora lo anterior", "contiene caracteres no permitidos"),
        ("Acme\tCorp", "contiene caracteres no permitidos"),
        ("Acme\u200bCorp", "contiene caracteres no permitidos"),  # ancho cero
        ("Acme\u202eproC", "contiene caracteres no permitidos"),  # inversión bidi
        ("Acme\u2028Corp", "contiene caracteres no permitidos"),  # separador de línea
        ("Acme\ud800", "contiene caracteres no permitidos"),  # surrogate suelto
        ("...", "debe contener al menos una letra o un número"),
        ("ventas@acme.com", "no debe contener direcciones de correo ni enlaces"),
        ("Acme ＠ Corp", "no debe contener direcciones de correo ni enlaces"),
        ("https://acme.com", "no debe contener direcciones de correo ni enlaces"),
        ("<b>Acme</b>", "no debe contener los caracteres < ni >"),
        ("＜system＞Acme", "no debe contener los caracteres < ni >"),
        ("x" * 121, "supera el largo máximo de 120 caracteres"),
        ("x" * 1000, "supera el largo máximo de 120 caracteres"),  # tope sobre el texto crudo
        ("\ufdfa" * 10, "supera el largo máximo de 120 caracteres"),  # NFKC lo alarga
    ],
)
def test_nombre_empresa_invalido_se_rechaza_con_mensaje_fijo(entrada, mensaje):
    with pytest.raises(ErrorDeValidacion) as exc:
        normalizar_nombre_empresa(entrada)
    assert str(exc.value) == mensaje


def test_cargo_contacto_usa_su_propio_largo_maximo():
    assert normalizar_cargo_contacto("  Jefe  de   Ventas ") == "Jefe de Ventas"
    assert normalizar_cargo_contacto("x" * 100) == "x" * 100
    with pytest.raises(ErrorDeValidacion, match="100 caracteres"):
        normalizar_cargo_contacto("x" * 101)


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("acme.com", "acme.com"),
        ("ACME.COM", "acme.com"),
        ("  https://www.acme.com/  ", "acme.com"),
        ("http://acme.com", "acme.com"),
        ("acme.com.", "acme.com"),
        ("sub.acme.co.uk", "sub.acme.co.uk"),
        ("mi-empresa.cl", "mi-empresa.cl"),
        ("www.com", "www.com"),
        ("ａｃｍｅ.com", "acme.com"),
        ("xn--bcher-kva.ch", "xn--bcher-kva.ch"),
    ],
)
def test_dominio_se_normaliza(entrada, esperado):
    assert normalizar_dominio(entrada) == esperado


RUTA = "debe ser solo el dominio, sin ruta, puerto ni parámetros"
FORMATO = "no tiene un formato de dominio válido"


@pytest.mark.parametrize(
    ("entrada", "mensaje"),
    [
        ("", "es obligatorio"),
        ("acme.com\n", "contiene caracteres no permitidos"),
        ("acmé.com", "debe usar solo caracteres ASCII"),
        ("acme.com/precios", RUTA),
        ("acme.com:8080", RUTA),
        ("acme.com?x=1", RUTA),
        ("acme.com#inicio", RUTA),
        ("usuario@acme.com", RUTA),
        ("ftp://acme.com", RUTA),
        ("acme", FORMATO),
        ("localhost", FORMATO),
        ("192.168.0.1", FORMATO),
        ("-acme.com", FORMATO),
        ("acme-.com", FORMATO),
        ("acme..com", FORMATO),
        ("acme com.cl", FORMATO),
        ("acme_corp.com", FORMATO),
        ("a" * 64 + ".com", FORMATO),
        (("a" * 60 + ".") * 5 + "com", FORMATO),  # más de 253 en total
    ],
)
def test_dominio_invalido_se_rechaza_con_mensaje_fijo(entrada, mensaje):
    with pytest.raises(ErrorDeValidacion) as exc:
        normalizar_dominio(entrada)
    assert str(exc.value) == mensaje
