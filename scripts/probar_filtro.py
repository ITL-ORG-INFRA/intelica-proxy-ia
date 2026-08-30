#!/usr/bin/env python3
"""Pasa lotes por el filtro SIN tocar AWS, y comprueba el resultado expected.

Corre el handler real del sanitizer contra un S3 y un DynamoDB simulados. No
reimplementa el filtro: importa el mismo codigo que corre en Lambda, con las
seis capas y el gate, asi que no puede divergir de produccion.

Cada carpeta de ejemplos/ lleva un manifiesto.json que declara que deberia
pasar con cada fichero. Esto lo verifica, asi que la bateria vale como prueba
de aceptacion y no solo como demostracion.

    ./scripts/probar_filtro.py                     todas las suites
    ./scripts/probar_filtro.py ejemplos/04-sad     una suite
    ./scripts/probar_filtro.py mi-lote.json        un fichero suelto
    ./scripts/probar_filtro.py --verbose ...       con todos los hallazgos

Devuelve 1 si algo no coincide con lo expected.
"""
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "tests"))

RAW, QUAR, CLEAN, RES = "sim-raw", "sim-quar", "sim-clean", "sim-res"

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-south-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "simulado")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "simulado")
os.environ.update({
    "RAW_BUCKET": RAW, "QUARANTINE_BUCKET": QUAR,
    "CLEAN_BUCKET": CLEAN, "RESULTS_BUCKET": RES,
    "BATCHES_TABLE": "sim-batches",
    "ANTHROPIC_SECRET_ARN": "arn:aws:secretsmanager:eu-south-2:0:secret:simulado",
    "LOG_LEVEL": "CRITICAL", "ENVIRONMENT": "local",
})
# Los mismos valores que el entorno real, salvo que se sobreescriban fuera.
os.environ.setdefault(
    "ALLOWED_MODELS", "claude-opus-4-5,claude-sonnet-4-5,claude-haiku-4-5-20251001")
os.environ.setdefault("GATE_REJECT_PCT", "1.0")
os.environ.setdefault("GATE_REJECT_ABS", "100")

if sys.stdout.isatty():
    R, B, D = "\033[0m", "\033[1m", "\033[2m"
    VERDE, ROJO, AMBAR, AZUL = "\033[32m", "\033[31m", "\033[33m", "\033[34m"
else:
    R = B = D = VERDE = ROJO = AMBAR = AZUL = ""


def suites_y_sueltos(rutas):
    """Separa lo que son carpetas con manifiesto de lo que son ficheros sueltos."""
    suites, sueltos = [], []
    for path in rutas:
        if os.path.isdir(path):
            manifest = os.path.join(path, "manifiesto.json")
            if os.path.isfile(manifest):
                suites.append(path)
            else:
                sueltos.extend(sorted(
                    os.path.join(path, f) for f in os.listdir(path)
                    if f.endswith(".json")))
        else:
            sueltos.append(path)
    return suites, sueltos


def compare(report, expected):
    """Devuelve la lista de discrepancias entre el report real y lo declarado."""
    problems = []

    if expected.get("expected") and report["status"] != expected["expected"]:
        problems.append(f"estado {report['status']!r}, se esperaba {expected['expected']!r}")

    for field in ("clean", "rejected"):
        if field in expected and report["request_counts"][field] != expected[field]:
            problems.append(
                f"{field}={report['request_counts'][field]}, se esperaba {expected[field]}")

    if "layers" in expected:
        seen = {int(k[5]) for k in report.get("summary_by_layer", {}) if k.startswith("layer")}
        missing = set(expected["layers"]) - seen
        if missing:
            problems.append(f"no disparo la layer {sorted(missing)}; disparo {sorted(seen)}")

    if expected.get("hard") and "bloqueo duro" not in report.get("reason", ""):
        problems.append("se esperaba bloqueo duro y el motivo no lo menciona")

    return problems


