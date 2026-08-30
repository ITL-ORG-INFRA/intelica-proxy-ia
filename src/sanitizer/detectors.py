"""Capas 2 a 5 — deteccion.

Un hallazgo NUNCA lleva el valor que lo provoco. Lleva donde estaba, de que
tipo es y poco mas. Si el hallazgo transportara el PAN, acabaria en un log, en
una metrica o en un mensaje de error, y el sanitizador se convertiria en el
sitio por donde se escapa lo que venia a contener.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Tuple

from normalize import as_text, decode_base64_blobs, sniff_binary


@dataclass
class Finding:
    layer: int
    type: str          # "pan" | "sad_track" | "sad_cvv" | "sad_pin" | "campo" | "binario"
    where: str         # ruta dentro del envelope, p.ej. "requests[3].params.messages[0]"
    detail: str = ""  # marca, longitud, formato — nunca el valor
    hard: bool = False  # aborta el lote entero, no solo la peticion

    def as_dict(self) -> Dict[str, Any]:
        return {"layer": self.layer, "type": self.type, "where": self.where,
                "detail": self.detail, "hard": self.hard}


# ---------------------------------------------------------------------------
# Capa 3 — PAN en texto libre
# ---------------------------------------------------------------------------

#: tirada continua de 13 a 19 digitos
_PAN_CONTIGUOUS = re.compile(r"(?<![\d])(\d{13,19})(?![\d])")

#: grupos separados por espacio o guion: 4-4-4-4 y sus variantes de 13 a 19
_PAN_GROUPED = re.compile(
    r"(?<![\d])(\d{4}[ \-]\d{4}[ \-]\d{4}[ \-]\d{1,7}|\d{4}[ \-]\d{6}[ \-]\d{4,5})(?![\d])"
)


def luhn(digits: str) -> bool:
    """Checksum de Luhn. Es lo que separa un PAN de 16 digitos cualesquiera."""
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


#: (nombre, longitudes validas, prefijos exactos, rangos numericos de prefijo)
_IIN: List[Tuple[str, Tuple[int, ...], Tuple[str, ...], Tuple[Tuple[int, int, int], ...]]] = [
    ("visa",       (13, 16, 19), ("4",), ()),
    ("mastercard", (16,),        ("51", "52", "53", "54", "55"), ((2221, 2720, 4),)),
    ("amex",       (15,),        ("34", "37"), ()),
    ("discover",   (16, 17, 18, 19), ("6011", "65"), ((644, 649, 3), (622126, 622925, 6))),
    ("diners",     (14, 15, 16, 17, 18, 19), ("36", "38", "39"), ((300, 305, 3),)),
    ("jcb",        (16, 17, 18, 19), (), ((3528, 3589, 4),)),
    ("unionpay",   (16, 17, 18, 19), ("62", "81"), ()),
    ("maestro",    (12, 13, 14, 15, 16, 17, 18, 19), ("50", "56", "57", "58", "6759"), ()),
]


def iin_brand(digits: str) -> str:
    """Devuelve la marca si el prefijo y la longitud encajan con una emisora.

    Luhn solo deja pasar 1 de cada 10 numeros al azar; exigir ademas un IIN
    valido descarta casi todo lo que no es una tarjeta (referencias, ids,
    numeros de cuenta) sin dejar pasar ninguna de las marcas reales.
    """
    length = len(digits)
    for brand, longitudes, prefixes, rangos in _IIN:
        if length not in longitudes:
            continue
        if any(digits.startswith(p) for p in prefixes):
            return brand
        for desde, hasta, n in rangos:
            if length >= n and desde <= int(digits[:n]) <= hasta:
                return brand
    return ""


def _digits_only(text: str) -> str:
    return text.replace(" ", "").replace("-", "")


def find_pans(text: str, where: str) -> List[Finding]:
    findings: List[Finding] = []
    vistos = set()
    for patron, fmt in ((_PAN_CONTIGUOUS, "contiguo"), (_PAN_GROUPED, "agrupado")):
        for match in patron.finditer(text):
            digits = _digits_only(match.group(1))
            if not 13 <= len(digits) <= 19 or digits in vistos:
                continue
            if not luhn(digits):
                continue
            brand = iin_brand(digits)
            if not brand:
                continue
            vistos.add(digits)
            findings.append(Finding(
                layer=3, type="pan", where=where,
                # Marca y longitud bastan para investigar. El numero, no.
                detail=f"{brand}/{len(digits)}d/{fmt}",
            ))
    return findings


# ---------------------------------------------------------------------------
# Capa 4 — SAD (datos de autenticacion). Nunca son almacenables: bloqueo duro.
# ---------------------------------------------------------------------------

#: banda 1: %B<pan>^<apellido/nombre>^<caducidad><codigo servicio>...?
_TRACK1 = re.compile(r"%[Bb]\d{12,19}\^[^\^]{2,26}\^\d{4}")

#: banda 2: ;<pan>=<caducidad><codigo servicio>...?  (el ';' inicial es opcional)
_TRACK2 = re.compile(r";?\d{12,19}[=Dd]\d{4}\d{3}")

_CVV_WORDS = re.compile(
    r"(?i)\b(cvv2?|cvc2?|cav2|cid|csc|c[oó]digo\s+de\s+seguridad|security\s+code|"
    r"card\s+verification)\b"
)
_PIN_WORDS = re.compile(r"(?i)\b(pin|pinblock|pin\s*block|clave\s+secreta)\b")

#: 3-4 digitos sueltos (CVV) o 4-6 (PIN) cerca de la palabra clave
_CVV_VALUE = re.compile(r"(?<!\d)\d{3,4}(?!\d)")
_PIN_VALUE = re.compile(r"(?<!\d)\d{4,6}(?!\d)")

#: distancia maxima entre la palabra clave y el numero para considerarlo contexto
_WINDOW = 40


def _near(text: str, palabras: re.Pattern, values: re.Pattern) -> bool:
    posiciones = [m.end() for m in palabras.finditer(text)]
    if not posiciones:
        return False
    for match in values.finditer(text):
        for fin in posiciones:
            if 0 <= match.start() - fin <= _WINDOW:
                return True
    return False


def find_sad(text: str, where: str) -> List[Finding]:
    findings: List[Finding] = []
    if _TRACK1.search(text):
        findings.append(Finding(4, "sad_track", where, "track1", hard=True))
    if _TRACK2.search(text):
        findings.append(Finding(4, "sad_track", where, "track2", hard=True))
    if _near(text, _CVV_WORDS, _CVV_VALUE):
        findings.append(Finding(4, "sad_cvv", where, "cvv en contexto", hard=True))
    if _near(text, _PIN_WORDS, _PIN_VALUE):
        findings.append(Finding(4, "sad_pin", where, "pin en contexto", hard=True))
    return findings


# ---------------------------------------------------------------------------
# Capa 2 — nombres de campo peligrosos
# ---------------------------------------------------------------------------

_FORBIDDEN_FIELDS = {
    "pan", "cc", "ccnum", "cc_num", "ccnumber", "cc_number", "card", "cardnum",
    "card_num", "cardnumber", "card_number", "creditcard", "credit_card",
    "numero_tarjeta", "numerotarjeta", "num_tarjeta", "tarjeta", "nro_tarjeta",
    "cvv", "cvv2", "cvc", "cvc2", "cav2", "cid", "csc", "codigo_seguridad",
    "track", "track1", "track2", "track_1", "track_2", "banda", "banda_magnetica",
    "expiry", "exp", "exp_date", "expdate", "expiration", "expiration_date",
    "caducidad", "fecha_caducidad", "vencimiento", "fecha_vencimiento",
    "pin", "pinblock", "pin_block", "clave", "cardholder", "titular",
}


def forbidden_field(name: str) -> bool:
    clean_text = re.sub(r"[^a-z0-9]", "_", name.strip().lower()).strip("_")
    return clean_text in _FORBIDDEN_FIELDS


# ---------------------------------------------------------------------------
# Capa 5 — binario embebido
# ---------------------------------------------------------------------------

def find_binary_and_base64(text: str, where: str) -> Tuple[List[Finding], List[str]]:
    """Revisa lo que venga en base64.

    Devuelve los hallazgos y los textos decodificados, para que quien llame
    vuelva a pasarles las capas 3 y 4: un PAN dentro de un base64 sigue siendo
    un PAN.
    """
    findings: List[Finding] = []
    texts: List[str] = []
    for _blob, data in decode_base64_blobs(text):
        fmt = sniff_binary(data)
        if fmt:
            findings.append(Finding(
                5, "binary", where, f"{fmt} en base64 ({len(data)} bytes)", hard=False))
            continue
        inner = as_text(data)
        if inner:
            texts.append(inner)
    return findings, texts


def find_data_uri(text: str, where: str) -> List[Finding]:
    if re.search(r"(?i)data:(image|application|video|audio)/[a-z0-9.+-]+;base64,", text):
        return [Finding(5, "binary", where, "data: URI", hard=False)]
    return []


# ---------------------------------------------------------------------------
# Escaneo completo de un texto
# ---------------------------------------------------------------------------

def scan_text(text: str, where: str) -> List[Finding]:
    """Pasa las capas 3, 4 y 5 sobre un texto ya normalizado."""
    if not text:
        return []
    findings: List[Finding] = []
    findings.extend(find_pans(text, where))
    findings.extend(find_sad(text, where))
    findings.extend(find_data_uri(text, where))

    binaries, inners = find_binary_and_base64(text, where)
    findings.extend(binaries)
    for inner in inners:
        # Un nivel de anidamiento basta: si alguien mete base64 dentro de
        # base64 dentro de base64, el propio anidamiento ya es la senal.
        findings.extend(find_pans(inner, where + "[base64]"))
        findings.extend(find_sad(inner, where + "[base64]"))
    return findings
