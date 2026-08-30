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
FAILURES = []


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


#: fixture -> (reglas exactas expected, fragmento que debe aparecer en la ruta)
EXPECTED = {
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

EXPECTED_SEVERITY = {
    "poisoned_unknown_key.jsonl": "SCHEMA",
    "poisoned_field_name.jsonl": "CHD",
}


def main():
    print("\n[1] la tabla del SPEC §10")
    for fixture, (expected_rules, fragmento) in EXPECTED.items():
        path = os.path.join(FIXTURES, fixture)
        if not os.path.isfile(path):
            ck(f"{fixture}", False, "no existe el fixture")
            continue

        # Cache limpia por fixture: un veredicto cacheado de otro caso falsearia
        # este, y la cache es global por diseno.
        detect.reset_cache()
        with open(path, encoding="utf-8") as f:
            veredicto, clean_count, rejected_count = validate_stream(f)

        reglas = sorted({h.rule for h in veredicto.findings})
        ok = reglas == sorted(expected_rules)
        ck(f"{fixture:32} {reglas or 'sin findings'}", ok,
           f"esperaba {sorted(expected_rules)}")

        if fragmento and veredicto.findings:
            paths = " ".join(h.path for h in veredicto.findings)
            ck(f"{'':32} ruta contiene {fragmento!r}", fragmento in paths, paths)

        if fixture in EXPECTED_SEVERITY and veredicto.findings:
            severities = {h.severity for h in veredicto.findings}
            ck(f"{'':32} severidad {EXPECTED_SEVERITY[fixture]}",
               severities == {EXPECTED_SEVERITY[fixture]}, severities)

    print("\n[2] el corpus real no produce NI UN hallazgo")
    # Es el test anti-falso-positivo y el mas importante de la suite: protege
    # contra el dia que alguien "mejore" el regex y el pipeline se pare a las
    # tres de la manana por una tabla de tarifas.
    for fixture in ("real_batch.jsonl", "tier_tables.jsonl"):
        detect.reset_cache()
        with open(os.path.join(FIXTURES, fixture), encoding="utf-8") as f:
            veredicto, clean_count, _ = validate_stream(f)
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
    for fixture in EXPECTED:
        path = os.path.join(FIXTURES, fixture)
        if not os.path.isfile(path):
            continue
        detect.reset_cache()
        with open(path, encoding="utf-8") as f:
            veredicto, _, _ = validate_stream(f)
        for h in veredicto.findings:
            serializado = json.dumps({"rule": h.rule, "path": h.path,
                                      "severity": h.severity, "line_no": h.line_no})
            if largos.search(serializado):
                fugas.append((fixture, serializado))
    ck(f"revisados los hallazgos de {len(EXPECTED)} fixtures", not fugas, fugas[:3])

    print("\n[5] el dedup por hash no altera el veredicto")
    detect.reset_cache()
    text = "un bloque system largo repetido " * 50
    first = detect.scan_text(text)
    segundo = detect.scan_text(text)
    ck("mismo resultado con y sin cache", first == segundo, (first, segundo))
    detect.reset_cache()
    ck("y tras limpiar la cache", detect.scan_text(text) == first)

    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
