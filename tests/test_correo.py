"""Validación del correo generado: qué se rechaza, qué texto legítimo pasa y cómo se normaliza."""

import json

import pytest

from app.correo import (
    MAX_ASUNTO,
    MAX_CUERPO,
    MAX_RESPUESTA,
    MIN_ASUNTO,
    MIN_CUERPO,
    REGLAS,
    CorreoGenerado,
    CorreoInvalido,
    validar_correo,
)

# Caracteres escritos con chr() para que el archivo no contenga invisibles literales.
BARRA = chr(92)
TAB = chr(9)
NBSP = chr(0xA0)
ANCHO_CERO = chr(0x200B)
BIDI = chr(0x202E)
ARROBA_ANCHA = chr(0xFF20)

ASUNTO_OK = "Una idea para su equipo comercial"
CUERPO_OK = (
    "Hola:\n\n"
    "Les escribo porque trabajo con equipos comerciales que buscan ordenar el "
    "seguimiento de sus clientes sin sumar herramientas complejas.\n\n"
    "¿Les parece si conversamos 15 minutos la próxima semana para ver si tiene "
    "sentido para ustedes?\n\n"
    "Saludos cordiales"
)


def respuesta(asunto=ASUNTO_OK, cuerpo=CUERPO_OK, **extra) -> str:
    return json.dumps({"asunto": asunto, "cuerpo": cuerpo, **extra}, ensure_ascii=False)


def validar(asunto=ASUNTO_OK, cuerpo=CUERPO_OK, empresa="Acme") -> CorreoGenerado:
    return validar_correo(respuesta(asunto, cuerpo), nombre_empresa=empresa)


def regla_que_falla(contenido, empresa="Acme") -> str:
    with pytest.raises(CorreoInvalido) as exc:
        validar_correo(contenido, nombre_empresa=empresa)
    return exc.value.regla


def test_correo_valido_se_devuelve_tal_cual():
    assert validar() == CorreoGenerado(asunto=ASUNTO_OK, cuerpo=CUERPO_OK)


# --- Falsos positivos: texto legítimo que debe pasar ---------------------------

@pytest.mark.parametrize("empresa, frase", [
    ("Acme", "Estimad@s:"),
    ("Acme", "Querid@s equipo de Acme:"),
    ("Acme", "Vi que su equipo de desarrollo trabaja con Node.js."),
    ("Acme", "Trabajo con empresas S.A. y SpA de distintos tamaños."),
    ("Acme S.A.", "Me dirijo al área comercial de Acme S.A. con una idea."),
    ("Acme", "Revisarlo toma unos 3,5 minutos."),
    ("Acme", "Trabajo con equipos de ventas B2B."),
    ("Acme", "Sé que su equipo atiende clientes 24/7."),
    ("Acme", "Muchos equipos usan herramientas de IBM."),
    ("Acme", "Pensando en sus planes para 2026."),
    ("Acme", "Pensando en el periodo 2025-2026."),
    ("Acme", "Por ejemplo, p. ej. su CRM, ERP o SaaS, etc."),
    ("Acme", "Es la versión v2.0 de nuestro método."),
    ("Acme", "¿Podríamos hablar? Les saluda alguien de Ñuñoa: áéíóú, ü, ¡gracias!"),
    # Ajuste A: el nombre de la empresa del lead nunca es motivo de rechazo.
    ("Amazon.com", "Me gustaría conversar con el equipo de Amazon.com."),
    ("Amazon.com", "Gracias por su tiempo, equipo de Amazon.com"),
    ("Amazon.com", "Sé que en AMAZON.COM valoran el tiempo de su equipo."),
    ("Amazon.com", "En amazon.com, ¿quién ve los temas comerciales?"),
    ("100% Natural", "Me gustaría presentarme al equipo de 100% Natural."),
    ("C++ Labs", "Un saludo al equipo de C++ Labs."),
    ("IBM", "Le escribo al equipo de IBM por una idea concreta."),
])
def test_texto_legitimo_pasa(empresa, frase):
    cuerpo = f"{frase}\n\n{CUERPO_OK}"
    assert validar(cuerpo=cuerpo, empresa=empresa).cuerpo == cuerpo


@pytest.mark.parametrize("empresa, asunto", [
    ("Amazon.com", "Una idea para Amazon.com"),
    ("Acme", "Propuesta para IBM y B2B"),
    ("Acme", "Su atención 24/7 en 2026"),
    ("Acme", "Sobre su CRM y ERP"),  # siglas: 7 de 14 letras, no es "mayormente mayúsculas"
    ("Acme", "IBM y CRM"),  # menos de 10 letras: la proporción no se evalúa
])
def test_asunto_legitimo_pasa(empresa, asunto):
    assert validar(asunto=asunto, empresa=empresa).asunto == asunto


