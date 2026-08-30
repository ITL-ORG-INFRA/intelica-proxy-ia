"""λ FETCHER + SANITIZER — la vuelta.

Baja el JSONL de resultados en streaming y le pasa un SEGUNDO pase de
sanitizacion antes de dejarlo en S3.

Por que sanitizar lo que vuelve, si lo que salio ya estaba limpio: porque el
control tiene que ser simetrico. Si el sanitizer de ida fallo, la unica
oportunidad de enterarse antes de que el dato llegue al consumidor es mirarlo
a la vuelta. Y un modelo puede generar una tirada de digitos que valide Luhn
por su cuenta. Un resultado con PAN no se escribe: se descarta y se alarma.
"""
import json
import os
import shutil
import time
from typing import Any, Dict, Iterable, List, Tuple

import boto3

import store
from anthropic_batches import AnthropicError, stream_results
from config import ENVIRONMENT, FETCH_MAX_PER_TICK, RESULTS_BUCKET, require
from detectors import scan_text
from logs import get_logger
from normalize import normalize
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Fetcher"

MARGIN_MS = 90_000
MIN_MS_PER_BATCH = 120_000


def _metric(name: str, value: float, unidad: str = "Count") -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": name, "Value": value, "Unit": unidad,
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        pass


#: claves que admitimos en una entrada de resultado. Lo demas no se escribe.
_RESULT = {"custom_id", "result"}


def _texts_from_result(entry: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
    result = entry.get("result") or {}
    message = result.get("message") or {}
    for i, block in enumerate(message.get("content") or []):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            yield f"content[{i}]", block["text"]
    error = result.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        # Un mensaje de error de Anthropic puede citar la entrada que fallo.
        yield "error.message", error["message"]


def _validates_schema(entry: Dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return "la entrada no es un objeto"
    extra = set(entry) - _RESULT
    if extra:
        return f"claves inesperadas: {sorted(extra)}"
    if not isinstance(entry.get("custom_id"), str):
        return "custom_id ausente o no es texto"
    if not isinstance(entry.get("result"), dict):
        return "result ausente o no es objeto"
    type = entry["result"].get("type")
    if type not in ("succeeded", "errored", "canceled", "expired"):
        return f"result.type desconocido: {type}"
    return ""


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("RESULTS_BUCKET", "BATCHES_TABLE", "ANTHROPIC_SECRET_ARN")

    def remaining_ms() -> int:
        try:
            return context.get_remaining_time_in_millis()
        except AttributeError:
            return 900_000

    completed = store.by_status(Status.COMPLETED, limit=50)
    completed.sort(key=lambda b: b.get("ended_at", "") or b.get("created_at", ""))

    resumen = {"fetched": 0, "discarded_with_pan": 0, "discarded_schema": 0,
               "failed_count": 0, "pending": 0}

    for batch in completed[:FETCH_MAX_PER_TICK]:
        if remaining_ms() < MIN_MS_PER_BATCH:
            resumen["pending"] += 1
            log.warning("sin tiempo para otro lote, queda para el siguiente tick")
            break
        try:
            _process(batch, resumen)
        except AnthropicError as exc:
            resumen["failed_count"] += 1
            log.error("Anthropic fallo al bajar resultados", extra={
                "ctx_batch_id": batch["batch_id"], "ctx_code": exc.code})
        except Exception:  # noqa: BLE001 — un lote roto no tumba el tick
            resumen["failed_count"] += 1
            log.exception("fallo bajando el lote", extra={"ctx_batch_id": batch["batch_id"]})

    log.info("tick de descarga", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen


def _process(batch: Dict[str, Any], resumen: Dict[str, int]) -> None:
    batch_id = batch["batch_id"]
    remote_id = batch.get("anthropic_batch_id")
    started = time.monotonic()

    tmp_path = f"/tmp/{batch_id}.jsonl"
    key_ = f"results/{batch_id}.jsonl"
    counters = {"succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
    with_pan: List[Dict[str, Any]] = []
    bad_schema = 0
    escritas = 0
    bytes_escritos = 0

    presupuesto = int(shutil.disk_usage("/tmp").free * 0.9)

    try:
        with open(tmp_path, "w", encoding="utf-8") as file_:
            for entry in stream_results(remote_id):
                problema = _validates_schema(entry)
                if problema:
                    bad_schema += 1
                    continue

                type = entry["result"].get("type", "errored")
                counters[type] = counters.get(type, 0) + 1

                # --- segundo pase -------------------------------------------
                findings = []
                for where, text in _texts_from_result(entry):
                    findings.extend(scan_text(normalize(text), where))
                if findings:
                    with_pan.append({
                        "custom_id": entry["custom_id"],
                        "findings": [h.as_dict() for h in findings],
                    })
                    continue  # no se escribe

                linea = json.dumps(entry, ensure_ascii=False) + "\n"
                bytes_escritos += len(linea.encode("utf-8"))
                if bytes_escritos > presupuesto:
                    raise RuntimeError(
                        f"los resultados no caben en /tmp ({presupuesto} bytes). "
                        "Sube FETCHER_EPHEMERAL_MB y vuelve a desplegar.")
                file_.write(linea)
                escritas += 1

        _s3.upload_file(tmp_path, RESULTS_BUCKET, key_, ExtraArgs={
            "ContentType": "application/jsonl",
            "Metadata": {"batch-id": batch_id, "entries": str(escritas)}})
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if with_pan:
        # Que vuelva un PAN significa que el control de ida fallo. Severidad alta.
        _metric("PanInResults", len(with_pan))
        log.error("resultados descartados por contener PAN", extra={
            "ctx_batch_id": batch_id, "ctx_discarded": len(with_pan)})
        _s3.put_object(
            Bucket=RESULTS_BUCKET, Key=f"results/{batch_id}.discarded.json",
            Body=json.dumps({"batch_id": batch_id, "discarded": with_pan},
                            ensure_ascii=False).encode("utf-8"),
            ContentType="application/json")

    store.release(batch_id, int(batch.get("request_count", 0)))
    store.update(batch_id, status=Status.DELIVERED, results_key=key_,
                 results_bytes=bytes_escritos, result_counts=counters,
                 entries_written=escritas, discarded_with_pan=len(with_pan),
                 discarded_schema=bad_schema, delivered_at=store.now_iso())

    resumen["fetched"] += 1
    resumen["discarded_with_pan"] += len(with_pan)
    resumen["discarded_schema"] += bad_schema
    _metric("BatchesDelivered", 1)
    log.info("lote entregado", extra={
        "ctx_batch_id": batch_id, "ctx_entries": escritas,
        "ctx_bytes": bytes_escritos,
        "ctx_seconds": round(time.monotonic() - started, 1)})
