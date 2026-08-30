"""λ CANARIO — la unica prueba falsable de que el sanitizer funciona.

Corre una vez por hora en dos fases:

    1. Comprueba el canary de la hora anterior. Si NO acabo en cuarentena,
       el sanitizer no esta bloqueando y suena la alarma.
    2. Planta uno nuevo en raw, con PANes de prueba conocidos.

Por que hace falta: "Macie no encontro nada" no es evidencia de nada, porque
no se puede distinguir de "Macie no miro". Esto si se puede: se sabe
exactamente que se planto y que tenia que pasar. Si deja de pasar, se sabe.

Los PANes son numeros de prueba publicados por las marcas para entornos de
integracion. No son datos de tarjeta reales y nunca lo han sido.
"""
import json
import time
from typing import Any, Dict, List

import boto3

import store
from cases import CASES
from config import CANARY_PREFIX, ENVIRONMENT, RAW_BUCKET, require
from logs import get_logger
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Canary"


def _metric(name: str, value: float) -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": name, "Value": value, "Unit": "Count",
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        log.warning("no se pudo publicar la metrica", extra={"ctx_metrica": name})


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("RAW_BUCKET", "BATCHES_TABLE")
    started = time.monotonic()

    verdict = _check_previous()
    plantado = _plant()

    resumen = {**verdict, "plantado": plantado,
               "ms": round((time.monotonic() - started) * 1000)}
    log.info("canary", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen


def _check_previous() -> Dict[str, Any]:
    """Mira como acabo el ultimo canary plantado."""
    previous = store.get("__canary__")
    if not previous or not previous.get("last_batch"):
        _metric("CanaryWithoutBaseline", 1)
        return {"verdict": "sin canary anterior con el que comparar"}

    batch_id = previous["last_batch"]
    batch = store.get(batch_id)

    if not batch:
        # El sanitizer ni lo registro: o no se disparo, o murio antes.
        _metric("CanaryNotProcessed", 1)
        log.error("el canary anterior no fue procesado: el sanitizer no se disparo",
                  extra={"ctx_batch_id": batch_id})
        return {"verdict": "FALLO", "reason": "el canary no llego al sanitizer"}

    state = batch.get("status")
    if state == Status.QUARANTINED:
        _metric("CanaryBlocked", 1)
        return {"verdict": "OK", "batch_id": batch_id, "status": state}

    # Cualquier otro estado significa que unos PANes de prueba pasaron el gate.
    _metric("CanaryNotBlocked", 1)
    log.error("EL SANITIZER NO BLOQUEO EL CANARIO", extra={
        "ctx_batch_id": batch_id, "ctx_status": state})
    return {"verdict": "FALLO", "batch_id": batch_id, "status": state,
            "reason": "PANes de prueba superaron el gate"}


def _plant() -> str:
    """Deja un lote nuevo con PANes de prueba en raw."""
    brand = store.now_iso().replace(":", "").replace("-", "")
    key_ = f"{CANARY_PREFIX}{brand}.json"

    documento = {
        "requests": [
            {"custom_id": f"canary-{i}", "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": text}],
            }}
            for i, (_name, text) in enumerate(CASES)
        ],
        "metadata": {"canary": True, "plantado_en": store.now_iso()},
    }

    _s3.put_object(
        Bucket=RAW_BUCKET, Key=key_,
        Body=json.dumps(documento, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        Metadata={"canary": "true"})

    response = _s3.head_object(Bucket=RAW_BUCKET, Key=key_)
    etag = response["ETag"].strip('"')

    # El id se calcula igual que en el sanitizer para poder buscarlo despues.
    import hashlib
    batch_id = "b_" + hashlib.sha256(
        f"{RAW_BUCKET}/{key_}/{etag}".encode("utf-8")).hexdigest()[:24]

    store.update("__canary__", last_batch=batch_id, last_key=key_,
                 plantado_en=store.now_iso(), cases=len(CASES))
    log.info("canary plantado", extra={"ctx_batch_id": batch_id, "ctx_casos": len(CASES)})
    return batch_id
