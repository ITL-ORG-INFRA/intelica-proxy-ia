"""El contrato con la infraestructura: variables, claves, estados y metricas.

Nada de esto lo comprueba una prueba de comportamiento. Un estado que se
escribe en castellano sigue funcionando de punta a punta —el sistema se
entiende consigo mismo— pero deja de encajar con el GSI, con las alarmas y con
lo que documentamos a los productores. El fallo aparece semanas despues, en
una consulta que no devuelve nada.

Asi que se comprueban los NOMBRES, uno a uno, contra la lista acordada.

    .venv/bin/python tests/contrato_test.py
"""
import os
import shutil
import sys
import tempfile
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))

from dobles import FakeCloudWatch, FakeResource, FakeS3, FakeTable  # noqa: E402

RAW, QUAR, CLEAN, RES = "b-raw", "b-quar", "b-clean", "b-res"

BASE_ENV = {
    "AWS_DEFAULT_REGION": "eu-south-2",
    "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
    "RAW_BUCKET": RAW, "QUARANTINE_BUCKET": QUAR,
    "CLEAN_BUCKET": CLEAN, "RESULTS_BUCKET": RES,
    "BATCHES_TABLE": "t-batches",
    "ANTHROPIC_SECRET_ARN": "arn:aws:secretsmanager:eu-south-2:1:secret:x",
    "ALLOWED_MODELS": "claude-sonnet-4-5",
    "GATE_REJECT_PCT": "1.0", "GATE_REJECT_ABS": "100",
    "INFLIGHT_LIMIT": "1000", "ENVIRONMENT": "test", "LOG_LEVEL": "ERROR",
}
os.environ.update(BASE_ENV)

FAILURES = []


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


def request(cid, text):
    return {"custom_id": cid, "params": {
        "model": "claude-sonnet-4-5", "max_tokens": 64,
        "messages": [{"role": "user", "content": text}]}}


# --- 1. variables de entorno -----------------------------------------------

#: los nombres retirados y el valor por defecto que taparian. El numero
#: importa: es lo que hace visible el fallo silencioso.
RETIRADAS = {
    "SUBMIT_MAX_POR_TICK": ("SUBMIT_MAX_PER_TICK", 2),
    "FETCH_MAX_POR_TICK": ("FETCH_MAX_PER_TICK", 2),
}


def prueba_variables(config):
    print("\n[1] variables de entorno")

    ck("SUBMIT_MAX_PER_TICK se lee", config.SUBMIT_MAX_PER_TICK == 7,
       config.SUBMIT_MAX_PER_TICK)
    ck("FETCH_MAX_PER_TICK se lee", config.FETCH_MAX_PER_TICK == 9,
       config.FETCH_MAX_PER_TICK)
    ck("CANARY_PREFIX es canary/", config.CANARY_PREFIX == "canary/",
       config.CANARY_PREFIX)

    print("\n[1b] un nombre retirado NO puede caer en el valor por defecto")
    # Este es el fallo que se quiere hacer imposible: Terraform se queda con
    # el nombre viejo, el codigo no lo lee, coge el default y nadie se entera.
    # Se envia 2 por tick durante semanas creyendo que se envian 20.
    for vieja, (nueva, por_defecto) in RETIRADAS.items():
        entorno = {vieja: "20"}
        try:
            config.reject_retired(entorno)
            ck(f"{vieja} revienta en vez de valer {por_defecto}", False,
               f"paso sin quejarse, y el codigo usaria {por_defecto}")
        except RuntimeError as exc:
            ck(f"{vieja} revienta en vez de valer {por_defecto}", True)
            ck(f"y el error dice como se llama ahora ({nueva})",
               nueva in str(exc) and vieja in str(exc), str(exc))

    ck("un entorno solo con los nombres nuevos no se queja",
       config.reject_retired({"SUBMIT_MAX_PER_TICK": "20",
                              "FETCH_MAX_PER_TICK": "20"}) is None)

    print("\n[1c] el prefijo del canary ya no es 'canario/'")
    ck("CANARY_PREFIX no es canario/", config.CANARY_PREFIX != "canario/",
       config.CANARY_PREFIX)
    fuente = open(os.path.join(REPO, "src", "common", "config.py"),
                  encoding="utf-8").read()
    for viejo in ("SUBMIT_MAX_POR_TICK", "FETCH_MAX_POR_TICK"):
        # Aparecen SOLO en la tabla de retiradas, nunca como nombre que se lea.
        ck(f"{viejo} no se lee en ninguna parte",
           f'_env("{viejo}"' not in fuente and f'_int("{viejo}"' not in fuente)


# --- 2. DynamoDB ------------------------------------------------------------

