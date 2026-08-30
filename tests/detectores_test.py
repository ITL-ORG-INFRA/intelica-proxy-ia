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

from detectors import scan_text, find_pans, iin_brand, luhn  # noqa: E402
from normalize import normalize  # noqa: E402

FAILURES = []


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


#: una por marca y por longitud, para que ninguna rama de la tabla IIN quede sin tocar
TEST_CARDS = [
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


def test_known_cards():
    print("\n[1] tarjetas de prueba de todas las marcas")
    for name, pan in TEST_CARDS:
        findings = find_pans(pan, "t")
        ck(f"{name} detectada", len(findings) == 1,
           f"luhn={luhn(pan)} iin='{iin_brand(pan)}'")


def test_formats():
    print("\n[2] en frase, agrupado y con separadores")
    cases = [
        ("en frase",         "mi tarjeta es 4111111111111111 gracias"),
        ("4-4-4-4 espacios", "4111 1111 1111 1111"),
        ("4-4-4-4 guiones",  "4111-1111-1111-1111"),
        ("amex 4-6-5",       "3782 822463 10005"),
        ("separadores mixtos", "4111 1111-1111 1111"),
    ]
    for name, text in cases:
        ck(name, len(find_pans(text, "t")) >= 1, text)


def test_false_positives():
    print("\n[3] lo que NO debe disparar")
    cases = [
        ("16 digitos sin Luhn", "1234567890123456"),
        ("timestamp",           "20260825120000"),
        ("identificador largo", "9999999999999999"),
        ("telefono",            "+34 600 123 456"),
        ("importe",             "1234567.89 EUR"),
        ("uuid",                "550e8400-e29b-41d4-a716-446655440000"),
        ("uuid en frase",       "Referencia 550e8400-e29b-41d4-a716-446655440000 del caso"),
        ("hash sha256",         "a" * 64),
        ("token opaco",         "eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop"),
    ]
    for name, text in cases:
        # Con escanear_texto, no con find_pans: el UUID no disparaba la capa 3
        # pero si la 5, porque encaja con el alfabeto base64url. Probar solo
        # una capa dejaba pasar el falso positivo.
        findings = scan_text(text, "t")
        ck(name, len(findings) == 0,
           [(h.layer, h.type, h.detail) for h in findings])


def test_evasions():
    print("\n[4] evasiones de codificacion que neutraliza la capa 0")
    cases = [
        ("fullwidth",      "４１１１１１１１１１１１１１１１"),
        ("zero-width",     "4111​1111​1111​1111"),
        ("soft hyphen",    "4111­1111­1111­1111"),
        ("guion Unicode",  "4111‑1111‑1111‑1111"),
        ("espacio duro",   "4111 1111 1111 1111"),
    ]
    for name, text in cases:
        ck(name, len(find_pans(normalize(text), "t")) >= 1,
           repr(normalize(text))[:60])


def test_sad():
    print("\n[5] SAD: siempre bloqueo duro")
    cases = [
        ("track1",      "%B4111111111111111^DOE/JOHN^25121011000000000000?"),
        ("track2",      ";4111111111111111=25121011000000000?"),
        ("cvv",         "el cvv es 123"),
        ("cvv2",        "CVV2: 4567"),
        ("pin",         "mi pin es 4321"),
    ]
    for name, text in cases:
        findings = [h for h in scan_text(text, "t") if h.layer == 4]
        ck(name, len(findings) >= 1 and all(h.hard for h in findings),
           [h.detail for h in findings])

    print("\n[6] SAD: sin contexto no hay hallazgo")
    for name, text in [("pin sin numero", "necesito el pin del router"),
                          ("numero sin contexto", "el total son 123 euros")]:
        findings = [h for h in scan_text(text, "t") if h.layer == 4]
        ck(name, len(findings) == 0, [h.detail for h in findings])


def test_base64_and_binary():
    print("\n[7] base64 y binario embebido")
    oculto = base64.b64encode(b"tarjeta 4111111111111111 fin").decode()
    findings = scan_text(f"adjunto: {oculto}", "t")
    ck("PAN escondido en base64", any(h.type == "pan" for h in findings),
       [h.as_dict() for h in findings])

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 200).decode()
    findings = scan_text(f"img: {png}", "t")
    ck("PNG en base64", any(h.type == "binary" for h in findings),
       [h.detail for h in findings])

    ck("data: URI", any(h.type == "binary" for h in
                        scan_text("data:image/png;base64,iVBORw0KGgo=", "t")))


def test_finding_carries_no_value():
    print("\n[8] el hallazgo nunca transporta el valor")
    findings = scan_text("tarjeta 4111111111111111", "t")
    ck("ningun hallazgo contiene el PAN",
       not any("4111111111111111" in str(h.as_dict()) for h in findings),
       [h.as_dict() for h in findings])


def main():
    test_known_cards()
    test_formats()
    test_false_positives()
    test_evasions()
    test_sad()
    test_base64_and_binary()
    test_finding_carries_no_value()
    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
