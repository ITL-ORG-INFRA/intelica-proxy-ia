"""Capas 0 y 2-5 del sanitizer.

Cubre las tarjetas de prueba de todas las marcas, las evasiones de codificacion
que neutraliza la capa 0, SAD, base64 y la ausencia de falsos positivos.

Los numeros son de prueba, publicados por las marcas para entornos de
integracion. No son datos de tarjeta reales.
Ver docs/SOBRE-LOS-PANES-DE-PRUEBA.md.

    .venv/bin/python tests/detectores_test.py
"""
import base64
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src", "sanitizer"))

from detectors import escanear_texto, find_pans, iin_marca, luhn  # noqa: E402
from normalize import normalize  # noqa: E402

FALLOS = []


def ck(nombre, condicion, detalle=""):
    print(("  OK   " if condicion else "  FALLA ") + nombre
          + ("" if condicion else f"  <- {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


#: una por marca y por longitud, para que ninguna rama de la tabla IIN quede sin tocar
TARJETAS_DE_PRUEBA = [
    ("visa-16",     "4111111111111111"),
    ("visa-16b",    "4012888888881881"),
    ("visa-13",     "4222222222222"),
    ("mc-16",       "5555555555554444"),
    ("mc-16b",      "5105105105105100"),
    ("mc-serie-2",  "2223003122003222"),
    ("amex-15",     "378282246310005"),
    ("amex-15b",    "371449635398431"),
    ("discover",    "6011111111111117"),
    ("discover-b",  "6011000990139424"),
    ("diners",      "30569309025904"),
    ("diners-b",    "38520000023237"),
    ("jcb",         "3530111333300000"),
    ("jcb-b",       "3566002020360505"),
]


def prueba_tarjetas_conocidas():
    print("\n[1] tarjetas de prueba de todas las marcas")
    for nombre, pan in TARJETAS_DE_PRUEBA:
        hallazgos = find_pans(pan, "t")
        ck(f"{nombre} detectada", len(hallazgos) == 1,
           f"luhn={luhn(pan)} iin='{iin_marca(pan)}'")


def prueba_formatos():
    print("\n[2] en frase, agrupado y con separadores")
    casos = [
        ("en frase",         "mi tarjeta es 4111111111111111 gracias"),
        ("4-4-4-4 espacios", "4111 1111 1111 1111"),
        ("4-4-4-4 guiones",  "4111-1111-1111-1111"),
        ("amex 4-6-5",       "3782 822463 10005"),
        ("separadores mixtos", "4111 1111-1111 1111"),
    ]
    for nombre, texto in casos:
        ck(nombre, len(find_pans(texto, "t")) >= 1, texto)


def prueba_falsos_positivos():
    print("\n[3] lo que NO debe disparar")
    casos = [
        ("16 digitos sin Luhn", "1234567890123456"),
        ("timestamp",           "20260825120000"),
        ("identificador largo", "9999999999999999"),
        ("telefono",            "+34 600 123 456"),
        ("importe",             "1234567.89 EUR"),
        ("uuid",                "550e8400-e29b-41d4-a716-446655440000"),
    ]
    for nombre, texto in casos:
        hallazgos = find_pans(texto, "t")
        ck(nombre, len(hallazgos) == 0, [h.detalle for h in hallazgos])


def prueba_evasiones():
    print("\n[4] evasiones de codificacion que neutraliza la capa 0")
    casos = [
        ("fullwidth",      "４１１１１１１１１１１１１１１１"),
        ("zero-width",     "4111​1111​1111​1111"),
        ("soft hyphen",    "4111­1111­1111­1111"),
        ("guion Unicode",  "4111‑1111‑1111‑1111"),
        ("espacio duro",   "4111 1111 1111 1111"),
    ]
    for nombre, texto in casos:
        ck(nombre, len(find_pans(normalize(texto), "t")) >= 1,
           repr(normalize(texto))[:60])


def prueba_sad():
    print("\n[5] SAD: siempre bloqueo duro")
    casos = [
        ("track1",      "%B4111111111111111^DOE/JOHN^25121011000000000000?"),
        ("track2",      ";4111111111111111=25121011000000000?"),
        ("cvv",         "el cvv es 123"),
        ("cvv2",        "CVV2: 4567"),
        ("pin",         "mi pin es 4321"),
    ]
    for nombre, texto in casos:
        hallazgos = [h for h in escanear_texto(texto, "t") if h.capa == 4]
        ck(nombre, len(hallazgos) >= 1 and all(h.duro for h in hallazgos),
           [h.detalle for h in hallazgos])

    print("\n[6] SAD: sin contexto no hay hallazgo")
    for nombre, texto in [("pin sin numero", "necesito el pin del router"),
                          ("numero sin contexto", "el total son 123 euros")]:
        hallazgos = [h for h in escanear_texto(texto, "t") if h.capa == 4]
        ck(nombre, len(hallazgos) == 0, [h.detalle for h in hallazgos])


def prueba_base64_y_binario():
    print("\n[7] base64 y binario embebido")
    oculto = base64.b64encode(b"tarjeta 4111111111111111 fin").decode()
    hallazgos = escanear_texto(f"adjunto: {oculto}", "t")
    ck("PAN escondido en base64", any(h.tipo == "pan" for h in hallazgos),
       [h.como_dict() for h in hallazgos])

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 200).decode()
    hallazgos = escanear_texto(f"img: {png}", "t")
    ck("PNG en base64", any(h.tipo == "binario" for h in hallazgos),
       [h.detalle for h in hallazgos])

    ck("data: URI", any(h.tipo == "binario" for h in
                        escanear_texto("data:image/png;base64,iVBORw0KGgo=", "t")))


def prueba_hallazgo_sin_valor():
    print("\n[8] el hallazgo nunca transporta el valor")
    hallazgos = escanear_texto("tarjeta 4111111111111111", "t")
    ck("ningun hallazgo contiene el PAN",
       not any("4111111111111111" in str(h.como_dict()) for h in hallazgos),
       [h.como_dict() for h in hallazgos])


def main():
    prueba_tarjetas_conocidas()
    prueba_formatos()
    prueba_falsos_positivos()
    prueba_evasiones()
    prueba_sad()
    prueba_base64_y_binario()
    prueba_hallazgo_sin_valor()
    print("\n" + ("TODO OK" if not FALLOS else f"{len(FALLOS)} FALLOS: {FALLOS}"))
    return 1 if FALLOS else 0


if __name__ == "__main__":
    sys.exit(main())