ESTADOS = {
    "RECEIVED": "received", "CLEAN_": "clean", "VERIFIED": "verified",
    "HELD": "held", "SUBMITTED": "submitted", "COMPLETED": "completed",
    "DELIVERED": "delivered", "QUARANTINED": "quarantined",
    "EXPIRED": "expired", "FAILED": "failed",
}
ESTADOS_LOTE = {"AWAITING_PARTS": "awaiting_parts", "READY": "ready",
                "SUBMITTED": "submitted", "QUARANTINED": "quarantined",
                "FAILED": "failed"}

#: castellano -> ingles. Ninguno de los de la izquierda puede reaparecer.
ATRIBUTOS_RETIRADOS = {
    "partes_esperadas": "expected_parts", "partes_limpias": "clean_parts",
    "partes_rechazadas": "rejected_parts", "manifiesto_visto": "manifest_seen",
    "ficheros": "files", "lote": "batch", "reclamado_en": "claimed_at",
    "consultado_en": "polled_at", "enviado_en": "submitted_at",
    "ultimo_lote": "last_batch", "ultima_clave": "last_key",
    "es_canario": "is_canary",
}


def prueba_dynamo(store, s3, table_, sanitizer):
    print("\n[2] estados de DynamoDB, en ingles")
    for nombre, valor in ESTADOS.items():
        ck(f"Status.{nombre} = {valor}", getattr(store.Status, nombre) == valor,
           getattr(store.Status, nombre))
    for nombre, valor in ESTADOS_LOTE.items():
        ck(f"BatchState.{nombre} = {valor}",
           getattr(store.BatchState, nombre) == valor,
           getattr(store.BatchState, nombre))

    print("\n[3] las claves de item")
    store.record_part("input/l", "input/l/p1.json", cleaned=True,
                      request_count=1, clean_key="clean/x.json")
    ck("la parte se guarda como part#<key>",
       "part#input/l/p1.json" in table_.items, sorted(table_.items))
    ck("el lote se guarda como batch#<carpeta>",
       "batch#input/l" in table_.items, sorted(table_.items))
    ck("ninguna clave usa 'lote#'",
       not any(k.startswith("lote#") for k in table_.items), sorted(table_.items))
    ck("ninguna clave usa 'parte#'",
       not any(k.startswith("parte#") for k in table_.items), sorted(table_.items))

    store.update("__canary__", last_batch="b_x", last_key="canary/x.json")
    ck("el canary se guarda como __canary__", "__canary__" in table_.items,
       sorted(table_.items))
    ck("no existe __canario__", "__canario__" not in table_.items)

    print("\n[4] los atributos persistidos, en ingles")
    store.record_manifest("input/l", ["p1.json"], 1)
    lote = table_.items["batch#input/l"]
    for esperado in ("expected_parts", "clean_parts", "manifest_seen",
                     "files", "batch"):
        ck(f"el lote lleva '{esperado}'", esperado in lote, sorted(lote))

    # Lo que de verdad importa: que NINGUN item tenga un nombre retirado.
    escritos = set()
    for item in table_.items.values():
        escritos.update(item)
    intrusos = escritos & set(ATRIBUTOS_RETIRADOS)
    ck("ningun item lleva un atributo en castellano", not intrusos, intrusos)

    print("\n[5] la clave de particion y el GSI no cambian")
    ck("todos los items tienen batch_id",
       all("batch_id" in i for i in table_.items.values()))
    fuente = open(os.path.join(REPO, "src", "common", "store.py"),
                  encoding="utf-8").read()
    ck("el GSI sigue siendo status-index", 'IndexName="status-index"' in fuente)

    print("\n[6] el lote que sanitiza escribe estado y atributos en ingles")
    s3.put_object(Bucket=RAW, Key="input/limpio.json",
                  Body=b'{"requests": [' +
                       b'{"custom_id": "a", "params": {"model": "claude-sonnet-4-5",'
                       b'"max_tokens": 16, "messages": [{"role": "user",'
                       b'"content": "resume esto"}]}}]}')
    r = sanitizer.lambda_handler(s3_event(RAW, "input/limpio.json"), Ctx())
    item = table_.items[r["batch_id"]]
    ck("status = clean", item["status"] == "clean", item["status"])
    ck("is_canary, no es_canario", "is_canary" in item and "es_canario" not in item,
       sorted(item))
    return r["batch_id"]


# --- 3. prefijos de S3 ------------------------------------------------------

def prueba_prefijos(s3, batch_id):
    print("\n[7] donde acaba cada cosa en S3")
    claves_clean = s3.keys_of(CLEAN)
    ck("el lote sanitizado va a clean/",
       f"clean/{batch_id}.json" in claves_clean, claves_clean)
    ck("el parte de estado va a clean/status/, no a estado/",
       f"status/{batch_id}.json" in claves_clean, claves_clean)
    ck("nada bajo 'estado/'",
       not any(k.startswith("estado/") for k in claves_clean), claves_clean)
    ck("nada bajo 'entrada/' en clean",
       not any(k.startswith("entrada/") for k in claves_clean), claves_clean)
    ck("las partes se subieron a raw bajo input/",
       s3.keys_of(RAW) == ["input/limpio.json"], s3.keys_of(RAW))


