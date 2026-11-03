"""Validación y normalización del correo que devuelve el modelo.

Funciones puras, sin LLM ni base de datos. validar_correo recibe el texto crudo
de la respuesta y devuelve el correo listo para guardar, o lanza CorreoInvalido
con el código de la regla que falló. Ese código va a los logs y a la nota del
reintento; el contenido generado nunca sale de aquí.

El nombre de la empresa del lead nunca es motivo de rechazo: la API acepta
nombres como "Amazon.com" o "100% Natural", y el correo puede nombrarlos.
"""

import json
import re
import unicodedata
from dataclasses import dataclass

# Deben coincidir con los CHECK de migraciones/esquema.sql.
MIN_ASUNTO, MAX_ASUNTO = 5, 90
MIN_CUERPO, MAX_CUERPO = 80, 1000
# Tope sobre la respuesta cruda: no parseamos ni recorremos con regex algo enorme.
MAX_RESPUESTA = 8000

# Código de cada regla y una descripción fija, escrita por nosotros, para la nota
# del reintento. Ninguna incluye contenido generado.
REGLAS = {
    "respuesta_larga": "la respuesta es demasiado larga; responde solo con el objeto JSON",
    "json": "la respuesta debe ser solo un objeto JSON válido",
    "estructura": 'el JSON debe tener exactamente las claves "asunto" y "cuerpo", ambas con texto',
    "asunto_multilinea": "el asunto debe ser una sola línea",
    "caracteres": "no uses tabulaciones ni caracteres invisibles o de control",
    "largo_asunto": f"el asunto debe tener entre {MIN_ASUNTO} y {MAX_ASUNTO} caracteres",
    "largo_cuerpo": f"el cuerpo debe tener entre {MIN_CUERPO} y {MAX_CUERPO} caracteres",
    "barra_invertida": "no escribas barras invertidas ni secuencias como \\n; usa saltos de línea reales",
    "marcador": "no dejes marcadores por completar como [Nombre], {campo}, ___ o XXX",
    "correo": "no incluyas direcciones de correo",
    "enlace": "no incluyas enlaces, direcciones web ni dominios",
    "telefono": "no incluyas números de teléfono",
    "promesa": "no incluyas porcentajes, precios, montos ni garantías",
    "mayusculas": "no escribas el texto en mayúsculas",
}


class CorreoInvalido(ValueError):
    """El mensaje es solo el código de la regla: se puede registrar sin filtrar contenido."""

    def __init__(self, regla: str) -> None:
        assert regla in REGLAS, regla
        super().__init__(regla)
        self.regla = regla


@dataclass(frozen=True)
class CorreoGenerado:
    asunto: str
    cuerpo: str


# Mismas categorías que rechaza normalizacion.py; en el cuerpo se permite "\n".
_CATEGORIAS_PROHIBIDAS = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}

# TLD comunes. Con una lista, "Node.js" o "v2.0" no parecen dominios; el costo es
# que un dominio con un TLD poco común no se detecta (los esquemas y "www." sí).
_TLDS = (
    "com", "net", "org", "info", "biz", "io", "co", "ai", "app", "dev", "me", "tv",
    "xyz", "online", "site", "tech", "store", "shop", "cloud", "eu", "us", "uk",
    "de", "fr", "it", "pt", "es", "mx", "ar", "cl", "pe", "uy", "py", "bo", "ec",
    "ve", "br", "cr", "gt", "pa", "do", "la",
)
_ESQUEMA_O_WWW = re.compile(r"://|\bwww\.", re.IGNORECASE)
# Una o más etiquetas seguidas de un TLD de la lista, sin pegarse a otra palabra.
# El lookbehind excluye "@" para que una dirección cuente como correo, no como dominio.
_DOMINIO = re.compile(
    r"(?<![\w@.-])(?:[^\W_](?:[\w-]*[^\W_])?\.)+(?:" + "|".join(_TLDS) + r")(?![\w-])",
    re.IGNORECASE,
)
# Exige punto después de la "@": "Estimad@s" o "Querid@s" no son direcciones.
_CORREO = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Dígitos con separadores de teléfono; luego se cuentan los dígitos.
_CANDIDATO_TELEFONO = re.compile(r"[+(]?\d[\d ().-]*\d")
_MIN_DIGITOS_TELEFONO = 8
# "2025-2026" tiene 8 dígitos, pero es un rango de años.
_RANGO_DE_ANIOS = re.compile(r"\b(?:19|20)\d{2} ?[-–] ?(?:19|20)\d{2}\b")
_MARCADOR = re.compile(r"[\[\]{}<>]|_{2,}|\bx{3,}\b", re.IGNORECASE)
_PROMESA = re.compile(r"[%$€£¥]|\bgarantiz", re.IGNORECASE)
_PROPORCION_MAX_MAYUSCULAS = 0.5
# Con pocas letras la proporción no dice nada: "IBM y B2B" es casi todo mayúsculas.
_MIN_LETRAS_MAYUSCULAS_ASUNTO = 10
_MIN_LETRAS_MAYUSCULAS_CUERPO = 20


