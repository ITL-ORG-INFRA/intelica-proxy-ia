#!/usr/bin/env python3
"""Pasa un lote por el filtro SIN tocar AWS.

Corre el handler real del sanitizer contra un S3 y un DynamoDB simulados en
memoria. No es una reimplementacion del filtro: es el mismo codigo que corre en
Lambda, con las seis capas y el gate, asi que no puede divergir de produccion.

Sirve para iterar en segundos en vez de en minutos, y para meterlo en un
pre-commit si algun equipo quiere validar sus lotes antes de subirlos.

    ./scripts/probar_filtro.py                          todos los ejemplos
    ./scripts/probar_filtro.py mi-lote.json             uno tuyo
    ./scripts/probar_filtro.py --detalle lote.json      con todos los hallazgos

Devuelve 1 si algun lote acaba en cuarentena.
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
    "LOG_LEVEL": "CRITICAL",
    "ENVIRONMENT": "local",
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


def main(argv):
    detalle = "--detalle" in argv
    ficheros = [a for a in argv if not a.startswith("--")]

    if not ficheros:
        carpeta = os.path.join(RAIZ, "ejemplos")
        if not os.path.isdir(carpeta):
            print("no hay ejemplos/ y no diste ningun fichero", file=sys.stderr)
            return 2
        ficheros = sorted(os.path.join(carpeta, f)
                          for f in os.listdir(carpeta) if f.endswith(".json"))

    from dobles import FakeCloudWatch, FakeResource, FakeS3, FakeTable

    s3, tabla, cw = FakeS3(), FakeTable(), FakeCloudWatch()
    paquete = tempfile.mkdtemp()
    for sub in ("common", "sanitizer"):
        shutil.copytree(os.path.join(RAIZ, "src", sub), paquete, dirs_exist_ok=True)
    sys.path.insert(0, paquete)

    def cliente(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw}[servicio]

    limpios = cuarentena = 0
    try:
        with mock.patch("boto3.client", side_effect=cliente), \
             mock.patch("boto3.resource", return_value=FakeResource(tabla)):
            import handler

            class Ctx:
                aws_request_id = "local"

                def get_remaining_time_in_millis(self):
                    return 900_000

            for ruta in ficheros:
                nombre = os.path.basename(ruta)
                try:
                    with open(ruta, "rb") as fichero:
                        crudo = fichero.read()
                    documento = json.loads(crudo)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"\n{AZUL}==>{R} {B}{nombre}{R}")
                    print(f"    {ROJO}✗{R} no se pudo leer: {exc}")
                    cuarentena += 1
                    continue

                print(f"\n{AZUL}==>{R} {B}{nombre}{R}")
                caso = (documento.get("metadata") or {}).get("caso")
                if caso:
                    print(f"    {D}·{R} {caso}")
                n = len(documento.get("requests", []))
                print(f"    {D}·{R} peticiones: {n}")

                clave = f"entrada/{nombre}"
                s3.put_object(Bucket=RAW, Key=clave, Body=crudo)
                evento = {"source": "aws.s3",
                          "detail": {"bucket": {"name": RAW},
                                     "object": {"key": clave, "etag": nombre}}}
                resultado = handler.lambda_handler(evento, Ctx())
                lote = resultado["batch_id"]

                parte = json.loads(s3.objetos[(CLEAN, f"estado/{lote}.json")])
                _imprimir(parte, detalle)
                if parte["estado"] == "limpio":
                    limpios += 1
                else:
                    cuarentena += 1
    finally:
        shutil.rmtree(paquete, ignore_errors=True)

    print(f"\n{AZUL}==>{R} {B}Resumen{R}")
    print(f"    {VERDE}{limpios} limpios{R} · {ROJO}{cuarentena} en cuarentena{R}\n")
    return 1 if cuarentena else 0


def _imprimir(parte, detalle):
    conteos = parte["peticiones"]
    if parte["estado"] == "limpio":
        print(f"    {VERDE}✓ LIMPIO{R}  {json.dumps(conteos)}")
    else:
        print(f"    {ROJO}✗ CUARENTENA{R}  {json.dumps(conteos)}")
        print(f"      motivo: {parte['motivo']}")

    for capa, cuantos in sorted(parte.get("resumen_por_capa", {}).items()):
        print(f"      {capa}: {cuantos}")

    rechazos = parte.get("rechazos", [])
    for rechazo in rechazos if detalle else rechazos[:4]:
        indice = rechazo.get("indice")
        if rechazo.get("hallazgos"):
            for h in rechazo["hallazgos"]:
                print(f"      requests[{indice}] capa {h['capa']} {h['tipo']} "
                      f"— {h['detalle']} en {h['donde']}")
        else:
            print(f"      requests[{indice}] {rechazo.get('detalle', '')}")
    if not detalle and len(rechazos) > 4:
        print(f"      {D}… y {len(rechazos) - 4} mas (usa --detalle){R}")

    for consejo in parte.get("que_hacer", []):
        print(f"      {AMBAR}→{R} {consejo}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
