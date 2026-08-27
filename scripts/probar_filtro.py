#!/usr/bin/env python3
"""Pasa lotes por el filtro SIN tocar AWS, y comprueba el resultado esperado.

Corre el handler real del sanitizer contra un S3 y un DynamoDB simulados. No
reimplementa el filtro: importa el mismo codigo que corre en Lambda, con las
seis capas y el gate, asi que no puede divergir de produccion.

Cada carpeta de ejemplos/ lleva un manifiesto.json que declara que deberia
pasar con cada fichero. Esto lo verifica, asi que la bateria vale como prueba
de aceptacion y no solo como demostracion.

    ./scripts/probar_filtro.py                     todas las suites
    ./scripts/probar_filtro.py ejemplos/04-sad     una suite
    ./scripts/probar_filtro.py mi-lote.json        un fichero suelto
    ./scripts/probar_filtro.py --detalle ...       con todos los hallazgos

Devuelve 1 si algo no coincide con lo esperado.
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
    for ruta in rutas:
        if os.path.isdir(ruta):
            manifiesto = os.path.join(ruta, "manifiesto.json")
            if os.path.isfile(manifiesto):
                suites.append(ruta)
            else:
                sueltos.extend(sorted(
                    os.path.join(ruta, f) for f in os.listdir(ruta)
                    if f.endswith(".json")))
        else:
            sueltos.append(ruta)
    return suites, sueltos


def comparar(parte, esperado):
    """Devuelve la lista de discrepancias entre el parte real y lo declarado."""
    fallos = []

    if esperado.get("esperado") and parte["estado"] != esperado["esperado"]:
        fallos.append(f"estado {parte['estado']!r}, se esperaba {esperado['esperado']!r}")

    for campo in ("limpias", "rechazadas"):
        if campo in esperado and parte["peticiones"][campo] != esperado[campo]:
            fallos.append(
                f"{campo}={parte['peticiones'][campo]}, se esperaba {esperado[campo]}")

    if "capas" in esperado:
        vistas = {int(k[4]) for k in parte.get("resumen_por_capa", {}) if k.startswith("capa")}
        faltan = set(esperado["capas"]) - vistas
        if faltan:
            fallos.append(f"no disparo la capa {sorted(faltan)}; disparo {sorted(vistas)}")

    if esperado.get("duro") and "bloqueo duro" not in parte.get("motivo", ""):
        fallos.append("se esperaba bloqueo duro y el motivo no lo menciona")

    return fallos


def imprimir(parte, detalle):
    conteos = parte["peticiones"]
    marca = f"{VERDE}limpio{R}" if parte["estado"] == "limpio" else f"{ROJO}cuarentena{R}"
    print(f"        {marca}  recibidas={conteos['recibidas']} "
          f"limpias={conteos['limpias']} rechazadas={conteos['rechazadas']}")
    if parte.get("motivo"):
        print(f"        {D}{parte['motivo'][:100]}{R}")
    for capa, cuantos in sorted(parte.get("resumen_por_capa", {}).items()):
        print(f"        {D}{capa}: {cuantos}{R}")
    if detalle:
        for rechazo in parte.get("rechazos", []):
            for h in rechazo.get("hallazgos", []) or [{"detalle": rechazo.get("detalle", "")}]:
                print(f"        {D}requests[{rechazo.get('indice')}] "
                      f"{h.get('tipo', '')} {h.get('detalle', '')}{R}")


def main(argv):
    detalle = "--detalle" in argv
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
    s3, tabla, cw = FakeS3(), FakeTable(), FakeCloudWatch()
    paquete = tempfile.mkdtemp()
    for sub in ("common", "sanitizer"):
        shutil.copytree(os.path.join(RAIZ, "src", sub), paquete, dirs_exist_ok=True)
    sys.path.insert(0, paquete)

    def cliente(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw}[servicio]

    ok = fallidos = 0
    try:
        with mock.patch("boto3.client", side_effect=cliente), \
             mock.patch("boto3.resource", return_value=FakeResource(tabla)):
            import handler

            class Ctx:
                aws_request_id = "local"

                def get_remaining_time_in_millis(self):
                    return 900_000

            def pasar(ruta, nombre):
                with open(ruta, "rb") as fichero:
                    crudo = fichero.read()
                clave = f"entrada/{nombre}"
                s3.put_object(Bucket=RAW, Key=clave, Body=crudo)
                evento = {"source": "aws.s3",
                          "detail": {"bucket": {"name": RAW},
                                     "object": {"key": clave, "etag": nombre}}}
                resultado = handler.lambda_handler(evento, Ctx())
                return json.loads(
                    s3.objetos[(CLEAN, f"estado/{resultado['batch_id']}.json")])

            for carpeta in suites:
                with open(os.path.join(carpeta, "manifiesto.json"), encoding="utf-8") as f:
                    manifiesto = json.load(f)
                print(f"\n{AZUL}==>{R} {B}{manifiesto['suite']}{R}")
                print(f"    {D}{manifiesto['descripcion']}{R}")

                for caso in manifiesto["casos"]:
                    ruta = os.path.join(carpeta, caso["fichero"])
                    try:
                        parte = pasar(ruta, f"{os.path.basename(carpeta)}-{caso['fichero']}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"    {ROJO}✗{R} {caso['fichero']:32} excepcion: {exc}")
                        fallidos += 1
                        continue

                    discrepancias = comparar(parte, caso)
                    if discrepancias:
                        print(f"    {ROJO}✗{R} {caso['fichero']:32} {caso['por_que']}")
                        for d in discrepancias:
                            print(f"        {ROJO}{d}{R}")
                        imprimir(parte, True)
                        fallidos += 1
                    else:
                        print(f"    {VERDE}✓{R} {caso['fichero']:32} {caso['por_que']}")
                        if detalle:
                            imprimir(parte, True)

            for ruta in sueltos:
                nombre = os.path.basename(ruta)
                print(f"\n{AZUL}==>{R} {B}{nombre}{R}")
                parte = pasar(ruta, nombre)
                imprimir(parte, True)
                for consejo in parte.get("que_hacer", []):
                    print(f"        {AMBAR}→{R} {consejo}")
    finally:
        shutil.rmtree(paquete, ignore_errors=True)

    if suites:
        total = ok + fallidos
        print(f"\n{AZUL}==>{R} {B}Resumen{R}")
        casos = sum(len(json.load(open(os.path.join(c, "manifiesto.json"),
                                      encoding="utf-8"))["casos"]) for c in suites)
        print(f"    {VERDE}{casos - fallidos} de {casos} como se esperaba{R}"
              + (f" · {ROJO}{fallidos} discrepancias{R}" if fallidos else ""))
        print()
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
