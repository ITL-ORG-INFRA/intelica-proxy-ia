"""λ SUBMITTER — el unico rol que habla con Anthropic.

No tiene permiso de lectura sobre raw ni sobre quarantine: si alguien roba
esta credencial, no saca una tarjeta. Lee de clean, que ya paso el sanitizer y
el verifier.

Tiene DOS disparadores, y los dos acaban en el mismo sitio:

  1. Evento de S3 sobre raw/**/_MANIFEST.json — el productor ha terminado de
     subir el lote y avisa. Si todas las partes estan sanitizadas, se envia en
     el acto.

  2. Horario — barrido de seguridad. Cubre el caso en que el manifiesto llego
     ANTES de que el sanitizer acabara con alguna parte: ahi el evento no pudo
     enviar y hace falta que alguien vuelva a mirar.

El envio se reclama con una escritura condicional, asi que si los dos caminos
coinciden en el mismo instante solo uno envia. Sin eso el lote se enviaria dos
veces y se pagaria dos veces.
"""
import json
import time
from typing import Any, Dict, List
from urllib.parse import unquote_plus

import boto3

import store
from anthropic_batches import AnthropicError, create_batch
from config import (
    CLEAN_BUCKET, ENVIRONMENT, INFLIGHT_LIMIT, RAW_BUCKET,
    SUBMIT_MAX_PER_TICK, require,
)
from logs import get_logger
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Submitter"


def _metric(name: str, value: float, unidad: str = "Count") -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": name, "Value": value, "Unit": unidad,
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        pass


def _throttled() -> float:
    """Segundos que Anthropic pidio esperar la ultima vez, si es que lo pidio."""
    headers = store.load_ratelimit()
    try:
        return float(headers.get("retry-after", 0))
    except (TypeError, ValueError):
        return 0.0


MANIFEST = "_MANIFEST.json"


