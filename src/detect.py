"""Deteccion pura: normaliza y busca. Sin AWS, sin I/O, sin logging.

Implementa las capas 0, 3 y 4 del SPEC §7. Las capas 1, 2 y 5 son estructurales
y viven en sanitize.py, porque necesitan el objeto y no solo el texto.

Las funciones devuelven NOMBRES DE REGLA, nunca el valor detectado. No es una
convencion que haya que recordar: es la firma. Un valor que no se devuelve no
puede acabar en un log, en una metrica ni en un mensaje de excepcion.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Dict, List, Tuple

# --- Capa 0 — normalizacion -------------------------------------------------

#: invisibles que separan digitos sin dejar rastro visible
_ZERO_WIDTH = dict.fromkeys([
    0x00AD,  # soft hyphen
    0x200B, 0x200C, 0x200D,  # zero width space / non-joiner / joiner
    0x2060,  # word joiner
    0xFEFF,  # BOM
])


def normalize(s: str) -> str:
    """NFKC y fuera los de ancho cero.

    NFKC convierte los digitos de ancho completo (４１１１…) a ASCII; el fixture
    poisoned_pan_fullwidth.jsonl verifica justo eso.
    """
    if not s:
        return s
    return unicodedata.normalize("NFKC", s).translate(_ZERO_WIDTH)


# --- Capa 3 — PAN -----------------------------------------------------------

#: Candidatos. La COMA NO ES SEPARADOR: en este corpus separa miles, y
#: admitirla convierte "120,000,000,000" en un candidato. La variante permisiva
#: se probo contra el corpus real y producia 2 candidatos que fallaban ambas
#: puertas; esta produce 0. Se usa la restrictiva.
_PAN = re.compile(r"""
    (?<![0-9])(?:
        [0-9]{13,19}                                    # contiguo
      | [0-9]{4}[ -][0-9]{4}[ -][0-9]{4}[ -][0-9]{1,4}  # 4-4-4-4
      | 3[47][0-9]{2}[ -][0-9]{6}[ -][0-9]{5}           # amex 4-6-5
    )(?![0-9])
""", re.X)


def luhn_ok(digits: str) -> bool:
    """Checksum de Luhn."""
    total, alt = 0, False
    for char in reversed(digits):
        value = ord(char) - 48
        if not 0 <= value <= 9:
            return False
        if alt:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alt = not alt
    return total % 10 == 0


#: (prefijos exactos, rangos numericos (desde, hasta, cuantos_digitos))
_IIN: Tuple[Tuple[Tuple[str, ...], Tuple[Tuple[int, int, int], ...]], ...] = (
    (("4",), ()),                                    # visa
    (("51", "52", "53", "54", "55"), ((2221, 2720, 4),)),   # mastercard
    (("34", "37"), ()),                              # amex
    (("6011", "65"), ((644, 649, 3), (622, 622, 3))),       # discover
    (("36", "38"), ((300, 305, 3),)),                # diners
    ((), ((3528, 3589, 4),)),                        # jcb
)


def iin_ok(digits: str) -> bool:
    """Segunda puerta: el prefijo tiene que ser de una emisora conocida.

    Luhn solo deja pasar 1 de cada 10 cifras al azar. El prefijo es lo que
    evita los falsos positivos en tablas financieras, que es el riesgo real de
    este corpus.
    """
    for prefijos, rangos in _IIN:
        if any(digits.startswith(p) for p in prefijos):
            return True
        for desde, hasta, n in rangos:
            if len(digits) >= n and desde <= int(digits[:n]) <= hasta:
                return True
    return False


def find_pan(text: str) -> List[str]:
    """Devuelve un 'pan' por cada candidato que pasa las DOS puertas."""
    encontrados = []
    for match in _PAN.finditer(text):
        digits = match.group(0).replace(" ", "").replace("-", "")
        if not 13 <= len(digits) <= 19:
            continue
        if luhn_ok(digits) and iin_ok(digits):
            encontrados.append("pan")
    return encontrados


# --- Capa 4 — SAD -----------------------------------------------------------

_TRACK1 = re.compile(r"%B\d{12,19}\^[^\^]{2,30}\^\d{4}")
_TRACK2 = re.compile(r";?\d{12,19}[=D]\d{4}\d{3}")

#: CVV SOLO contextual. Tres o cuatro digitos sueltos aparecen por todas
#: partes; sin la palabra al lado esto seria un generador de falsos positivos.
_CVV_CTX = re.compile(
    r"(?:cvv2?|cvc2?|cid|csc|c[oó]digo de seguridad|security code)\W{0,10}(\d{3,4})(?!\d)",
    re.IGNORECASE)


def find_sad(text: str) -> List[str]:
    encontrados = []
    if _TRACK1.search(text):
        encontrados.append("sad.track1")
    if _TRACK2.search(text):
        encontrados.append("sad.track2")
    if _CVV_CTX.search(text):
        encontrados.append("sad.cvv")
    return encontrados


# --- Escaneo con dedup ------------------------------------------------------

#: El bloque system pesa 4 KB y es identico en todas las lineas del lote: es el
#: 72% del payload. Sin cache se escanea lo mismo N veces. No es una
#: optimizacion, es la diferencia entre escanear 1x y 9x.
_CACHE: Dict[bytes, List[str]] = {}


def scan_text(text: str) -> List[str]:
    """Capas 3 y 4 sobre un texto ya normalizado, con dedup por hash."""
    if not text:
        return []
    clave = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
    cacheado = _CACHE.get(clave)
    if cacheado is not None:
        return list(cacheado)
    reglas = find_pan(text) + find_sad(text)
    _CACHE[clave] = reglas
    return list(reglas)


def reset_cache() -> None:
    """Para los tests: que un caso no vea el veredicto cacheado de otro."""
    _CACHE.clear()