# --- Reglas de contenido ------------------------------------------------------

@pytest.mark.parametrize("empresa, frase, regla", [
    # Enlaces y dominios
    ("Acme", "Más información en https://acme.com", "enlace"),
    ("Acme", "Más información en ftp://archivos", "enlace"),
    ("Acme", "Más información en www.acme.com", "enlace"),
    ("Acme", "Más información en acme.com", "enlace"),
    ("Acme", "Más información en ACME.IO.", "enlace"),
    ("Acme", "Escríbanos desde ventas.acme.cl", "enlace"),
    # El nombre se ignora solo como mención exacta, no dentro de un enlace.
    ("Amazon.com", "Vean amazon.com/ofertas", "enlace"),
    ("Amazon.com", "Vean www.Amazon.com", "enlace"),
    ("Amazon.com", "Vean Amazon.com.br", "enlace"),
    ("Acme", "Saludos a Acme, visiten acme.com", "enlace"),
    # Correos
    ("Acme", "Escríbanme a ventas@acme.com", "correo"),
    ("Amazon.com", "Escríbanme a contacto@Amazon.com", "correo"),
    ("Acme", f"Escríbanme a ventas{ARROBA_ANCHA}acme.com", "correo"),
    # Teléfonos
    ("Acme", "Llámenme al +56 9 1234 5678", "telefono"),
    ("Acme", "Llámenme al (02) 2345-6789", "telefono"),
    ("Acme", "Llámenme al 800 123 4567", "telefono"),
    ("Acme", "Llámenme al +1 555 1234", "telefono"),
    ("Acme", "Llámenme al 2.345.6789", "telefono"),
    # Marcadores por completar
    ("Acme", "Hola [Nombre]:", "marcador"),
    ("Acme", "Hola {{nombre}}:", "marcador"),
    ("Acme", "Hola <nombre>:", "marcador"),
    ("Acme", "Firma: ___", "marcador"),
    ("Acme", "Saludos, XXX", "marcador"),
    ("Acme", "Saludos, xxxx", "marcador"),
    # Datos inventados o promesas
    ("Acme", "Mejoramos sus ventas un 30%.", "promesa"),
    ("Acme", "Desde US$ 500 al mes.", "promesa"),
    ("Acme", "Desde 200 € al mes.", "promesa"),
    ("Acme", "Garantizamos resultados.", "promesa"),
    ("Acme", "Resultados garantizados desde el primer día.", "promesa"),
    ("100% Natural", "Al equipo de 100% Natural: crecen un 40% con nosotros.", "promesa"),
    # Secuencias de escape literales
    ("Acme", f"Hola{BARRA}nque tal", "barra_invertida"),
    ("Acme", f"Saludos{BARRA}t", "barra_invertida"),
])
def test_regla_de_contenido_en_el_cuerpo(empresa, frase, regla):
    cuerpo = f"{frase}\n\n{CUERPO_OK}"
    assert regla_que_falla(respuesta(cuerpo=cuerpo), empresa) == regla


@pytest.mark.parametrize("asunto, regla", [
    ("Visiten acme.com hoy", "enlace"),
    ("Hola [Nombre], una idea", "marcador"),
    ("Llámenos al 800 123 4567", "telefono"),
    ("Un 30% más de ventas", "promesa"),
    ("OFERTA EXCLUSIVA PARA USTEDES", "mayusculas"),
])
def test_regla_de_contenido_en_el_asunto(asunto, regla):
    assert regla_que_falla(respuesta(asunto=asunto)) == regla


def test_cuerpo_mayormente_en_mayusculas():
    assert regla_que_falla(respuesta(cuerpo=CUERPO_OK.upper())) == "mayusculas"


def test_el_nombre_en_mayusculas_no_vuelve_mayusculas_al_cuerpo():
    empresa = "ACME CORPORACION INTERNACIONAL DE SERVICIOS"
    # Sin descontar el nombre, más de la mitad de las letras serían mayúsculas.
    cuerpo = f"Hola, equipo de {empresa}:\n\nUna idea breve para {empresa}.\n\nSaludos"
    letras = [c for c in cuerpo if c.isalpha()]
    assert sum(c.isupper() for c in letras) / len(letras) > 0.5
    assert validar(cuerpo=cuerpo, empresa=empresa).cuerpo == cuerpo


# --- Formato de la respuesta --------------------------------------------------