def _key_from_event(event: Dict[str, Any]) -> str:
    """La clave del objeto si el evento viene de S3; vacio si es un tick.

    Solo se decodifica la de la notificacion nativa de S3. EventBridge la
    entrega sin codificar, y decodificarla convertiria un '+' literal en un
    espacio: el manifiesto pasaria a apuntar a un lote que no existe, y el
    submitter lo marcaria fallido con un nombre corrupto.
    """
    if event.get("source") == "aws.s3" or "detail" in event:
        return event.get("detail", {}).get("object", {}).get("key", "")
    registros = event.get("Records") or []
    if registros and "s3" in registros[0]:
        return unquote_plus(registros[0]["s3"].get("object", {}).get("key", ""))
    return ""


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("CLEAN_BUCKET", "BATCHES_TABLE", "ANTHROPIC_SECRET_ARN")
    started = time.monotonic()

    key = _key_from_event(event)
    if key.endswith(MANIFEST):
        return _by_manifest(key, started)

    wait = _throttled()
    if wait > 0:
        # Auto-frenado: Anthropic ya dijo que esperasemos. Insistir solo
        # empeora la ventana de rate limit.
        log.warning("tick omitido por retry-after", extra={"ctx_retry_after": wait})
        _metric("TicksThrottled", 1)
        return {"skipped": "retry-after", "seconds": wait}

    candidates = store.by_statuses(Status.AWAITING_ADMISSION, limit=50)
    # Los mas antiguos primero: un lote retenido no puede quedarse atras para
    # siempre porque vayan llegando otros nuevos.
    candidates.sort(key=lambda b: b.get("created_at", ""))

    submitted, held, failed_count = 0, 0, 0
    in_flight = store.inflight()

    for batch in candidates:
        if submitted >= SUBMIT_MAX_PER_TICK:
            break
        batch_id = batch["batch_id"]
        how_many = int(batch.get("request_count", 0))

        if batch.get("is_canary"):
            # El canary prueba el sanitizer, no la Batch API. Si llega hasta
            # aqui es que el gate fallo, y enviarlo solo agravaria el fallo.
            store.update(batch_id, status=Status.FAILED,
                         reason="canary detenido en el submitter: el gate no lo bloqueo")
            log.error("canary detenido en el submitter", extra={"ctx_batch_id": batch_id})
            _metric("CanaryStoppedAtSubmitter", 1)
            continue

        if not store.try_admit(batch_id, how_many, INFLIGHT_LIMIT):
            if batch.get("status") != Status.HELD:
                store.update(batch_id, status=Status.HELD,
                             reason=f"cola en vuelo llena ({in_flight}/{INFLIGHT_LIMIT})")
            held += 1
            continue

        try:
            clean_key = batch.get("clean_key")
            body = _s3.get_object(Bucket=CLEAN_BUCKET, Key=clean_key)["Body"].read()
            documento = json.loads(body)

            remote_batch, headers = create_batch(documento["requests"])
            store.save_ratelimit(headers)

            store.update(
                batch_id,
                status=Status.SUBMITTED,
                anthropic_batch_id=remote_batch["id"],
                submitted_at=store.now_iso(),
                expira_en=remote_batch.get("expires_at"),
            )
            submitted += 1
            in_flight += how_many
            log.info("lote enviado", extra={
                "ctx_batch_id": batch_id, "ctx_anthropic_id": remote_batch["id"],
                "ctx_requests": how_many, "ctx_en_vuelo": in_flight})

        except AnthropicError as exc:
            # La reserva se devuelve: si no se envio, no ocupa cola.
            store.release(batch_id, how_many)
            failed_count += 1
            if exc.retry_after:
                store.save_ratelimit({"retry-after": str(exc.retry_after)})
            store.update(batch_id, status=Status.VERIFIED,
                         reason=f"envio fallido: {exc.codigo}")
            log.error("fallo al enviar", extra={
                "ctx_batch_id": batch_id, "ctx_codigo": exc.codigo})
            if exc.codigo == "rate_limited":
                break  # no se insiste en el mismo tick
        except Exception:  # noqa: BLE001
            store.release(batch_id, how_many)
            failed_count += 1
            store.update(batch_id, status=Status.VERIFIED, reason="fallo interno del envio")
            log.exception("fallo interno al enviar", extra={"ctx_batch_id": batch_id})

    # Red de seguridad del camino del manifiesto: si alguno llego antes de que
    # el sanitizer acabara, aqui se recoge.
    pending_items = sweep_pending()

    _metric("BatchesSubmitted", submitted)
    _metric("BatchesHeld", held)
    _metric("RequestsInFlight", in_flight)
    _metric("QueueOccupancy", (in_flight / INFLIGHT_LIMIT * 100) if INFLIGHT_LIMIT else 0, "Percent")

    resumen = {"submitted": submitted, "held": held, "failed_count": failed_count,
               "in_flight": in_flight,
               "batches_swept": pending_items["reviewed"],
               "batches_submitted": pending_items["submitted"],
               "batches_awaiting": pending_items["awaiting"],
               "ms": round((time.monotonic() - started) * 1000)}
    log.info("tick de envio", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen


# ---------------------------------------------------------------------------
# Camino 1: llego el manifiesto
# ---------------------------------------------------------------------------

def _by_manifest(manifest_key: str, started: float) -> Dict[str, Any]:
    """El productor ha cerrado el lote. Se envia si esta completo."""
    folder = store.batch_of(manifest_key)
    if not folder:
        log.error("manifiesto en la raiz del bucket, sin lote al que pertenecer",
                  extra={"ctx_key": manifest_key})
        return {"error": "manifiesto sin carpeta de lote"}

    try:
        manifest = json.loads(
            _s3.get_object(Bucket=RAW_BUCKET, Key=manifest_key)["Body"].read())
    except Exception as exc:  # noqa: BLE001
        log.error("manifiesto ilegible", extra={"ctx_key": manifest_key})
        store.mark_batch(folder, store.BatchState.FAILED,
                          reason=f"manifiesto ilegible: {type(exc).__name__}")
        _metric("ManifestUnreadable", 1)
        return {"error": "manifiesto ilegible", "batch": folder}

    files = manifest.get("files") or []
    if not isinstance(files, list) or not files:
        store.mark_batch(folder, store.BatchState.FAILED,
                          reason="el manifiesto no lista ficheros en 'files'")
        _metric("ManifestInvalid", 1)
        return {"error": "manifiesto sin 'files'", "batch": folder}

    store.record_manifest(folder, files,
                               int(manifest.get("total_requests", 0) or 0))
    log.info("manifiesto recibido", extra={
        "ctx_batch": folder, "ctx_files": len(files)})
    _metric("ManifestsReceived", 1)

    return _try_submit(folder, started)


def _try_submit(folder: str, started: float) -> Dict[str, Any]:
    """Envia el lote si esta listo; si no, lo deja anotado y sale."""
    verdict = store.batch_verdict(folder)

    if verdict == "awaiting_parts":
        # No es un error: el sanitizer sigue trabajando. El barrido programado
        # lo recogera cuando acabe.
        store.mark_batch(folder, store.BatchState.AWAITING_PARTS)
        state = store.batch_state(folder) or {}
        log.info("lote incompleto, queda esperando", extra={
            "ctx_batch": folder,
            "ctx_clean": state.get("clean_parts", 0),
            "ctx_expected": state.get("expected_parts", 0)})
        return {"batch": folder, "status": "awaiting_parts",
                "clean": state.get("clean_parts", 0),
                "expected": state.get("expected_parts", 0)}

    if verdict == "quarantined":
        # Un lote es una unidad. Si una parte fue rechazada no se manda nada:
        # enviar solo las limpias seria normalizar que entren datos que no deben.
        store.mark_batch(folder, store.BatchState.QUARANTINED,
                          reason="alguna parte del lote fue rechazada")
        _metric("BatchesQuarantined", 1)
        log.error("lote en cuarentena: alguna parte fue rechazada",
                  extra={"ctx_batch": folder})
        return {"batch": folder, "status": "quarantined"}

    if verdict == "no_manifest":
        return {"batch": folder, "status": "no_manifest"}

    # --- listo ---
    try:
        store.claim_submission(folder)
    except store.AlreadySubmitted:
        # El otro camino se lo llevo. Salir limpio es lo correcto.
        log.info("envio ya reclamado por otra invocacion", extra={"ctx_batch": folder})
        return {"batch": folder, "status": "already_claimed"}

    return _submit_batch(folder, started)


def _submit_batch(folder: str, started: float) -> Dict[str, Any]:
    """Ensambla las partes limpias del lote y las manda a Anthropic."""
    state = store.batch_state(folder) or {}
    files = state.get("files") or []

    requests: List[Dict[str, Any]] = []
    ids_vistos = set()
    for name in files:
        # La ruta de la salida limpia se lee del registro de la parte, no se
        # construye. Asi el ensamblado no depende de una convencion de paths
        # que el sanitizer podria cambiar sin que nadie relacione las dos cosas.
        registro = store.part_state(f"{folder}/{name}")
        clean_key = (registro or {}).get("clean_key")
        if not clean_key:
            store.mark_batch(folder, store.BatchState.FAILED,
                              reason=f"la parte {name} no tiene salida limpia registrada")
            log.error("parte sin salida limpia registrada",
                      extra={"ctx_batch": folder, "ctx_file": name})
            return {"batch": folder, "status": "failed", "falta": name}

        try:
            body = _s3.get_object(Bucket=CLEAN_BUCKET, Key=clean_key)["Body"].read()
        except Exception:  # noqa: BLE001
            # Los counters ya dijeron que estaba, asi que esto no deberia
            # pasar; si pasa, fallar el lote es mejor que enviarlo incompleto.
            store.mark_batch(folder, store.BatchState.FAILED,
                              reason=f"no se pudo leer la salida limpia de {name}")
            log.error("salida limpia ilegible",
                      extra={"ctx_batch": folder, "ctx_file": name})
            return {"batch": folder, "status": "failed", "falta": name}

        for request in json.loads(body).get("requests", []):
            custom_id = request.get("custom_id")
            if custom_id in ids_vistos:
                # Al fusionar ficheros dos partes pueden traer el mismo id, y
                # Anthropic rechaza el POST entero sin decir cual. Se nombra.
                store.mark_batch(folder, store.BatchState.FAILED,
                                  reason=f"custom_id duplicado al fusionar: {custom_id}")
                log.error("custom_id duplicado entre partes", extra={
                    "ctx_batch": folder, "ctx_custom_id": custom_id,
                    "ctx_file": name})
                _metric("DuplicateCustomId", 1)
                return {"batch": folder, "status": "failed",
                        "custom_id_duplicado": custom_id}
            ids_vistos.add(custom_id)
            requests.append(request)

    if not requests:
        store.mark_batch(folder, store.BatchState.FAILED,
                          reason="el lote no tiene ninguna peticion")
        return {"batch": folder, "status": "failed", "reason": "sin peticiones"}

    try:
        remote_batch, headers = create_batch(requests)
        store.save_ratelimit(headers)
    except AnthropicError as exc:
        # Se devuelve a LISTO para que el barrido lo reintente.
        store.mark_batch(folder, store.BatchState.READY,
                          reason=f"envio fallido: {exc.codigo}")
        if exc.retry_after:
            store.save_ratelimit({"retry-after": str(exc.retry_after)})
        log.error("fallo al enviar el lote", extra={
            "ctx_batch": folder, "ctx_codigo": exc.codigo})
        _metric("SubmitsFailed", 1)
        return {"batch": folder, "status": "reintentable", "codigo": exc.codigo}

    store.mark_batch(folder, store.BatchState.SUBMITTED,
                      batch_ids=[remote_batch["id"]],
                      anthropic_batch_id=remote_batch["id"],
                      request_count=len(requests),
                      submitted_at=store.now_iso(),
                      expira_en=remote_batch.get("expires_at"))

    _metric("BatchesSubmitted", 1)
    _metric("RequestsSubmitted", len(requests))
    log.info("lote enviado", extra={
        "ctx_batch": folder, "ctx_anthropic_id": remote_batch["id"],
        "ctx_requests": len(requests),
        "ctx_ms": round((time.monotonic() - started) * 1000)})

    return {"batch": folder, "status": "submitted",
            "anthropic_batch_id": remote_batch["id"],
            "requests": len(requests)}


def sweep_pending() -> Dict[str, int]:
    """Camino 2: reintenta los lotes que quedaron esperando partes.

    Es la red de seguridad del caso "el manifiesto llego primero". Sin esto,
    un lote cuyo manifiesto se adelanto al sanitizer se queda quieto para
    siempre y nadie se entera.
    """
    resumen = {"reviewed": 0, "submitted": 0, "awaiting": 0, "closed": 0}
    for item in store.pending_batches(limit=50):
        folder = item.get("batch") or ""
        if not folder:
            continue
        resumen["reviewed"] += 1
        result = _try_submit(folder, time.monotonic())
        if result.get("status") == "submitted":
            resumen["submitted"] += 1
        elif result.get("status") == "awaiting_parts":
            resumen["awaiting"] += 1
        else:
            resumen["cerrados"] += 1
    return resumen
