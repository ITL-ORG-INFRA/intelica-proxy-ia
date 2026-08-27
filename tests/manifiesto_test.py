"""El flujo disparado por manifiesto, sobre S3 y DynamoDB simulados.

Lo que importa comprobar aqui no es el camino feliz, es que los DOS caminos
—el evento del manifiesto y el barrido programado— convergen sin enviar el
lote dos veces, y que un lote incompleto espera en vez de enviarse a medias.

    .venv/bin/python tests/manifiesto_test.py
"""
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))

from dobles import FakeCloudWatch, FakeResource, FakeS3, FakeTable  # noqa: E402

RAW, QUAR, CLEAN, RES = "b-raw", "b-quar", "b-clean", "b-res"

os.environ.update({
    "AWS_DEFAULT_REGION": "eu-south-2",
    "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
    "RAW_BUCKET": RAW, "QUARANTINE_BUCKET": QUAR,
    "CLEAN_BUCKET": CLEAN, "RESULTS_BUCKET": RES,
    "BATCHES_TABLE": "t-batches",
    "ANTHROPIC_SECRET_ARN": "arn:aws:secretsmanager:eu-south-2:1:secret:x",
    "ALLOWED_MODELS": "claude-sonnet-4-5",
    "GATE_REJECT_PCT": "1.0", "GATE_REJECT_ABS": "100",
    "INFLIGHT_LIMIT": "100000", "ENVIRONMENT": "test", "LOG_LEVEL": "ERROR",
})

FALLOS = []
LOTE = "entrada/lote-2026-08-27"


