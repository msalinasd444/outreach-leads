"""Normalización y validación de los datos de un lead.

Funciones puras, sin FastAPI ni base de datos: se testean solas y el worker puede
contar con que lo que sale de aquí no trae saltos de línea, invisibles ni etiquetas.

Los mensajes de error son fijos y nunca incluyen el valor recibido, porque la API
los devuelve tal cual al cliente (ver errores.py).
"""

import re
import unicodedata

# Deben coincidir con los CHECK de migraciones/esquema.sql.
MAX_NOMBRE_EMPRESA = 120
MAX_CARGO_CONTACTO = 100
MAX_DOMINIO = 253

# Tope sobre el texto crudo, antes de normalizar: no gastamos CPU en NFKC sobre
# entradas enormes. Es holgado para no castigar espacios que luego se colapsan.
_FACTOR_TOPE_CRUDO = 4

# Categorías Unicode rechazadas: controles como \n o \t (Cc); formato e invisibles,
# como los de dirección (bidi) o ancho cero (Cf); surrogates sueltos (Cs); uso
# privado (Co); sin asignar (Cn), y separadores de línea y de párrafo (Zl, Zp).
_CATEGORIAS_PROHIBIDAS = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}

_ETIQUETA_DOMINIO = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# TLD: al menos 2 caracteres y empieza con letra (así una IP como 10.0.0.1 no pasa).
_TLD = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])")


class ErrorDeValidacion(ValueError):
    """Error con un mensaje fijo, apto para mostrarse al cliente."""


def _normalizar_base(valor: str, max_largo: int) -> str:
    """Pasos comunes a los tres campos: NFKC, sin caracteres prohibidos, sin espacios a los extremos."""
    if len(valor) > max_largo * _FACTOR_TOPE_CRUDO:
        raise ErrorDeValidacion(f"supera el largo máximo de {max_largo} caracteres")
    # NFKC unifica formas equivalentes y convierte las variantes de ancho completo
    # (＜, ＠, Ａ) a su forma ASCII, para que no sirvan de disfraz.
    texto = unicodedata.normalize("NFKC", valor)
    if any(unicodedata.category(c) in _CATEGORIAS_PROHIBIDAS for c in texto):
        raise ErrorDeValidacion("contiene caracteres no permitidos")
    texto = texto.strip()
    if not texto:
        raise ErrorDeValidacion("es obligatorio")
    return texto


def _normalizar_texto(valor: str, max_largo: int) -> str:
    texto = " ".join(_normalizar_base(valor, max_largo).split())
    if len(texto) > max_largo:
        raise ErrorDeValidacion(f"supera el largo máximo de {max_largo} caracteres")
    if not any(c.isalnum() for c in texto):
        raise ErrorDeValidacion("debe contener al menos una letra o un número")
    # Ningún nombre de empresa ni cargo los lleva, y el correo final no puede
    # contener enlaces ni direcciones: mejor que no entren.
    if "@" in texto or "://" in texto:
        raise ErrorDeValidacion("no debe contener direcciones de correo ni enlaces")
    # Los datos irán delimitados dentro del prompt: sin < ni >, un
    # dato no puede abrir ni cerrar una etiqueta.
    if "<" in texto or ">" in texto:
        raise ErrorDeValidacion("no debe contener los caracteres < ni >")
    return texto


def normalizar_nombre_empresa(valor: str) -> str:
    return _normalizar_texto(valor, MAX_NOMBRE_EMPRESA)


def normalizar_cargo_contacto(valor: str) -> str:
    return _normalizar_texto(valor, MAX_CARGO_CONTACTO)


def normalizar_dominio(valor: str) -> str:
    texto = _normalizar_base(valor, MAX_DOMINIO)
    if not texto.isascii():
        raise ErrorDeValidacion("debe usar solo caracteres ASCII")
    texto = texto.lower()

    # Lo que toleramos: esquema http(s), "/" final, "." final y "www." inicial.
    for esquema in ("https://", "http://"):
        if texto.startswith(esquema):
            texto = texto[len(esquema):]
            break
    texto = texto.removesuffix("/").removesuffix(".")
    if texto.startswith("www.") and "." in texto[4:]:  # "www.com" se queda como está
        texto = texto[4:]

    # Lo que rechazamos sin intentar adivinar: rutas, puertos, parámetros, usuario.
    if any(c in texto for c in "/?#@:"):
        raise ErrorDeValidacion("debe ser solo el dominio, sin ruta, puerto ni parámetros")

    etiquetas = texto.split(".")
    if (
        len(texto) > MAX_DOMINIO
        or len(etiquetas) < 2
        or not all(_ETIQUETA_DOMINIO.fullmatch(e) for e in etiquetas[:-1])
        or not _TLD.fullmatch(etiquetas[-1])
    ):
        raise ErrorDeValidacion("no tiene un formato de dominio válido")
    return texto