def show(report, detail):
    counts = report["request_counts"]
    brand = f"{VERDE}limpio{R}" if report["status"] == "clean" else f"{ROJO}cuarentena{R}"
    print(f"        {brand}  recibidas={counts['received']} "
          f"limpias={counts['clean']} rechazadas={counts['rejected']}")
    if report.get("reason"):
        print(f"        {D}{report['reason'][:100]}{R}")
    for layer, how_many in sorted(report.get("summary_by_layer", {}).items()):
        print(f"        {D}{layer}: {how_many}{R}")
    if detail:
        for rejection in report.get("rejections", []):
            for h in rejection.get("findings", []) or [{"detail": rejection.get("detail", "")}]:
                print(f"        {D}requests[{rejection.get('index')}] "
                      f"{h.get('type', '')} {h.get('detail', '')}{R}")


def main(argv):
    detail = "--verbose" in argv
    rutas = [a for a in argv if not a.startswith("--")]
    if not rutas:
        base = os.path.join(RAIZ, "ejemplos")
        if not os.path.isdir(base):
            print("no existe ejemplos/ y no diste ninguna ruta", file=sys.stderr)
            return 2
        rutas = sorted(os.path.join(base, d) for d in os.listdir(base)
                       if os.path.isdir(os.path.join(base, d)))

    suites, sueltos = suites_y_sueltos(rutas)

    from dobles import FakeCloudWatch, FakeResource, FakeS3, FakeTable
    s3, table_, cw = FakeS3(), FakeTable(), FakeCloudWatch()
    paquete = tempfile.mkdtemp()
    for sub in ("common", "sanitizer"):
        shutil.copytree(os.path.join(RAIZ, "src", sub), paquete, dirs_exist_ok=True)
    sys.path.insert(0, paquete)

    def fake_client(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw}[servicio]

    ok = fallidos = 0
    try:
        with mock.patch("boto3.client", side_effect=fake_client), \
             mock.patch("boto3.resource", return_value=FakeResource(table_)):
            import handler

            class Ctx:
                aws_request_id = "local"

                def get_remaining_time_in_millis(self):
                    return 900_000

            def run_layers(path, name):
                with open(path, "rb") as file_:
                    crudo = file_.read()
                key_ = f"input/{name}"
                s3.put_object(Bucket=RAW, Key=key_, Body=crudo)
                event = {"source": "aws.s3",
                          "detail": {"bucket": {"name": RAW},
                                     "object": {"key": key_, "etag": name}}}
                result = handler.lambda_handler(event, Ctx())
                return json.loads(
                    s3.objetos[(CLEAN, f"status/{result['batch_id']}.json")])

            for folder in suites:
                with open(os.path.join(folder, "manifiesto.json"), encoding="utf-8") as f:
                    manifest = json.load(f)
                print(f"\n{AZUL}==>{R} {B}{manifest['suite']}{R}")
                print(f"    {D}{manifest['description']}{R}")

                for case in manifest["cases"]:
                    path = os.path.join(folder, case["file"])
                    try:
                        report = run_layers(path, f"{os.path.basename(folder)}-{case['file']}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"    {ROJO}✗{R} {case['file']:32} excepcion: {exc}")
                        fallidos += 1
                        continue

                    discrepancias = compare(report, case)
                    if discrepancias:
                        print(f"    {ROJO}✗{R} {case['file']:32} {case['why']}")
                        for d in discrepancias:
                            print(f"        {ROJO}{d}{R}")
                        show(report, True)
                        fallidos += 1
                    else:
                        print(f"    {VERDE}✓{R} {case['file']:32} {case['why']}")
                        if detail:
                            show(report, True)

            for path in sueltos:
                name = os.path.basename(path)
                print(f"\n{AZUL}==>{R} {B}{name}{R}")
                report = run_layers(path, name)
                show(report, True)
                for consejo in report.get("what_to_do", []):
                    print(f"        {AMBAR}→{R} {consejo}")
    finally:
        shutil.rmtree(paquete, ignore_errors=True)

    if suites:
        total = ok + fallidos
        print(f"\n{AZUL}==>{R} {B}Resumen{R}")
        cases = sum(len(json.load(open(os.path.join(c, "manifiesto.json"),
                                      encoding="utf-8"))["cases"]) for c in suites)
        print(f"    {VERDE}{cases - fallidos} de {cases} como se esperaba{R}"
              + (f" · {ROJO}{fallidos} discrepancias{R}" if fallidos else ""))
        print()
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
