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

FAILURES = []
BATCH = "input/lote-2026-08-27"


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


def build_event(*folders):
    target = tempfile.mkdtemp()
    shutil.copytree(os.path.join(REPO, "src", "common"), target, dirs_exist_ok=True)
    for folder in folders:
        shutil.copytree(os.path.join(REPO, "src", folder), target, dirs_exist_ok=True)
    return target


class Ctx:
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 900_000


def s3_event(bucket, key):
    return {"source": "aws.s3", "detail-type": "Object Created",
            "detail": {"bucket": {"name": bucket},
                       "object": {"key": key, "etag": "etag-" + key}}}


def part(name, texts):
    # Sin puntos: el envelope exige custom_id alfanumerico, guion o guion bajo.
    clean_text = name.replace(".json", "").replace(".", "-")
    return json.dumps({"requests": [
        {"custom_id": f"{clean_text}-{i}", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 64,
            "messages": [{"role": "user", "content": t}]}}
        for i, t in enumerate(texts)]}).encode()


def manifest(files, total=0):
    return json.dumps({"batch": BATCH.rsplit("/", 1)[-1],
                       "files": files, "total_requests": total}).encode()


def main():
    s3, table_, cw = FakeS3(), FakeTable(), FakeCloudWatch()
    claves_vistas = []

    class FakeSecrets:
        def get_secret_value(self, SecretId, **_kw):
            return {"SecretString": json.dumps({"api_key": "sk-ant-simulada"})}

    def fake_client(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw, "secretsmanager": FakeSecrets()}[servicio]

    pkg_san = build_event("sanitizer")
    pkg_sub = build_event("submitter")

    submitted = []

    def create_fake_batch(requests):
        submitted.append(requests)
        return ({"id": f"msgbatch_{len(submitted)}", "expires_at": "2026-08-28T00:00:00Z"},
                {"anthropic-ratelimit-requests-remaining": "999"})

    with mock.patch("boto3.client", side_effect=fake_client), \
         mock.patch("boto3.resource", return_value=FakeResource(table_)):

        sys.path.insert(0, pkg_san)
        import handler as sanitizer
        import store

        def sanitizer_key_intact(key_):
            """Devuelve la clave tal como la vio el sanitizer."""
            bucket, key, _etag = sanitizer._source(s3_event(RAW, key_))
            return key

        print("\n[1] el sanitizer ignora el manifiesto")
        s3.put_object(Bucket=RAW, Key=f"{BATCH}/_MANIFEST.json",
                      Body=manifest(["parte-01.json"]))
        r = sanitizer.lambda_handler(s3_event(RAW, f"{BATCH}/_MANIFEST.json"), Ctx())
        ck("no lo trata como lote", r.get("skipped") == "es un manifiesto", r)
        ck("no lo mando a cuarentena", len(s3.keys_of(QUAR)) == 0, s3.keys_of(QUAR))

        print("\n[2] cada parte sanitizada se cuenta una vez")
        for name in ("parte-01.json", "parte-02.json"):
            s3.put_object(Bucket=RAW, Key=f"{BATCH}/{name}",
                          Body=part(name, ["Resume el expediente", "Clasifica el caso"]))
            sanitizer.lambda_handler(s3_event(RAW, f"{BATCH}/{name}"), Ctx())

        state = store.batch_state(BATCH) or {}
        ck("2 partes limpias contadas", state.get("clean_parts") == 2, state)

        # Reentrega: S3 es at-least-once y el contador no puede inflarse.
        sanitizer.lambda_handler(s3_event(RAW, f"{BATCH}/parte-01.json"), Ctx())
        state = store.batch_state(BATCH) or {}
        ck("una reentrega NO infla el contador", state.get("clean_parts") == 2,
           state.get("clean_parts"))

        # --- ahora el submitter ---
        for modulo in ("handler", "store", "config", "logs", "detectors", "normalize",
                       "envelope", "secret_store", "anthropic_batches"):
            sys.modules.pop(modulo, None)
        sys.path.remove(pkg_san)
        sys.path.insert(0, pkg_sub)

        import handler as submitter
        import store as store2
        submitter.create_batch = create_fake_batch

        print("\n[3] manifiesto con todas las partes listas -> envia")
        # El manifiesto de [1] listaba una sola parte; ahora se sube el real,
        # con las dos que el sanitizer ya proceso.
        s3.put_object(Bucket=RAW, Key=f"{BATCH}/_MANIFEST.json",
                      Body=manifest(["parte-01.json", "parte-02.json"], 4))
        r = submitter.lambda_handler(s3_event(RAW, f"{BATCH}/_MANIFEST.json"), Ctx())
        ck("estado enviado", r.get("status") == "submitted", r)
        ck("4 peticiones fusionadas", r.get("requests") == 4, r)
        ck("una sola llamada a Anthropic", len(submitted) == 1, len(submitted))

        print("\n[4] el segundo camino NO reenvia")
        # Aqui esta la carrera: el barrido corre despues del evento. Sin la
        # reclamacion condicional, el lote se enviaria dos veces.
        r2 = submitter.lambda_handler(s3_event(RAW, f"{BATCH}/_MANIFEST.json"), Ctx())
        ck("el reintento no envia", r2.get("status") != "submitted", r2)
        ck("sigue habiendo UNA sola llamada", len(submitted) == 1, len(submitted))
        barrido = submitter.sweep_pending()
        ck("el barrido tampoco reenvia", len(submitted) == 1, barrido)

        print("\n[5] manifiesto que llega ANTES de que el sanitizer acabe")
        OTHER = "input/lote-parcial"
        s3.put_object(Bucket=RAW, Key=f"{OTHER}/_MANIFEST.json",
                      Body=manifest(["a.json", "b.json"]))
        r = submitter.lambda_handler(s3_event(RAW, f"{OTHER}/_MANIFEST.json"), Ctx())
        ck("queda esperando partes", r.get("status") == "awaiting_parts", r)
        ck("no envia nada", len(submitted) == 1, len(submitted))
        ck("dice cuantas faltan", r.get("expected") == 2 and r.get("clean", 0) == 0, r)

        print("\n[6] y el barrido lo recoge cuando las partes llegan")
        for name, texts in (("a.json", ["uno"]), ("b.json", ["dos"])):
            clean_key = f"clean/{OTHER}/{name}"
            store2.record_part(OTHER, f"{OTHER}/{name}", cleaned=True,
                                   clean_key=clean_key)
            s3.put_object(Bucket=CLEAN, Key=clean_key, Body=part(name, texts))
        barrido = submitter.sweep_pending()
        ck("el barrido lo envia", barrido["submitted"] == 1, barrido)
        ck("ya van dos llamadas", len(submitted) == 2, len(submitted))

        print("\n[7] una parte en cuarentena tumba el lote entero")
        BAD = "input/lote-sucio"
        s3.put_object(Bucket=RAW, Key=f"{BAD}/_MANIFEST.json",
                      Body=manifest(["ok.json", "malo.json"]))
        store2.record_part(BAD, f"{BAD}/ok.json", cleaned=True)
        store2.record_part(BAD, f"{BAD}/malo.json", cleaned=False)
        r = submitter.lambda_handler(s3_event(RAW, f"{BAD}/_MANIFEST.json"), Ctx())
        ck("lote en cuarentena", r.get("status") == "quarantined", r)
        ck("no se envio la parte limpia", len(submitted) == 2, len(submitted))

        print("\n[8] custom_id duplicado entre partes")
        DUP = "input/lote-dup"
        s3.put_object(Bucket=RAW, Key=f"{DUP}/_MANIFEST.json",
                      Body=manifest(["x.json", "y.json"]))
        store2.record_part(DUP, f"{DUP}/x.json", cleaned=True,
                               clean_key=f"clean/{DUP}/x.json")
        store2.record_part(DUP, f"{DUP}/y.json", cleaned=True,
                               clean_key=f"clean/{DUP}/y.json")
        # Las dos partes traen el MISMO custom_id: Anthropic rechazaria el POST
        # entero sin decir cual, asi que hay que detectarlo antes y nombrarlo.
        mismo = json.dumps({"requests": [{"custom_id": "colision", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hola"}]}}]}).encode()
        s3.put_object(Bucket=CLEAN, Key=f"clean/{DUP}/x.json", Body=mismo)
        s3.put_object(Bucket=CLEAN, Key=f"clean/{DUP}/y.json", Body=mismo)
        r = submitter.lambda_handler(s3_event(RAW, f"{DUP}/_MANIFEST.json"), Ctx())
        ck("lote fallido", r.get("status") == "failed", r)
        ck("nombra el id duplicado", r.get("custom_id_duplicado") == "colision", r)
        ck("no se envio", len(submitted) == 2, len(submitted))

        print("\n[9] claves con '+': EventBridge NO las codifica")
        # Regresion. Se aplicaba unquote_plus a la clave de EventBridge, que
        # llega sin codificar: un '+' literal —valido en S3— se convertia en
        # espacio, la clave dejaba de existir y el lote moria con un 404 que
        # apuntaba a un fichero que nadie habia subido.
        MORE = "input/lote+2026/parte-01.json"
        s3.put_object(Bucket=RAW, Key=MORE, Body=part("mas", ["hola"]))
        r = sanitizer_key_intact(MORE)
        ck("el sanitizer conserva el '+'", r == MORE, r)

        MORE_MAN = "input/lote+2026/_MANIFEST.json"
        s3.put_object(Bucket=RAW, Key=MORE_MAN, Body=manifest(["parte-01.json"]))
        r = submitter.lambda_handler(s3_event(RAW, MORE_MAN), Ctx())
        ck("el submitter no corrompe el lote",
           r.get("batch") == "input/lote+2026", r)
        ck("y NO dice que el manifiesto es ilegible",
           r.get("error") != "manifiesto ilegible", r)

        print("\n[10] la notificacion nativa de S3 SI se decodifica")
        # Formato Records: ahi S3 codifica el espacio como '+', asi que hay que
        # deshacerlo. Los dos formatos necesitan tratos opuestos.
        evento_records = {"Records": [{"s3": {
            "bucket": {"name": RAW},
            "object": {"key": "input/lote+2026/parte-01.json", "eTag": "e"}}}]}
        ck("Records: '+' se convierte en espacio",
           submitter._key_from_event(evento_records) == "input/lote 2026/parte-01.json",
           submitter._key_from_event(evento_records))
        ck("EventBridge: '+' se queda",
           submitter._key_from_event(s3_event(RAW, MORE_MAN)) == MORE_MAN,
           submitter._key_from_event(s3_event(RAW, MORE_MAN)))

        print("\n[11] manifiesto ilegible")
        BROKEN = "input/lote-roto"
        s3.put_object(Bucket=RAW, Key=f"{BROKEN}/_MANIFEST.json", Body=b"{no es json")
        r = submitter.lambda_handler(s3_event(RAW, f"{BROKEN}/_MANIFEST.json"), Ctx())
        ck("lo marca fallido sin reventar", r.get("error") == "manifiesto ilegible", r)

    shutil.rmtree(pkg_san, ignore_errors=True)
    shutil.rmtree(pkg_sub, ignore_errors=True)
    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
