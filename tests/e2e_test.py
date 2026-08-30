"""Prueba de extremo a extremo del proxy, sin tocar AWS ni Anthropic.

Monta cada Lambda igual que lo hace deploy/deploy.sh (src/common + su carpeta,
en plano) y hace correr el pipeline completo sobre S3 y DynamoDB simulados.

    python3 -m venv .venv
    .venv/bin/pip install -r layer/requirements.txt boto3
    .venv/bin/python tests/e2e_test.py
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
    "ALLOWED_MODELS": "claude-sonnet-4-5,claude-haiku-4-5-20251001",
    "GATE_REJECT_PCT": "1.0", "GATE_REJECT_ABS": "100",
    "INFLIGHT_LIMIT": "1000", "ENVIRONMENT": "test", "LOG_LEVEL": "ERROR",
})

FAILURES = []


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


def build_event(*folders):
    """Replica el empaquetado de deploy.sh."""
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
                       "object": {"key": key, "etag": "etag-simulado"}}}


def request(cid, text, modelo="claude-sonnet-4-5"):
    return {"custom_id": cid, "params": {
        "model": modelo, "max_tokens": 256,
        "messages": [{"role": "user", "content": text}]}}


def main():
    s3, table_, cw = FakeS3(), FakeTable(), FakeCloudWatch()

    def fake_client(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw}[servicio]

    pkg_san = build_event("sanitizer")
    pkg_ver = build_event("sanitizer", "verifier")
    sys.path.insert(0, pkg_san)

    with mock.patch("boto3.client", side_effect=fake_client), \
         mock.patch("boto3.resource", return_value=FakeResource(table_)):
        import handler as sanitizer
        import store

        print("\n[1] lote limpio -> cruza a la zona limpia")
        doc = {"requests": [request(f"fila-{i}", f"Resume el documento {i}")
                            for i in range(5)]}
        s3.put_object(Bucket=RAW, Key="input/limpio.json",
                      Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "input/limpio.json"), Ctx())
        ck("estado limpio", r["status"] == store.Status.CLEAN_, r)
        ck("5 peticiones limpias", r["clean"] == 5, r)
        ck("nada en cuarentena", len(s3.keys_of(QUAR)) == 0, s3.keys_of(QUAR))
        clean_batch = r["batch_id"]

        clean_keys = s3.keys_of(CLEAN)
        ck("un solo lote en clean/",
           [k for k in clean_keys if k.startswith("clean/")] == [f"clean/{clean_batch}.json"],
           clean_keys)
        ck("el lote escrito en clean/", f"clean/{clean_batch}.json" in clean_keys,
           clean_keys)
        ck("parte de estado escrito en estado/",
           f"status/{clean_batch}.json" in clean_keys, clean_keys)

        part = json.loads(s3.objetos[(CLEAN, f"status/{clean_batch}.json")])
        ck("el parte dice limpio", part["status"] == store.Status.CLEAN_, part)
        ck("conteos correctos",
           part["request_counts"] == {"received": 5, "clean": 5, "rejected": 0},
           part["request_counts"])
        ck("el parte NO lleva el payload", "requests" not in part, list(part))

        print("\n[2] un PAN entre cinco -> el gate aborta el lote entero")
        doc = {"requests": [request(f"f-{i}", f"Documento {i}") for i in range(4)]
               + [request("f-4", "paga con 4111111111111111")]}
        s3.put_object(Bucket=RAW, Key="input/conpan.json",
                      Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "input/conpan.json"), Ctx())
        ck("lote en cuarentena", r["status"] == store.Status.QUARANTINED, r)
        ck("el motivo cita el gate", "gate" in r["reason"], r["reason"])
        batches_in_clean = [k for k in s3.keys_of(CLEAN) if k.startswith("clean/")]
        ck("las 4 limpias TAMPOCO cruzan", batches_in_clean == [f"clean/{clean_batch}.json"],
           batches_in_clean)
        ck("informe en cuarentena", len(s3.keys_of(QUAR)) == 1, s3.keys_of(QUAR))

        informe = json.loads(s3.objetos[(QUAR, s3.keys_of(QUAR)[0])])
        ck("el informe NO copia el payload", "requests" not in informe, list(informe))
        ck("el informe apunta al raw", informe["source"]["key"] == "input/conpan.json")
        ck("el informe no contiene el PAN",
           "4111111111111111" not in json.dumps(informe), "fuga en el informe")

        # El productor no tiene acceso al CDE, asi que su unica via para saber
        # que paso es el parte que queda fuera, en el bucket clean.
        rejection_report = json.loads(s3.objetos[(CLEAN, f"status/{r['batch_id']}.json")])
        ck("hay parte de estado del lote rechazado",
           rejection_report["status"] == store.Status.QUARANTINED, rejection_report)
        ck("el parte explica el motivo", "gate" in rejection_report["reason"],
           rejection_report["reason"])
        ck("el parte dice que capa disparo",
           any("pan" in k for k in rejection_report["summary_by_layer"]),
           rejection_report["summary_by_layer"])
        ck("el parte incluye que hacer", len(rejection_report["what_to_do"]) >= 1,
           rejection_report["what_to_do"])
        ck("el parte NO contiene el PAN",
           "4111111111111111" not in json.dumps(rejection_report), "fuga en el parte")
        ck("el parte NO contiene el payload", "requests" not in rejection_report,
           list(rejection_report))

        print("\n[3] SAD -> bloqueo duro, sin mirar el resto")
        doc = {"requests": [request("s-0", "banda ;4111111111111111=25121011000000000?")]
               + [request(f"s-{i}", f"inofensivo {i}") for i in range(1, 50)]}
        s3.put_object(Bucket=RAW, Key="input/sad.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "input/sad.json"), Ctx())
        ck("cuarentena por bloqueo duro", r["status"] == store.Status.QUARANTINED, r)
        ck("el motivo dice bloqueo duro", "bloqueo duro" in r["reason"], r["reason"])
        ck("metrica HardBlock emitida", "HardBlock" in cw.names(), cw.names())

        print("\n[4] envelope deny-by-default")
        doc = {"requests": [{"custom_id": "x", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hola"}],
            "pan": "4111111111111111"}}]}
        s3.put_object(Bucket=RAW, Key="input/clave.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "input/clave.json"), Ctx())
        ck("clave desconocida en params -> cuarentena",
           r["status"] == store.Status.QUARANTINED, r)

        doc = {"requests": [request("y", "hola")], "campo_raro": 1}
        s3.put_object(Bucket=RAW, Key="input/raiz.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "input/raiz.json"), Ctx())
        ck("clave desconocida en la raiz -> cuarentena",
           r["status"] == store.Status.QUARANTINED, r)

        print("\n[4b] el custom_id tambien se escanea")
        # Regresion: el custom_id viaja a Anthropic tal cual, pero solo se
        # validaba su juego de caracteres. Un PAN es alfanumerico, asi que era
        # un custom_id valido y cruzaba la frontera sin pasar por ninguna capa.
        doc = {"requests": [{"custom_id": "4111111111111111", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 10,
            "messages": [{"role": "user", "content": "contenido perfectamente limpio"}]}}]}
        s3.put_object(Bucket=RAW, Key="input/cid.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "input/cid.json"), Ctx())
        ck("PAN en el custom_id -> cuarentena",
           r["status"] == store.Status.QUARANTINED, r)
        part_cid = json.loads(s3.objetos[(CLEAN, f"status/{r['batch_id']}.json")])
        ck("el hallazgo apunta al custom_id",
           any("custom_id" in h.get("where", "")
               for rz in part_cid.get("rejections", [])
               for h in rz.get("findings", [])),
           part_cid.get("rejections"))

        print("\n[5] modelo fuera de la lista blanca")
        doc = {"requests": [request("z", "hola", modelo="gpt-4")]}
        s3.put_object(Bucket=RAW, Key="input/modelo.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "input/modelo.json"), Ctx())
        ck("modelo no permitido -> cuarentena", r["status"] == store.Status.QUARANTINED, r)

        print("\n[6] reentrega del mismo objeto: id estable, sin duplicar")
        antes = len(table_.items)
        r2 = sanitizer.lambda_handler(s3_event(RAW, "input/limpio.json"), Ctx())
        ck("mismo batch_id", r2["batch_id"] == clean_batch, (r2["batch_id"], clean_batch))
        ck("no aparecen items nuevos", len(table_.items) == antes, (antes, len(table_.items)))

        print("\n[7] el canary debe quedar bloqueado")
        # Se importan del modulo real que usa la Lambda, no de una copia: si
        # alguien anade un caso al canary, esta prueba lo cubre sola.
        sys.path.insert(0, os.path.join(REPO, "src", "canary"))
        from cases import CASES
        doc = {"requests": [request(f"canary-{i}", text)
                            for i, (_name, text) in enumerate(CASES)]}
        s3.put_object(Bucket=RAW, Key="canary/test.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(s3_event(RAW, "canary/test.json"), Ctx())
        ck("el canary NO cruza", r["status"] == store.Status.QUARANTINED, r)
        item = table_.items[r["batch_id"]]
        ck("marcado como canary", item.get("is_canary") is True, item.get("is_canary"))
        # Un canary bloqueado es una buena noticia, no una incidencia: no puede
        # compartir metrica con los productores o la alarma suena a diario por
        # un exito, y en dos semanas nadie la mira.
        ck("emite CanaryHardBlock, no HardBlock",
           "CanaryHardBlock" in cw.names() and
           cw.names().count("HardBlock") == 1,   # solo la del caso [3], real
           [n for n in cw.names() if "Bloqueo" in n])
        ck("y CanaryQuarantined, no BatchesQuarantined",
           "CanaryQuarantined" in cw.names(),
           [n for n in cw.names() if "uarentena" in n])

    # --- verifier, sobre el objeto que el sanitizer dio por bueno --------
    for modulo in ("handler", "store", "config", "logs", "detectors", "normalize",
                   "envelope", "secret_store", "anthropic_batches"):
        sys.modules.pop(modulo, None)
    sys.path.remove(pkg_san)
    sys.path.insert(0, pkg_ver)

    with mock.patch("boto3.client", side_effect=fake_client), \
         mock.patch("boto3.resource", return_value=FakeResource(table_)):
        import handler as verifier
        import store as store2

        print("\n[8] verifier sobre datos limpios de verdad")
        clean_key = s3.keys_of(CLEAN)[0]
        r = verifier.lambda_handler(s3_event(CLEAN, clean_key), Ctx())
        ck("estado verificado", r["status"] == store2.Status.VERIFIED, r)
        ck("el objeto sigue en clean", (CLEAN, clean_key) in s3.objetos)

        print("\n[9] verifier ante un fallo del sanitizer")
        # Se falsifica un objeto limpio con un PAN partido por puntos: el
        # regex del sanitizer es ciego a eso, la ventana deslizante no.
        envenenado = {"batch_id": "b_envenenado", "requests": [
            {"custom_id": "p", "params": {
                "model": "claude-sonnet-4-5", "max_tokens": 10,
                "messages": [{"role": "user", "content": "ref 4111.1111.1111.1111"}]}}]}
        s3.put_object(Bucket=CLEAN, Key="clean/b_envenenado.json",
                      Body=json.dumps(envenenado).encode())
        r = verifier.lambda_handler(s3_event(CLEAN, "clean/b_envenenado.json"), Ctx())
        ck("detecta el PAN en zona limpia", r["status"] == store2.Status.QUARANTINED, r)
        ck("borra el objeto de clean",
           (CLEAN, "clean/b_envenenado.json") not in s3.objetos, s3.keys_of(CLEAN))
        ck("deja informe en cuarentena",
           any("verifier" in k for k in s3.keys_of(QUAR)), s3.keys_of(QUAR))
        ck("alarma FalloDelSanitizer", "SanitizerFailure" in cw.names())

        print("\n[10] admision: la cola en vuelo no se puede sobrepasar")
        store2.update("b_uno", status=store2.Status.VERIFIED, request_count=600,
                      created_at="2026-01-01T00:00:00+00:00")
        store2.update("b_dos", status=store2.Status.VERIFIED, request_count=600,
                      created_at="2026-01-01T00:01:00+00:00")
        ck("cola vacia al empezar", store2.inflight() == 0, store2.inflight())
        ck("el primero entra (600/1000)", store2.try_admit("b_uno", 600, 1000) is True)
        ck("cola a 600", store2.inflight() == 600, store2.inflight())
        ck("el segundo NO entra (600+600>1000)",
           store2.try_admit("b_dos", 600, 1000) is False)
        ck("la cola no se movio", store2.inflight() == 600, store2.inflight())
        ck("al liberar el primero baja", store2.release("b_uno", 600) and
           store2.inflight() == 0, store2.inflight())
        ck("liberar dos veces no descuenta de mas",
           store2.release("b_uno", 600) is False and store2.inflight() == 0,
           store2.inflight())
        ck("ahora si entra el segundo", store2.try_admit("b_dos", 600, 1000) is True)

    shutil.rmtree(pkg_san, ignore_errors=True)
    shutil.rmtree(pkg_ver, ignore_errors=True)
    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