@pytest.mark.parametrize("contenido, regla", [
    (None, "json"),
    ("", "json"),
    ("   ", "json"),
    ("no es json", "json"),
    ("```json\n" + respuesta() + "\n```", "json"),
    ("[" * 100_000, "respuesta_larga"),
    ("[]", "estructura"),
    ('"texto"', "estructura"),
    (json.dumps({"asunto": ASUNTO_OK}), "estructura"),
    (respuesta(firma="Ana"), "estructura"),
    (json.dumps({"asunto": ASUNTO_OK, "cuerpo": 123}), "estructura"),
    (json.dumps({"asunto": None, "cuerpo": CUERPO_OK}), "estructura"),
])
def test_respuesta_mal_formada(contenido, regla):
    assert regla_que_falla(contenido) == regla


def test_respuesta_justo_bajo_el_tope_se_procesa():
    contenido = respuesta() + " " * (MAX_RESPUESTA - len(respuesta()))
    assert len(contenido) == MAX_RESPUESTA
    assert validar_correo(contenido, nombre_empresa="Acme").asunto == ASUNTO_OK


@pytest.mark.parametrize("asunto, cuerpo, regla", [
    ("a" * (MIN_ASUNTO - 1), CUERPO_OK, "largo_asunto"),
    ("a" * (MAX_ASUNTO + 1), CUERPO_OK, "largo_asunto"),
    (ASUNTO_OK, "palabra " * 9, "largo_cuerpo"),  # 71 caracteres tras recortar
    (ASUNTO_OK, "palabra " * 126, "largo_cuerpo"),  # 1007 caracteres
    ("   Hola   ", CUERPO_OK, "largo_asunto"),  # se mide después de normalizar
])
def test_largos_fuera_de_rango(asunto, cuerpo, regla):
    assert regla_que_falla(respuesta(asunto, cuerpo)) == regla


def test_largos_en_el_limite_pasan():
    asunto = "a" * MAX_ASUNTO
    cuerpo = ("palabra " * 200)[:MAX_CUERPO - 1] + "."
    assert len(cuerpo) == MAX_CUERPO
    correo = validar(asunto=asunto, cuerpo=cuerpo)
    assert (len(correo.asunto), len(correo.cuerpo)) == (MAX_ASUNTO, MAX_CUERPO)
    minimo = cuerpo[:MIN_CUERPO - 1] + "."
    assert len(validar(asunto="a" * MIN_ASUNTO, cuerpo=minimo).cuerpo) == MIN_CUERPO


def test_asunto_en_varias_lineas():
    assert regla_que_falla(respuesta(asunto="Una idea\npara ustedes")) == "asunto_multilinea"


@pytest.mark.parametrize("caracter", [TAB, ANCHO_CERO, BIDI, chr(0), chr(0x7F), chr(0x2028)])
def test_caracteres_prohibidos(caracter):
    cuerpo = CUERPO_OK.replace("Hola:", f"Hola{caracter}:")
    assert regla_que_falla(respuesta(cuerpo=cuerpo)) == "caracteres"


# --- Normalización ------------------------------------------------------------

def test_normaliza_espacios_saltos_y_ancho_completo():
    asunto = f"  Una  idea{NBSP}para   Ａｃｍｅ  "
    cuerpo = (
        "Hola:  \r\n\r\n\r\n\r\n"
        "Les escribo   porque trabajo con equipos comerciales que buscan ordenar el\r"
        "seguimiento de sus clientes.   \n\n\n"
        "Saludos cordiales   "
    )
    correo = validar(asunto=asunto, cuerpo=cuerpo)
    assert correo.asunto == "Una idea para Acme"
    assert correo.cuerpo == (
        "Hola:\n\n"
        "Les escribo porque trabajo con equipos comerciales que buscan ordenar el\n"
        "seguimiento de sus clientes.\n\n"
        "Saludos cordiales"
    )


def test_validar_dos_veces_da_lo_mismo():
    primero = validar(asunto=f" {ASUNTO_OK} ", cuerpo=CUERPO_OK.replace("\n\n", "\n\n\n\n"))
    segundo = validar(asunto=primero.asunto, cuerpo=primero.cuerpo)
    assert primero == segundo


# --- El error no filtra contenido ---------------------------------------------

def test_el_error_solo_lleva_el_codigo_de_la_regla():
    with pytest.raises(CorreoInvalido) as exc:
        validar(cuerpo=f"Datos privados: ventas@acme.com\n\n{CUERPO_OK}")
    assert str(exc.value) == "correo"
    assert exc.value.args == ("correo",)


def test_cada_regla_tiene_una_descripcion_fija():
    for codigo, descripcion in REGLAS.items():
        assert codigo.isidentifier()
        assert descripcion and "{" not in descripcion.replace("{campo}", "")
