"""Capa 0 — normalizacion.

Todo lo que venga despues detecta sobre el texto que salga de aqui. Sin este
paso, "４１１１ ­1111​1111 1111" no se parece a un PAN para ningun regex,
y sin embargo el modelo al otro lado lo lee perfectamente.

Regla que gobierna el modulo: **se envia lo que se escaneo**. El texto
normalizado es el que viaja a la zona limpia, no el original. Si se escaneara
una representacion y se enviara otra, el hueco entre las dos es exactamente
por donde se cuela un PAN.
"""
import base64
import binascii
import re
import unicodedata
from typing import List, Tuple

#: invisibles: separan digitos sin dejar rastro visible
_INVISIBLES = dict.fromkeys([
    0x00AD,  # soft hyphen
    0x200B, 0x200C, 0x200D,  # zero width space / non-joiner / joiner
    0x2060, 0x2061, 0x2062, 0x2063, 0x2064,  # word joiner e invisibles matematicos
    0xFEFF,  # BOM / zero width no-break space
    0x180E,  # mongolian vowel separator
])

#: controles de direccion: reordenan lo que se ve sin cambiar los bytes
_BIDI = dict.fromkeys([
    0x200E, 0x200F,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
])

_STRIP = {**_INVISIBLES, **_BIDI}

#: separadores exoticos que hacen de guion o espacio entre grupos de digitos
_SEPARATORS = {
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2015: "-",
    0x2212: "-", 0xFE58: "-", 0xFE63: "-", 0xFF0D: "-",
    0x00A0: " ", 0x2007: " ", 0x202F: " ", 0x2009: " ", 0x2002: " ", 0x2003: " ",
}

#: candidato a base64: suficientemente largo para esconder algo
_B64 = re.compile(r"(?:[A-Za-z0-9+/]{4}){8,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_B64URL = re.compile(r"(?:[A-Za-z0-9_-]{4}){8,}(?:[A-Za-z0-9_-]{2}==|[A-Za-z0-9_-]{3}=)?")

#: cabeceras binarias que no queremos ni decodificadas
_MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"%PDF-": "pdf",
    b"PK\x03\x04": "zip/ooxml",
    b"RIFF": "riff/webp",
    b"\x1f\x8b": "gzip",
    b"BM": "bmp",
    b"\x00\x00\x01\x00": "ico",
}


def normalize(text: str) -> str:
    """Deja el texto en una sola forma canonica antes de mirarlo."""
    if not text:
        return text
    # NFKC colapsa fullwidth (４ -> 4), digitos con estilo y ligaduras.
    out = unicodedata.normalize("NFKC", text)
    out = out.translate(_STRIP)
    out = out.translate(_SEPARATORS)
    return out


def decode_base64_blobs(text: str, max_blobs: int = 64) -> List[Tuple[str, bytes]]:
    """Saca lo que haya escondido en base64 para poder escanearlo.

    Devuelve (blob_original, bytes_decodificados). No modifica el texto: quien
    llama decide si eso es motivo de bloqueo o solo material a escanear.
    """
    found: List[Tuple[str, bytes]] = []
    seen = set()
    for pattern in (_B64, _B64URL):
        for match in pattern.finditer(text):
            blob = match.group(0)
            if blob in seen or len(found) >= max_blobs:
                continue
            seen.add(blob)
            padded = blob + "=" * (-len(blob) % 4)
            try:
                if pattern is _B64URL:
                    data = base64.urlsafe_b64decode(padded)
                else:
                    data = base64.b64decode(padded, validate=True)
            except (binascii.Error, ValueError):
                continue
            if data:
                found.append((blob, data))
    return found


def sniff_binary(data: bytes) -> str:
    """Nombra el formato si los primeros bytes delatan un binario."""
    for magic, name in _MAGIC.items():
        if data.startswith(magic):
            return name
    # Sin cabecera conocida: se considera binario si no es texto legible.
    sample = data[:512]
    if b"\x00" in sample:
        return "binario"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "binario"
    return ""


def as_text(data: bytes) -> str:
    """Lo decodificado, como texto escaneable. Vacio si no lo es."""
    try:
        return normalize(data.decode("utf-8"))
    except UnicodeDecodeError:
        return ""
