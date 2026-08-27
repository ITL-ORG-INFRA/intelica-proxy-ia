"""La tabla del SPEC §10, ejecutada.

El SPEC dice que esa tabla esta verificada, no que sea aspiracional: "si tu
implementacion difiere de la tabla, el bug es tuyo, no de la tabla". Esto la
comprueba fixture a fixture.

    .venv/bin/python tests/fixtures_test.py
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

import detect  # noqa: E402
from sanitize import validate_stream  # noqa: E402

FIXTURES = os.path.join(REPO, "tests", "fixtures")
FALLOS = []


def ck(nombre, condicion, detalle=""):
    print(("  OK   " if condicion else "  FALLA ") + nombre
          + ("" if condicion else f"  <- {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


#: fixture -> (reglas exactas esperadas, fragmento que debe aparecer en la ruta)
ESPERADO = {
    "real_batch.jsonl":             ([], None),
    "tier_tables.jsonl":            ([], None),
    "poisoned_pan_content.jsonl":   (["pan"], "messages"),
    "poisoned_pan_spaced.jsonl":    (["pan"], "messages"),
    "poisoned_pan_fullwidth.jsonl": (["pan"], "messages"),
    "poisoned_pan_custom_id.jsonl": (["pan"], "custom_id"),
    "poisoned_pan_system.jsonl":    (["pan"], "system"),
    "poisoned_track2.jsonl":        (["pan", "sad.track2"], None),
    "poisoned_cvv_ctx.jsonl":       (["sad.cvv"], None),
    "poisoned_unknown_key.jsonl":   (["schema.unknown_key"], "metadata"),
    "poisoned_field_name.jsonl":    (["field_name"], "card_number"),
    "poisoned_content_blocks.jsonl": (["schema.content_blocks"], "content"),
}

SEVERIDAD_ESPERADA = {
    "poisoned_unknown_key.jsonl": "SCHEMA",
    "poisoned_field_name.jsonl": "CHD",
}


def main():
    print("\n[1] la tabla del SPEC §10")
    for fixture, (reglas_esperadas, fragmento) in ESPERADO.items():
        ruta = os.path.join(FIXTURES, fixture)
        if not os.path.isfile(ruta):
            ck(f"{fixture}", False, "no existe el fixture")
            continue

        # Cache limpia por fixture: un veredicto cacheado de otro caso falsearia
        # este, y la cache es global por diseno.
        detect.reset_cache()
        with open(ruta, encoding="utf-8") as f:
            veredicto, limpias, rechazadas = validate_stream(f)

        reglas = sorted({h.rule for h in veredicto.findings})
        ok = reglas == sorted(reglas_esperadas)
        ck(f"{fixture:32} {reglas or 'sin hallazgos'}", ok,
           f"esperaba {sorted(reglas_esperadas)}")

        if fragmento and veredicto.findings:
            rutas = " ".join(h.path for h in veredicto.findings)
            ck(f"{'':32} ruta contiene {fragmento!r}", fragmento in rutas, rutas)

        if fixture in SEVERIDAD_ESPERADA and veredicto.findings:
            severidades = {h.severity for h in veredicto.findings}
            ck(f"{'':32} severidad {SEVERIDAD_ESPERADA[fixture]}",
               severidades == {SEVERIDAD_ESPERADA[fixture]}, severidades)

    print("\n[2] el corpus real no produce NI UN hallazgo")
    # Es el test anti-falso-positivo y el mas importante de la suite: protege
    # contra el dia que alguien "mejore" el regex y el pipeline se pare a las
    # tres de la manana por una tabla de tarifas.
    for fixture in ("real_batch.jsonl", "tier_tables.jsonl"):
        detect.reset_cache()
        with open(os.path.join(FIXTURES, fixture), encoding="utf-8") as f:
            veredicto, limpias, _ = validate_stream(f)
        ck(f"{fixture:32} {veredicto.n_ok}/{veredicto.n_lines} lineas limpias",
           veredicto.n_ok == veredicto.n_lines and not veredicto.findings,
           [(h.rule, h.path) for h in veredicto.findings])

    print("\n[3] cifras largas del corpus que NO deben disparar")
    for cifra in ("120,000,000,000", "15,000,000,000", "USD 0.0000026",
                  "72 of 877", "1234567890123456", "20260825120000"):
        detect.reset_cache()
        ck(f"{cifra!r:24}", detect.scan_text(detect.normalize(cifra)) == [],
           detect.scan_text(detect.normalize(cifra)))

    print("\n[4] ningun Finding serializado lleva 6+ digitos seguidos")
    # Verificacion automatica de la invariante "no se filtra el valor".
    detect.reset_cache()
    largos = re.compile(r"\d{6,}")
    fugas = []
    for fixture in ESPERADO:
        ruta = os.path.join(FIXTURES, fixture)
        if not os.path.isfile(ruta):
            continue
        detect.reset_cache()
        with open(ruta, encoding="utf-8") as f:
            veredicto, _, _ = validate_stream(f)
        for h in veredicto.findings:
            serializado = json.dumps({"rule": h.rule, "path": h.path,
                                      "severity": h.severity, "line_no": h.line_no})
            if largos.search(serializado):
                fugas.append((fixture, serializado))
    ck(f"revisados los hallazgos de {len(ESPERADO)} fixtures", not fugas, fugas[:3])

    print("\n[5] el dedup por hash no altera el veredicto")
    detect.reset_cache()
    texto = "un bloque system largo repetido " * 50
    primero = detect.scan_text(texto)
    segundo = detect.scan_text(texto)
    ck("mismo resultado con y sin cache", primero == segundo, (primero, segundo))
    detect.reset_cache()
    ck("y tras limpiar la cache", detect.scan_text(texto) == primero)

    print("\n" + ("TODO OK" if not FALLOS else f"{len(FALLOS)} FALLOS: {FALLOS}"))
    return 1 if FALLOS else 0


if __name__ == "__main__":
    sys.exit(main())
