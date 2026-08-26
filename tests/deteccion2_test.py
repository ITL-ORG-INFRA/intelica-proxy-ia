"""Las dos implementaciones de deteccion, enfrentadas.

Lo que se comprueba aqui no es que la segunda implementacion funcione. Es que
NO falla en los mismos sitios que la primera. Si ambas fueran ciegas ante los
mismos casos, el verificador no verificaria nada y su "no encontre nada" no
seria evidencia de nada — que es exactamente el motivo por el que se descarto
Macie.

    sanitizer     -> regex sobre tiradas de digitos, luego Luhn e IIN
    verificador   -> sin regex: extrae todos los digitos y desliza una ventana

    .venv/bin/python tests/deteccion2_test.py
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src", "sanitizer"))
sys.path.insert(0, os.path.join(REPO, "src", "verificador"))

from detectors import find_pans  # noqa: E402
from deteccion2 import buscar_panes  # noqa: E402

FALLOS = []


def ck(nombre, condicion, detalle=""):
    print(("  OK   " if condicion else "  FALLA ") + nombre
          + ("" if condicion else f"  <- {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


def prueba_acuerdo():
    print("\n[1] en lo evidente, las dos coinciden")
    for pan in ["4111111111111111", "5555555555554444",
                "378282246310005", "4111 1111 1111 1111"]:
        regex = len(find_pans(pan, "t")) > 0
        ventana = len(buscar_panes(pan)) > 0
        ck(f"ambas detectan {pan[:8]}...", regex and ventana,
           f"regex={regex} ventana={ventana}")


def prueba_donde_aporta():
    print("\n[2] separadores que el regex no contempla")
    print("    (esta es la razon de existir del verificador)")
    casos = [
        "4111.1111.1111.1111",
        "4111/1111/1111/1111",
        "4111.x.1111/1111 abc 1111",
        "4111_1111_1111_1111",
        "4111\n1111\n1111\n1111",
        "num 4111 ref 1111 lote 1111 fin 1111",
    ]
    ciegos = 0
    for texto in casos:
        regex = len(find_pans(texto, "t")) > 0
        ventana = len(buscar_panes(texto)) > 0
        if not regex and ventana:
            ciegos += 1
            estado = "regex CIEGO, ventana LO COGE"
        elif regex and ventana:
            estado = "ambas"
        else:
            estado = "ninguna"
        ck(f"{texto[:34]!r:38} -> {estado}", ventana,
           f"regex={regex} ventana={ventana}")

    ck(f"la ventana aporta cobertura real ({ciegos}/{len(casos)} casos)",
       ciegos >= 4, f"solo {ciegos} casos donde el regex es ciego")


def prueba_no_se_queda_corta():
    print("\n[3] la ventana tampoco falla en los de siempre")
    for pan in ["4012888888881881", "5105105105105100", "6011111111111117",
                "3530111333300000", "30569309025904"]:
        ck(f"detecta {pan[:6]}...", len(buscar_panes(pan)) > 0)


def prueba_texto_limpio():
    print("\n[4] texto limpio de verdad: sin hallazgos")
    for texto in ["hola, resume este contrato de arrendamiento por favor",
                  "el importe asciende a 1.234,56 euros",
                  "factura 2026-0000123 del 25 de agosto"]:
        hallazgos = buscar_panes(texto)
        ck(f"{texto[:38]!r}", len(hallazgos) == 0, hallazgos)


def prueba_rendimiento():
    print("\n[5] termina y no se va de tiempo")
    grande = "lorem ipsum 12345 dolor sit amet 98765 " * 2000

    inicio = time.monotonic()
    buscar_panes(grande)
    ms = (time.monotonic() - inicio) * 1000
    ck(f"78k caracteres sin PAN en {ms:.0f} ms", ms < 5000, f"{ms:.0f} ms")

    con_pan = grande + " 4111111111111111 " + grande
    inicio = time.monotonic()
    hallazgos = buscar_panes(con_pan)
    ms = (time.monotonic() - inicio) * 1000
    ck(f"156k caracteres con PAN en {ms:.0f} ms", len(hallazgos) >= 1 and ms < 10000,
       f"{ms:.0f} ms, {len(hallazgos)} hallazgos")


def prueba_hallazgo_sin_valor():
    print("\n[6] el hallazgo no lleva el numero")
    hallazgos = buscar_panes("tarjeta 4111111111111111")
    ck("sin PAN en el hallazgo",
       not any("4111" in str(h) for h in hallazgos), hallazgos)
    ck("marca 'separado' correcta cuando venia junto",
       bool(hallazgos) and hallazgos[0]["separado"] is False, hallazgos)

    separado = buscar_panes("4111.1111.1111.1111")
    ck("y detecta cuando venia partido",
       bool(separado) and separado[0]["separado"] is True, separado)


def main():
    prueba_acuerdo()
    prueba_donde_aporta()
    prueba_no_se_queda_corta()
    prueba_texto_limpio()
    prueba_rendimiento()
    prueba_hallazgo_sin_valor()
    print("\n" + ("TODO OK" if not FALLOS else f"{len(FALLOS)} FALLOS: {FALLOS}"))
    return 1 if FALLOS else 0


if __name__ == "__main__":
    sys.exit(main())