# --- 4. namespaces y metricas ----------------------------------------------

NAMESPACES = {
    "canary": "IntelicaProxyIA/Canary",
    "reconciler": "IntelicaProxyIA/Reconciler",
    "verifier": "IntelicaProxyIA/Verifier",
    "sanitizer": "IntelicaProxyIA/Sanitizer",
    "submitter": "IntelicaProxyIA/Submitter",
    "fetcher": "IntelicaProxyIA/Fetcher",
}

#: las que tienen alarma. Si una desaparece, la alarma deja de sonar y no hay
#: nada que lo delate: una alarma sobre una metrica que nadie emite se queda
#: en INSUFFICIENT_DATA, que no es un estado de alarma.
CON_ALARMA = ["HardBlock", "CanaryNotBlocked", "CanaryNotProcessed",
              "SanitizerFailure", "PanInResults", "BatchesQuarantined",
              "BatchesExpired", "QueueOccupancy"]

OTRAS = ["CanaryBlocked", "CanaryHardBlock", "CanaryQuarantined",
         "CanaryStoppedAtSubmitter", "CanaryWithoutBaseline", "BatchesSubmitted",
         "BatchesDelivered", "BatchesHeld", "BatchesCompleted", "BatchesVerified",
         "PollsFailed", "PollsPerformed", "ManifestUnreadable", "ManifestInvalid",
         "ManifestsReceived", "DuplicateCustomId", "SubmitsFailed",
         "RequestsInFlight", "RequestsSubmitted", "RequestsClean",
         "RequestsRejected", "RejectionRate", "PansInCleanZone",
         "TextsWithLongDigits", "TicksThrottled", "TicksWithoutPoll"]


def emitidas():
    """Los literales que cada handler pasa a _metric(), leidos del fuente."""
    import re
    fuera = {}
    for modulo, carpeta in (("canary", "canary"), ("reconciler", "reconciler"),
                            ("verifier", "verifier"), ("sanitizer", "sanitizer"),
                            ("submitter", "submitter"), ("fetcher", "fetcher")):
        ruta = os.path.join(REPO, "src", carpeta, "handler.py")
        texto = open(ruta, encoding="utf-8").read()
        namespace = re.search(r'NAMESPACE = "([^"]+)"', texto)
        fuera[modulo] = (namespace.group(1) if namespace else "",
                         set(re.findall(r'_metric\("([A-Za-z]+)"', texto)))
    return fuera


def prueba_metricas():
    print("\n[8] namespaces de CloudWatch")
    fuera = emitidas()
    for modulo, esperado in NAMESPACES.items():
        ck(f"{modulo}: {esperado}", fuera[modulo][0] == esperado, fuera[modulo][0])

    print("\n[9] las metricas con alarma se siguen emitiendo")
    todas = set()
    for _ns, nombres in fuera.values():
        todas |= nombres
    for metrica in CON_ALARMA:
        ck(f"{metrica} se emite", metrica in todas, sorted(todas))

    print("\n[10] las demas metricas, y ninguna de mas")
    for metrica in OTRAS:
        ck(f"{metrica} se emite", metrica in todas, sorted(todas))
    sobran = todas - set(CON_ALARMA) - set(OTRAS)
    ck("no se emite ninguna metrica fuera de la lista acordada", not sobran, sobran)

    print("\n[11] no queda ningun nombre de metrica ni namespace en castellano")
    import re
    for carpeta in NAMESPACES:
        texto = open(os.path.join(REPO, "src", carpeta, "handler.py"),
                     encoding="utf-8").read()
        for viejo in ("Canario", "Reconciliador", "Verificador", "BloqueoDuro",
                      "FalloDelSanitizer", "EnCuarentena", "Cuarentena",
                      "Rechazad", "Enviad"):
            apariciones = re.findall(rf'(?:_metric\("|NAMESPACE = ")[^"]*{viejo}',
                                     texto)
            ck(f"{carpeta}: nada llamado *{viejo}*", not apariciones, apariciones)


def main():
    s3, table_, cw = FakeS3(), FakeTable(), FakeCloudWatch()

    def fake_client(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw}[servicio]

    pkg = build_event("sanitizer")
    sys.path.insert(0, pkg)

    os.environ["SUBMIT_MAX_PER_TICK"] = "7"
    os.environ["FETCH_MAX_PER_TICK"] = "9"

    with mock.patch("boto3.client", side_effect=fake_client), \
         mock.patch("boto3.resource", return_value=FakeResource(table_)):
        import config
        import handler as sanitizer
        import store

        prueba_variables(config)
        batch_id = prueba_dynamo(store, s3, table_, sanitizer)
        prueba_prefijos(s3, batch_id)

    prueba_metricas()

    shutil.rmtree(pkg, ignore_errors=True)
    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