def ck(nombre, condicion, detalle=""):
    print(("  OK   " if condicion else "  FALLA ") + nombre
          + ("" if condicion else f"  <- {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


def montar(*carpetas):
    destino = tempfile.mkdtemp()
    shutil.copytree(os.path.join(REPO, "src", "common"), destino, dirs_exist_ok=True)
    for carpeta in carpetas:
        shutil.copytree(os.path.join(REPO, "src", carpeta), destino, dirs_exist_ok=True)
    return destino


class Ctx:
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 900_000


def evento_s3(bucket, key):
    return {"source": "aws.s3", "detail-type": "Object Created",
            "detail": {"bucket": {"name": bucket},
                       "object": {"key": key, "etag": "etag-" + key}}}


def parte(nombre, textos):
    # Sin puntos: el envelope exige custom_id alfanumerico, guion o guion bajo.
    limpio = nombre.replace(".json", "").replace(".", "-")
    return json.dumps({"requests": [
        {"custom_id": f"{limpio}-{i}", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 64,
            "messages": [{"role": "user", "content": t}]}}
        for i, t in enumerate(textos)]}).encode()


def manifiesto(ficheros, total=0):
    return json.dumps({"lote": LOTE.rsplit("/", 1)[-1],
                       "files": ficheros, "total_requests": total}).encode()


def main():
    s3, tabla, cw = FakeS3(), FakeTable(), FakeCloudWatch()

    class FakeSecretos:
        def get_secret_value(self, SecretId, **_kw):
            return {"SecretString": json.dumps({"api_key": "sk-ant-simulada"})}

    def cliente(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw, "secretsmanager": FakeSecretos()}[servicio]

    pkg_san = montar("sanitizer")
    pkg_sub = montar("submitter")

    enviados = []

    def crear_lote_falso(peticiones):
        enviados.append(peticiones)
        return ({"id": f"msgbatch_{len(enviados)}", "expires_at": "2026-08-28T00:00:00Z"},
                {"anthropic-ratelimit-requests-remaining": "999"})

    with mock.patch("boto3.client", side_effect=cliente), \
         mock.patch("boto3.resource", return_value=FakeResource(tabla)):

        sys.path.insert(0, pkg_san)
        import handler as sanitizer
        import store

        print("\n[1] el sanitizer ignora el manifiesto")
        s3.put_object(Bucket=RAW, Key=f"{LOTE}/_MANIFEST.json",
                      Body=manifiesto(["parte-01.json"]))
        r = sanitizer.lambda_handler(evento_s3(RAW, f"{LOTE}/_MANIFEST.json"), Ctx())
        ck("no lo trata como lote", r.get("omitido") == "es un manifiesto", r)
        ck("no lo mando a cuarentena", len(s3.claves(QUAR)) == 0, s3.claves(QUAR))

        print("\n[2] cada parte sanitizada se cuenta una vez")
        for nombre in ("parte-01.json", "parte-02.json"):
            s3.put_object(Bucket=RAW, Key=f"{LOTE}/{nombre}",
                          Body=parte(nombre, ["Resume el expediente", "Clasifica el caso"]))
            sanitizer.lambda_handler(evento_s3(RAW, f"{LOTE}/{nombre}"), Ctx())

        estado = store.estado_lote(LOTE) or {}
        ck("2 partes limpias contadas", estado.get("partes_limpias") == 2, estado)

        # Reentrega: S3 es at-least-once y el contador no puede inflarse.
        sanitizer.lambda_handler(evento_s3(RAW, f"{LOTE}/parte-01.json"), Ctx())
        estado = store.estado_lote(LOTE) or {}
        ck("una reentrega NO infla el contador", estado.get("partes_limpias") == 2,
           estado.get("partes_limpias"))

        # --- ahora el submitter ---
        for modulo in ("handler", "store", "config", "logs", "detectors", "normalize",
                       "envelope", "secret_store", "anthropic_batches"):
            sys.modules.pop(modulo, None)
        sys.path.remove(pkg_san)
        sys.path.insert(0, pkg_sub)

        import handler as submitter
        import store as store2
        submitter.crear_lote = crear_lote_falso

        print("\n[3] manifiesto con todas las partes listas -> envia")
        # El manifiesto de [1] listaba una sola parte; ahora se sube el real,
        # con las dos que el sanitizer ya proceso.
        s3.put_object(Bucket=RAW, Key=f"{LOTE}/_MANIFEST.json",
                      Body=manifiesto(["parte-01.json", "parte-02.json"], 4))
        r = submitter.lambda_handler(evento_s3(RAW, f"{LOTE}/_MANIFEST.json"), Ctx())
        ck("estado enviado", r.get("estado") == "enviado", r)
        ck("4 peticiones fusionadas", r.get("peticiones") == 4, r)
        ck("una sola llamada a Anthropic", len(enviados) == 1, len(enviados))

        print("\n[4] el segundo camino NO reenvia")
        # Aqui esta la carrera: el barrido corre despues del evento. Sin la
        # reclamacion condicional, el lote se enviaria dos veces.
        r2 = submitter.lambda_handler(evento_s3(RAW, f"{LOTE}/_MANIFEST.json"), Ctx())
        ck("el reintento no envia", r2.get("estado") != "enviado", r2)
        ck("sigue habiendo UNA sola llamada", len(enviados) == 1, len(enviados))
        barrido = submitter.barrer_pendientes()
        ck("el barrido tampoco reenvia", len(enviados) == 1, barrido)

        print("\n[5] manifiesto que llega ANTES de que el sanitizer acabe")
        OTRO = "entrada/lote-parcial"
        s3.put_object(Bucket=RAW, Key=f"{OTRO}/_MANIFEST.json",
                      Body=manifiesto(["a.json", "b.json"]))
        r = submitter.lambda_handler(evento_s3(RAW, f"{OTRO}/_MANIFEST.json"), Ctx())
        ck("queda esperando partes", r.get("estado") == "esperando_partes", r)
        ck("no envia nada", len(enviados) == 1, len(enviados))
        ck("dice cuantas faltan", r.get("esperadas") == 2 and r.get("limpias", 0) == 0, r)

        print("\n[6] y el barrido lo recoge cuando las partes llegan")
        for nombre, textos in (("a.json", ["uno"]), ("b.json", ["dos"])):
            clean_key = f"clean/{OTRO}/{nombre}"
            store2.registrar_parte(OTRO, f"{OTRO}/{nombre}", limpia=True,
                                   clean_key=clean_key)
            s3.put_object(Bucket=CLEAN, Key=clean_key, Body=parte(nombre, textos))
        barrido = submitter.barrer_pendientes()
        ck("el barrido lo envia", barrido["enviados"] == 1, barrido)
        ck("ya van dos llamadas", len(enviados) == 2, len(enviados))

        print("\n[7] una parte en cuarentena tumba el lote entero")
        MALO = "entrada/lote-sucio"
        s3.put_object(Bucket=RAW, Key=f"{MALO}/_MANIFEST.json",
                      Body=manifiesto(["ok.json", "malo.json"]))
        store2.registrar_parte(MALO, f"{MALO}/ok.json", limpia=True)
        store2.registrar_parte(MALO, f"{MALO}/malo.json", limpia=False)
        r = submitter.lambda_handler(evento_s3(RAW, f"{MALO}/_MANIFEST.json"), Ctx())
        ck("lote en cuarentena", r.get("estado") == "cuarentena", r)
        ck("no se envio la parte limpia", len(enviados) == 2, len(enviados))

        print("\n[8] custom_id duplicado entre partes")
        DUP = "entrada/lote-dup"
        s3.put_object(Bucket=RAW, Key=f"{DUP}/_MANIFEST.json",
                      Body=manifiesto(["x.json", "y.json"]))
        store2.registrar_parte(DUP, f"{DUP}/x.json", limpia=True,
                               clean_key=f"clean/{DUP}/x.json")
        store2.registrar_parte(DUP, f"{DUP}/y.json", limpia=True,
                               clean_key=f"clean/{DUP}/y.json")
        # Las dos partes traen el MISMO custom_id: Anthropic rechazaria el POST
        # entero sin decir cual, asi que hay que detectarlo antes y nombrarlo.
        mismo = json.dumps({"requests": [{"custom_id": "colision", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hola"}]}}]}).encode()
        s3.put_object(Bucket=CLEAN, Key=f"clean/{DUP}/x.json", Body=mismo)
        s3.put_object(Bucket=CLEAN, Key=f"clean/{DUP}/y.json", Body=mismo)
        r = submitter.lambda_handler(evento_s3(RAW, f"{DUP}/_MANIFEST.json"), Ctx())
        ck("lote fallido", r.get("estado") == "fallido", r)
        ck("nombra el id duplicado", r.get("custom_id_duplicado") == "colision", r)
        ck("no se envio", len(enviados) == 2, len(enviados))

        print("\n[9] manifiesto ilegible")
        ROTO = "entrada/lote-roto"
        s3.put_object(Bucket=RAW, Key=f"{ROTO}/_MANIFEST.json", Body=b"{no es json")
        r = submitter.lambda_handler(evento_s3(RAW, f"{ROTO}/_MANIFEST.json"), Ctx())
        ck("lo marca fallido sin reventar", r.get("error") == "manifiesto ilegible", r)

    shutil.rmtree(pkg_san, ignore_errors=True)
    shutil.rmtree(pkg_sub, ignore_errors=True)
    print("\n" + ("TODO OK" if not FALLOS else f"{len(FALLOS)} FALLOS: {FALLOS}"))
    return 1 if FALLOS else 0


if __name__ == "__main__":
    sys.exit(main())
