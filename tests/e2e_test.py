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

FALLOS = []


def ck(nombre, condicion, detalle=""):
    print(("  OK   " if condicion else "  FALLA ") + nombre
          + ("" if condicion else f"  <- {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


def montar(*carpetas):
    """Replica el empaquetado de deploy.sh."""
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
                       "object": {"key": key, "etag": "etag-simulado"}}}


def peticion(cid, texto, modelo="claude-sonnet-4-5"):
    return {"custom_id": cid, "params": {
        "model": modelo, "max_tokens": 256,
        "messages": [{"role": "user", "content": texto}]}}


def main():
    s3, tabla, cw = FakeS3(), FakeTable(), FakeCloudWatch()

    def cliente(servicio, **_kw):
        return {"s3": s3, "cloudwatch": cw}[servicio]

    pkg_san = montar("sanitizer")
    pkg_ver = montar("sanitizer", "verificador")
    sys.path.insert(0, pkg_san)

    with mock.patch("boto3.client", side_effect=cliente), \
         mock.patch("boto3.resource", return_value=FakeResource(tabla)):
        import handler as sanitizer
        import store

        print("\n[1] lote limpio -> cruza a la zona limpia")
        doc = {"requests": [peticion(f"fila-{i}", f"Resume el documento {i}")
                            for i in range(5)]}
        s3.put_object(Bucket=RAW, Key="entrada/limpio.json",
                      Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "entrada/limpio.json"), Ctx())
        ck("estado limpio", r["estado"] == store.Status.LIMPIO, r)
        ck("5 peticiones limpias", r["limpias"] == 5, r)
        ck("nada en cuarentena", len(s3.claves(QUAR)) == 0, s3.claves(QUAR))
        lote_limpio = r["batch_id"]

        claves_clean = s3.claves(CLEAN)
        ck("un solo lote en clean/",
           [k for k in claves_clean if k.startswith("clean/")] == [f"clean/{lote_limpio}.json"],
           claves_clean)
        ck("el lote escrito en clean/", f"clean/{lote_limpio}.json" in claves_clean,
           claves_clean)
        ck("parte de estado escrito en estado/",
           f"estado/{lote_limpio}.json" in claves_clean, claves_clean)

        parte = json.loads(s3.objetos[(CLEAN, f"estado/{lote_limpio}.json")])
        ck("el parte dice limpio", parte["estado"] == store.Status.LIMPIO, parte)
        ck("conteos correctos",
           parte["peticiones"] == {"recibidas": 5, "limpias": 5, "rechazadas": 0},
           parte["peticiones"])
        ck("el parte NO lleva el payload", "requests" not in parte, list(parte))

        print("\n[2] un PAN entre cinco -> el gate aborta el lote entero")
        doc = {"requests": [peticion(f"f-{i}", f"Documento {i}") for i in range(4)]
               + [peticion("f-4", "paga con 4111111111111111")]}
        s3.put_object(Bucket=RAW, Key="entrada/conpan.json",
                      Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "entrada/conpan.json"), Ctx())
        ck("lote en cuarentena", r["estado"] == store.Status.CUARENTENA, r)
        ck("el motivo cita el gate", "gate" in r["motivo"], r["motivo"])
        lotes_en_clean = [k for k in s3.claves(CLEAN) if k.startswith("clean/")]
        ck("las 4 limpias TAMPOCO cruzan", lotes_en_clean == [f"clean/{lote_limpio}.json"],
           lotes_en_clean)
        ck("informe en cuarentena", len(s3.claves(QUAR)) == 1, s3.claves(QUAR))

        informe = json.loads(s3.objetos[(QUAR, s3.claves(QUAR)[0])])
        ck("el informe NO copia el payload", "requests" not in informe, list(informe))
        ck("el informe apunta al raw", informe["origen"]["key"] == "entrada/conpan.json")
        ck("el informe no contiene el PAN",
           "4111111111111111" not in json.dumps(informe), "fuga en el informe")

        # El productor no tiene acceso al CDE, asi que su unica via para saber
        # que paso es el parte que queda fuera, en el bucket clean.
        parte_rechazo = json.loads(s3.objetos[(CLEAN, f"estado/{r['batch_id']}.json")])
        ck("hay parte de estado del lote rechazado",
           parte_rechazo["estado"] == store.Status.CUARENTENA, parte_rechazo)
        ck("el parte explica el motivo", "gate" in parte_rechazo["motivo"],
           parte_rechazo["motivo"])
        ck("el parte dice que capa disparo",
           any("pan" in k for k in parte_rechazo["resumen_por_capa"]),
           parte_rechazo["resumen_por_capa"])
        ck("el parte incluye que hacer", len(parte_rechazo["que_hacer"]) >= 1,
           parte_rechazo["que_hacer"])
        ck("el parte NO contiene el PAN",
           "4111111111111111" not in json.dumps(parte_rechazo), "fuga en el parte")
        ck("el parte NO contiene el payload", "requests" not in parte_rechazo,
           list(parte_rechazo))

        print("\n[3] SAD -> bloqueo duro, sin mirar el resto")
        doc = {"requests": [peticion("s-0", "banda ;4111111111111111=25121011000000000?")]
               + [peticion(f"s-{i}", f"inofensivo {i}") for i in range(1, 50)]}
        s3.put_object(Bucket=RAW, Key="entrada/sad.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "entrada/sad.json"), Ctx())
        ck("cuarentena por bloqueo duro", r["estado"] == store.Status.CUARENTENA, r)
        ck("el motivo dice bloqueo duro", "bloqueo duro" in r["motivo"], r["motivo"])
        ck("metrica BloqueoDuro emitida", "BloqueoDuro" in cw.nombres(), cw.nombres())

        print("\n[4] envelope deny-by-default")
        doc = {"requests": [{"custom_id": "x", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hola"}],
            "pan": "4111111111111111"}}]}
        s3.put_object(Bucket=RAW, Key="entrada/clave.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "entrada/clave.json"), Ctx())
        ck("clave desconocida en params -> cuarentena",
           r["estado"] == store.Status.CUARENTENA, r)

        doc = {"requests": [peticion("y", "hola")], "campo_raro": 1}
        s3.put_object(Bucket=RAW, Key="entrada/raiz.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "entrada/raiz.json"), Ctx())
        ck("clave desconocida en la raiz -> cuarentena",
           r["estado"] == store.Status.CUARENTENA, r)

        print("\n[4b] el custom_id tambien se escanea")
        # Regresion: el custom_id viaja a Anthropic tal cual, pero solo se
        # validaba su juego de caracteres. Un PAN es alfanumerico, asi que era
        # un custom_id valido y cruzaba la frontera sin pasar por ninguna capa.
        doc = {"requests": [{"custom_id": "4111111111111111", "params": {
            "model": "claude-sonnet-4-5", "max_tokens": 10,
            "messages": [{"role": "user", "content": "contenido perfectamente limpio"}]}}]}
        s3.put_object(Bucket=RAW, Key="entrada/cid.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "entrada/cid.json"), Ctx())
        ck("PAN en el custom_id -> cuarentena",
           r["estado"] == store.Status.CUARENTENA, r)
        parte_cid = json.loads(s3.objetos[(CLEAN, f"estado/{r['batch_id']}.json")])
        ck("el hallazgo apunta al custom_id",
           any("custom_id" in h.get("donde", "")
               for rz in parte_cid.get("rechazos", [])
               for h in rz.get("hallazgos", [])),
           parte_cid.get("rechazos"))

        print("\n[5] modelo fuera de la lista blanca")
        doc = {"requests": [peticion("z", "hola", modelo="gpt-4")]}
        s3.put_object(Bucket=RAW, Key="entrada/modelo.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "entrada/modelo.json"), Ctx())
        ck("modelo no permitido -> cuarentena", r["estado"] == store.Status.CUARENTENA, r)

        print("\n[6] reentrega del mismo objeto: id estable, sin duplicar")
        antes = len(tabla.items)
        r2 = sanitizer.lambda_handler(evento_s3(RAW, "entrada/limpio.json"), Ctx())
        ck("mismo batch_id", r2["batch_id"] == lote_limpio, (r2["batch_id"], lote_limpio))
        ck("no aparecen items nuevos", len(tabla.items) == antes, (antes, len(tabla.items)))

        print("\n[7] el canario debe quedar bloqueado")
        # Se importan del modulo real que usa la Lambda, no de una copia: si
        # alguien anade un caso al canario, esta prueba lo cubre sola.
        sys.path.insert(0, os.path.join(REPO, "src", "canario"))
        from casos import CASOS
        doc = {"requests": [peticion(f"canario-{i}", texto)
                            for i, (_nombre, texto) in enumerate(CASOS)]}
        s3.put_object(Bucket=RAW, Key="canario/prueba.json", Body=json.dumps(doc).encode())
        r = sanitizer.lambda_handler(evento_s3(RAW, "canario/prueba.json"), Ctx())
        ck("el canario NO cruza", r["estado"] == store.Status.CUARENTENA, r)
        item = tabla.items[r["batch_id"]]
        ck("marcado como canario", item.get("es_canario") is True, item.get("es_canario"))
        # Un canario bloqueado es una buena noticia, no una incidencia: no puede
        # compartir metrica con los productores o la alarma suena a diario por
        # un exito, y en dos semanas nadie la mira.
        ck("emite CanarioBloqueoDuro, no BloqueoDuro",
           "CanarioBloqueoDuro" in cw.nombres() and
           cw.nombres().count("BloqueoDuro") == 1,   # solo la del caso [3], real
           [n for n in cw.nombres() if "Bloqueo" in n])
        ck("y CanarioEnCuarentena, no LotesEnCuarentena",
           "CanarioEnCuarentena" in cw.nombres(),
           [n for n in cw.nombres() if "uarentena" in n])

    # --- verificador, sobre el objeto que el sanitizer dio por bueno --------
    for modulo in ("handler", "store", "config", "logs", "detectors", "normalize",
                   "envelope", "secret_store", "anthropic_batches"):
        sys.modules.pop(modulo, None)
    sys.path.remove(pkg_san)
    sys.path.insert(0, pkg_ver)

    with mock.patch("boto3.client", side_effect=cliente), \
         mock.patch("boto3.resource", return_value=FakeResource(tabla)):
        import handler as verificador
        import store as store2

        print("\n[8] verificador sobre datos limpios de verdad")
        clave_limpia = s3.claves(CLEAN)[0]
        r = verificador.lambda_handler(evento_s3(CLEAN, clave_limpia), Ctx())
        ck("estado verificado", r["estado"] == store2.Status.VERIFICADO, r)
        ck("el objeto sigue en clean", (CLEAN, clave_limpia) in s3.objetos)

        print("\n[9] verificador ante un fallo del sanitizer")
        # Se falsifica un objeto limpio con un PAN partido por puntos: el
        # regex del sanitizer es ciego a eso, la ventana deslizante no.
        envenenado = {"batch_id": "b_envenenado", "requests": [
            {"custom_id": "p", "params": {
                "model": "claude-sonnet-4-5", "max_tokens": 10,
                "messages": [{"role": "user", "content": "ref 4111.1111.1111.1111"}]}}]}
        s3.put_object(Bucket=CLEAN, Key="clean/b_envenenado.json",
                      Body=json.dumps(envenenado).encode())
        r = verificador.lambda_handler(evento_s3(CLEAN, "clean/b_envenenado.json"), Ctx())
        ck("detecta el PAN en zona limpia", r["estado"] == store2.Status.CUARENTENA, r)
        ck("borra el objeto de clean",
           (CLEAN, "clean/b_envenenado.json") not in s3.objetos, s3.claves(CLEAN))
        ck("deja informe en cuarentena",
           any("verificador" in k for k in s3.claves(QUAR)), s3.claves(QUAR))
        ck("alarma FalloDelSanitizer", "FalloDelSanitizer" in cw.nombres())

        print("\n[10] admision: la cola en vuelo no se puede sobrepasar")
        store2.update("b_uno", status=store2.Status.VERIFICADO, request_count=600,
                      created_at="2026-01-01T00:00:00+00:00")
        store2.update("b_dos", status=store2.Status.VERIFICADO, request_count=600,
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
    print("\n" + ("TODO OK" if not FALLOS else f"{len(FALLOS)} FALLOS: {FALLOS}"))
    return 1 if FALLOS else 0


if __name__ == "__main__":
    sys.exit(main())
