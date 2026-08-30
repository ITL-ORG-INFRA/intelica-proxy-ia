"""Las dos implementaciones de deteccion, enfrentadas.

Lo que se comprueba aqui no es que la segunda implementacion funcione. Es que
NO falla en los mismos sitios que la primera. Si ambas fueran ciegas ante los
mismos cases, el verifier no verificaria nada y su "no encontre nada" no
seria evidencia de nada — que es exactamente el motivo por el que se descarto
Macie.

    sanitizer     -> regex sobre tiradas de digitos, luego Luhn e IIN
    verifier   -> sin regex: extrae todos los digitos y desliza una ventana

    .venv/bin/python tests/detection2_test.py
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src", "sanitizer"))
sys.path.insert(0, os.path.join(REPO, "src", "verifier"))

from detectors import find_pans  # noqa: E402
from detection2 import pans_in_stream  # noqa: E402

FAILURES = []


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


def test_agreement():
    print("\n[1] en lo evidente, las dos coinciden")
    for pan in ["4111111111111111", "5555555555554444",
                "378282246310005", "4111 1111 1111 1111"]:
        regex = len(find_pans(pan, "t")) > 0
        ventana = len(pans_in_stream(pan)) > 0
        ck(f"ambas detectan {pan[:8]}...", regex and ventana,
           f"regex={regex} ventana={ventana}")


def test_where_it_helps():
    print("\n[2] separadores que el regex no contempla")
    print("    (esta es la razon de existir del verifier)")
    cases = [
        "4111.1111.1111.1111",
        "4111/1111/1111/1111",
        "4111.x.1111/1111 abc 1111",
        "4111_1111_1111_1111",
        "4111\n1111\n1111\n1111",
        "num 4111 ref 1111 lote 1111 fin 1111",
    ]
    ciegos = 0
    for text in cases:
        regex = len(find_pans(text, "t")) > 0
        ventana = len(pans_in_stream(text)) > 0
        if not regex and ventana:
            ciegos += 1
            state = "regex CIEGO, ventana LO COGE"
        elif regex and ventana:
            state = "ambas"
        else:
            state = "ninguna"
        ck(f"{text[:34]!r:38} -> {state}", ventana,
           f"regex={regex} ventana={ventana}")

    ck(f"la ventana aporta cobertura real ({ciegos}/{len(cases)} cases)",
       ciegos >= 4, f"solo {ciegos} cases donde el regex es ciego")


def test_not_weaker():
    print("\n[3] la ventana tampoco falla en los de siempre")
    for pan in ["4012888888881881", "5105105105105100", "6011111111111117",
                "3530111333300000", "30569309025904"]:
        ck(f"detecta {pan[:6]}...", len(pans_in_stream(pan)) > 0)


def test_clean_text():
    print("\n[4] texto limpio de verdad: sin hallazgos")
    for text in ["hola, resume este contrato de arrendamiento por favor",
                  "el importe asciende a 1.234,56 euros",
                  "factura 2026-0000123 del 25 de agosto"]:
        findings = pans_in_stream(text)
        ck(f"{text[:38]!r}", len(findings) == 0, findings)


def test_performance():
    print("\n[5] termina y no se va de tiempo")
    grande = "lorem ipsum 12345 dolor sit amet 98765 " * 2000

    inicio = time.monotonic()
    pans_in_stream(grande)
    ms = (time.monotonic() - inicio) * 1000
    ck(f"78k caracteres sin PAN en {ms:.0f} ms", ms < 5000, f"{ms:.0f} ms")

    con_pan = grande + " 4111111111111111 " + grande
    inicio = time.monotonic()
    findings = pans_in_stream(con_pan)
    ms = (time.monotonic() - inicio) * 1000
    ck(f"156k caracteres con PAN en {ms:.0f} ms", len(findings) >= 1 and ms < 10000,
       f"{ms:.0f} ms, {len(findings)} hallazgos")


def test_finding_carries_no_value():
    print("\n[6] el hallazgo no lleva el numero")
    findings = pans_in_stream("tarjeta 4111111111111111")
    ck("sin PAN en el hallazgo",
       not any("4111" in str(h) for h in findings), findings)
    ck("marca 'separado' correcta cuando venia junto",
       bool(findings) and findings[0]["separado"] is False, findings)

    separado = pans_in_stream("4111.1111.1111.1111")
    ck("y detecta cuando venia partido",
       bool(separado) and separado[0]["separado"] is True, separado)


def main():
    test_agreement()
    test_where_it_helps()
    test_not_weaker()
    test_clean_text()
    test_performance()
    test_finding_carries_no_value()
    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
