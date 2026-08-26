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
class Hallazgo:
    capa: int
    tipo: str          # "pan" | "sad_track" | "sad_cvv" | "sad_pin" | "campo" | "binario"
    donde: str         # ruta dentro del envelope, p.ej. "requests[3].params.messages[0]"
    detalle: str = ""  # marca, longitud, formato — nunca el valor
    duro: bool = False  # aborta el lote entero, no solo la peticion

    def como_dict(self) -> Dict[str, Any]:
        return {"capa": self.capa, "tipo": self.tipo, "donde": self.donde,
                "detalle": self.detalle, "duro": self.duro}


# ---------------------------------------------------------------------------
# Capa 3 — PAN en texto libre
# ---------------------------------------------------------------------------

#: tirada continua de 13 a 19 digitos
_PAN_CONTIGUO = re.compile(r"(?<![\d])(\d{13,19})(?![\d])")

#: grupos separados por espacio o guion: 4-4-4-4 y sus variantes de 13 a 19
_PAN_AGRUPADO = re.compile(
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


def iin_marca(digits: str) -> str:
    """Devuelve la marca si el prefijo y la longitud encajan con una emisora.

    Luhn solo deja pasar 1 de cada 10 numeros al azar; exigir ademas un IIN
    valido descarta casi todo lo que no es una tarjeta (referencias, ids,
    numeros de cuenta) sin dejar pasar ninguna de las marcas reales.
    """
    length = len(digits)
    for marca, longitudes, prefijos, rangos in _IIN:
        if length not in longitudes:
            continue
        if any(digits.startswith(p) for p in prefijos):
            return marca
        for desde, hasta, n in rangos:
            if length >= n and desde <= int(digits[:n]) <= hasta:
                return marca
    return ""


def _solo_digitos(texto: str) -> str:
    return texto.replace(" ", "").replace("-", "")


def find_pans(texto: str, donde: str) -> List[Hallazgo]:
    hallazgos: List[Hallazgo] = []
    vistos = set()
    for patron, formato in ((_PAN_CONTIGUO, "contiguo"), (_PAN_AGRUPADO, "agrupado")):
        for match in patron.finditer(texto):
            digits = _solo_digitos(match.group(1))
            if not 13 <= len(digits) <= 19 or digits in vistos:
                continue
            if not luhn(digits):
                continue
            marca = iin_marca(digits)
            if not marca:
                continue
            vistos.add(digits)
            hallazgos.append(Hallazgo(
                capa=3, tipo="pan", donde=donde,
                # Marca y longitud bastan para investigar. El numero, no.
                detalle=f"{marca}/{len(digits)}d/{formato}",
            ))
    return hallazgos


# ---------------------------------------------------------------------------
# Capa 4 — SAD (datos de autenticacion). Nunca son almacenables: bloqueo duro.
# ---------------------------------------------------------------------------

#: banda 1: %B<pan>^<apellido/nombre>^<caducidad><codigo servicio>...?
_TRACK1 = re.compile(r"%[Bb]\d{12,19}\^[^\^]{2,26}\^\d{4}")

#: banda 2: ;<pan>=<caducidad><codigo servicio>...?  (el ';' inicial es opcional)
_TRACK2 = re.compile(r";?\d{12,19}[=Dd]\d{4}\d{3}")

_CVV_PALABRAS = re.compile(
    r"(?i)\b(cvv2?|cvc2?|cav2|cid|csc|c[oó]digo\s+de\s+seguridad|security\s+code|"
    r"card\s+verification)\b"
)
_PIN_PALABRAS = re.compile(r"(?i)\b(pin|pinblock|pin\s*block|clave\s+secreta)\b")

#: 3-4 digitos sueltos (CVV) o 4-6 (PIN) cerca de la palabra clave
_CVV_VALOR = re.compile(r"(?<!\d)\d{3,4}(?!\d)")
_PIN_VALOR = re.compile(r"(?<!\d)\d{4,6}(?!\d)")

#: distancia maxima entre la palabra clave y el numero para considerarlo contexto
_VENTANA = 40


def _cerca(texto: str, palabras: re.Pattern, valores: re.Pattern) -> bool:
    posiciones = [m.end() for m in palabras.finditer(texto)]
    if not posiciones:
        return False
    for match in valores.finditer(texto):
        for fin in posiciones:
            if 0 <= match.start() - fin <= _VENTANA:
                return True
    return False


def find_sad(texto: str, donde: str) -> List[Hallazgo]:
    hallazgos: List[Hallazgo] = []
    if _TRACK1.search(texto):
        hallazgos.append(Hallazgo(4, "sad_track", donde, "track1", duro=True))
    if _TRACK2.search(texto):
        hallazgos.append(Hallazgo(4, "sad_track", donde, "track2", duro=True))
    if _cerca(texto, _CVV_PALABRAS, _CVV_VALOR):
        hallazgos.append(Hallazgo(4, "sad_cvv", donde, "cvv en contexto", duro=True))
    if _cerca(texto, _PIN_PALABRAS, _PIN_VALOR):
        hallazgos.append(Hallazgo(4, "sad_pin", donde, "pin en contexto", duro=True))
    return hallazgos


# ---------------------------------------------------------------------------
# Capa 2 — nombres de campo peligrosos
# ---------------------------------------------------------------------------

_CAMPOS_PROHIBIDOS = {
    "pan", "cc", "ccnum", "cc_num", "ccnumber", "cc_number", "card", "cardnum",
    "card_num", "cardnumber", "card_number", "creditcard", "credit_card",
    "numero_tarjeta", "numerotarjeta", "num_tarjeta", "tarjeta", "nro_tarjeta",
    "cvv", "cvv2", "cvc", "cvc2", "cav2", "cid", "csc", "codigo_seguridad",
    "track", "track1", "track2", "track_1", "track_2", "banda", "banda_magnetica",
    "expiry", "exp", "exp_date", "expdate", "expiration", "expiration_date",
    "caducidad", "fecha_caducidad", "vencimiento", "fecha_vencimiento",
    "pin", "pinblock", "pin_block", "clave", "cardholder", "titular",
}


def campo_prohibido(nombre: str) -> bool:
    limpio = re.sub(r"[^a-z0-9]", "_", nombre.strip().lower()).strip("_")
    return limpio in _CAMPOS_PROHIBIDOS


# ---------------------------------------------------------------------------
# Capa 5 — binario embebido
# ---------------------------------------------------------------------------

def find_binario_y_base64(texto: str, donde: str) -> Tuple[List[Hallazgo], List[str]]:
    """Revisa lo que venga en base64.

    Devuelve los hallazgos y los textos decodificados, para que quien llame
    vuelva a pasarles las capas 3 y 4: un PAN dentro de un base64 sigue siendo
    un PAN.
    """
    hallazgos: List[Hallazgo] = []
    textos: List[str] = []
    for _blob, data in decode_base64_blobs(texto):
        formato = sniff_binary(data)
        if formato:
            hallazgos.append(Hallazgo(
                5, "binario", donde, f"{formato} en base64 ({len(data)} bytes)", duro=False))
            continue
        interior = as_text(data)
        if interior:
            textos.append(interior)
    return hallazgos, textos


def find_data_uri(texto: str, donde: str) -> List[Hallazgo]:
    if re.search(r"(?i)data:(image|application|video|audio)/[a-z0-9.+-]+;base64,", texto):
        return [Hallazgo(5, "binario", donde, "data: URI", duro=False)]
    return []


# ---------------------------------------------------------------------------
# Escaneo completo de un texto
# ---------------------------------------------------------------------------

def escanear_texto(texto: str, donde: str) -> List[Hallazgo]:
    """Pasa las capas 3, 4 y 5 sobre un texto ya normalizado."""
    if not texto:
        return []
    hallazgos: List[Hallazgo] = []
    hallazgos.extend(find_pans(texto, donde))
    hallazgos.extend(find_sad(texto, donde))
    hallazgos.extend(find_data_uri(texto, donde))

    binarios, interiores = find_binario_y_base64(texto, donde)
    hallazgos.extend(binarios)
    for interior in interiores:
        # Un nivel de anidamiento basta: si alguien mete base64 dentro de
        # base64 dentro de base64, el propio anidamiento ya es la senal.
        hallazgos.extend(find_pans(interior, donde + "[base64]"))
        hallazgos.extend(find_sad(interior, donde + "[base64]"))
    return hallazgos