def validar_correo(contenido: str | None, *, nombre_empresa: str) -> CorreoGenerado:
    """Valida la respuesta cruda del modelo. nombre_empresa es el del lead, ya normalizado."""
    if contenido is None or not contenido.strip():
        raise CorreoInvalido("json")
    if len(contenido) > MAX_RESPUESTA:
        raise CorreoInvalido("respuesta_larga")
    try:
        datos = json.loads(contenido)
    except (ValueError, RecursionError):
        raise CorreoInvalido("json") from None
    if (
        type(datos) is not dict
        or set(datos) != {"asunto", "cuerpo"}
        or not all(isinstance(v, str) for v in datos.values())
    ):
        raise CorreoInvalido("estructura")

    asunto = _normalizar(datos["asunto"], multilinea=False)
    cuerpo = _normalizar(datos["cuerpo"], multilinea=True)
    if not MIN_ASUNTO <= len(asunto) <= MAX_ASUNTO:
        raise CorreoInvalido("largo_asunto")
    if not MIN_CUERPO <= len(cuerpo) <= MAX_CUERPO:
        raise CorreoInvalido("largo_cuerpo")

    _revisar_contenido(asunto, nombre_empresa, _MIN_LETRAS_MAYUSCULAS_ASUNTO)
    _revisar_contenido(cuerpo, nombre_empresa, _MIN_LETRAS_MAYUSCULAS_CUERPO)
    return CorreoGenerado(asunto=asunto, cuerpo=cuerpo)


def _normalizar(valor: str, *, multilinea: bool) -> str:
    # NFKC primero: convierte "＠" o "ｗｗｗ" de ancho completo a ASCII, para que
    # no sirvan de disfraz ante las reglas de abajo.
    texto = unicodedata.normalize("NFKC", valor).replace("\r\n", "\n").replace("\r", "\n")
    if not multilinea and "\n" in texto.strip():
        raise CorreoInvalido("asunto_multilinea")
    if any(unicodedata.category(c) in _CATEGORIAS_PROHIBIDAS for c in texto if c != "\n"):
        raise CorreoInvalido("caracteres")
    # Espacios repetidos a uno, sin espacios en los bordes de cada línea y como
    # mucho una línea en blanco entre párrafos.
    lineas = [" ".join(linea.split()) for linea in texto.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def _revisar_contenido(texto: str, nombre_empresa: str, min_letras_mayusculas: int) -> None:
    texto = _sin_nombre_de_empresa(texto, nombre_empresa)
    if "\\" in texto:
        raise CorreoInvalido("barra_invertida")
    if _MARCADOR.search(texto):
        raise CorreoInvalido("marcador")
    if _CORREO.search(texto):
        raise CorreoInvalido("correo")
    if _ESQUEMA_O_WWW.search(texto) or _DOMINIO.search(texto):
        raise CorreoInvalido("enlace")
    if _hay_telefono(texto):
        raise CorreoInvalido("telefono")
    if _PROMESA.search(texto):
        raise CorreoInvalido("promesa")
    if _mayormente_mayusculas(texto, min_letras_mayusculas):
        raise CorreoInvalido("mayusculas")


def _sin_nombre_de_empresa(texto: str, nombre_empresa: str) -> str:
    """Reemplaza las menciones exactas del nombre (sin distinguir mayúsculas) por una palabra neutra.

    Solo cuenta como mención si no está pegada a algo que la convierta en otra
    cosa: "www.Amazon.com", "ventas@Amazon.com", "Amazon.com/ofertas" o
    "Amazon.com.br" no se reemplazan y las reglas los siguen detectando.
    """
    if not nombre_empresa:
        return texto
    patron = r"(?<![\w.@/-])" + re.escape(nombre_empresa) + r"(?![\w/@-]|\.[^\W_])"
    return re.sub(patron, "Empresa", texto, flags=re.IGNORECASE)


def _hay_telefono(texto: str) -> bool:
    texto = _RANGO_DE_ANIOS.sub("años", texto)
    for candidato in _CANDIDATO_TELEFONO.findall(texto):
        # ". " separa oraciones, no grupos de un número: "En 2026. 1500" no es un teléfono.
        for trozo in candidato.split(". "):
            if sum(c.isdigit() for c in trozo) >= _MIN_DIGITOS_TELEFONO:
                return True
    return False


def _mayormente_mayusculas(texto: str, min_letras: int) -> bool:
    letras = [c for c in texto if c.isalpha()]
    if len(letras) < min_letras:
        return False
    return sum(c.isupper() for c in letras) / len(letras) > _PROPORCION_MAX_MAYUSCULAS
