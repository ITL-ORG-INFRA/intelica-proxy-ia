"""λ VERIFICADOR — segunda opinion sobre la zona limpia.

Lee lo que el sanitizer dio por bueno y lo vuelve a mirar con OTRO algoritmo
(detection2). Si encuentra algo, no es "una peticion mala": es que el
sanitizer fallo, y eso significa que hay CHD fuera del CDE.

Ante un hallazgo aqui la respuesta es borrar el objeto de clean y alarmar. Se
pierde la evidencia en clean a proposito: el original sigue en raw, dentro del
CDE, que es donde se investiga. Lo que no se puede es dejar un PAN reposando
en un bucket que esta fuera del entorno protegido.
"""
import json
import time
from typing import Any, Dict, List

import boto3

import store
from config import CLEAN_BUCKET, ENVIRONMENT, QUARANTINE_BUCKET, require
from detection2 import pans_in_stream, has_suspicious_digits
from logs import get_logger
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Verifier"

#: la ventana deslizante es ruidosa por diseno; con los primeros basta para decidir
MAX_FINDINGS_REPORTED = 50


def _metric(name: str, value: float) -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": name, "Value": value, "Unit": "Count",
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}],
        }])
    except Exception:  # noqa: BLE001
        log.warning("no se pudo publicar la metrica", extra={"ctx_metrica": name})


def _texts(documento: Dict[str, Any]):
    """Recorre el documento limpio sacando todo lo que sea texto."""
    for i, request in enumerate(documento.get("requests", [])):
        params = request.get("params", {})
        sistema = params.get("system")
        if isinstance(sistema, str):
            yield f"requests[{i}].params.system", sistema
        elif isinstance(sistema, list):
            for j, block in enumerate(sistema):
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    yield f"requests[{i}].params.system[{j}]", block["text"]
        for j, message in enumerate(params.get("messages", [])):
            content = message.get("content")
            base = f"requests[{i}].params.messages[{j}].content"
            if isinstance(content, str):
                yield base, content
            elif isinstance(content, list):
                for k, block in enumerate(content):
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        yield f"{base}[{k}]", block["text"]


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("CLEAN_BUCKET", "QUARANTINE_BUCKET", "BATCHES_TABLE")
    started = time.monotonic()

    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name") or CLEAN_BUCKET
    key = detail.get("object", {}).get("key", "")
    if not key:
        raise ValueError("el evento no trae la clave del objeto")

    body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    documento = json.loads(body)
    batch = documento.get("batch_id", key)

    findings: List[Dict[str, Any]] = []
    sospechas = 0
    reviewed = 0
    vistos = set()

    for where, text in _texts(documento):
        reviewed += 1
        # El system block se repite; verificarlo una vez es suficiente.
        key_ = hash(text)
        if key_ in vistos:
            continue
        vistos.add(key_)

        for encontrado in pans_in_stream(text):
            findings.append({"where": where, **encontrado})
            if len(findings) >= MAX_FINDINGS_REPORTED:
                break
        if has_suspicious_digits(text):
            sospechas += 1
        if len(findings) >= MAX_FINDINGS_REPORTED:
            break

    ms = round((time.monotonic() - started) * 1000)

    if findings:
        # Esto es un fallo del sanitizer, no del productor. Severidad maxima.
        _metric("SanitizerFailure", 1)
        _metric("PansInCleanZone", len(findings))
        log.error("PAN detectado en la zona limpia: el sanitizer fallo", extra={
            "ctx_batch_id": batch, "ctx_findings": len(findings),
            "ctx_where": findings[0]["where"], "ctx_brand": findings[0]["brand"]})

        _s3.put_object(
            Bucket=QUARANTINE_BUCKET, Key=f"quarantine/verifier/{batch}.json",
            Body=json.dumps({
                "batch_id": batch,
                "reason": "el verifier encontro PAN en datos ya sanitizados",
                "severidad": "fallo_de_control",
                "clean_key_deleted": f"{bucket}/{key}",
                "findings": findings,
                "detectado_en": store.now_iso(),
            }, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json")

        # Fuera de la zona limpia, ya.
        _s3.delete_object(Bucket=bucket, Key=key)

        store.update(batch, status=Status.QUARANTINED, clean_key=None,
                     reason="verifier: PAN en zona limpia")
        return {"batch_id": batch, "status": Status.QUARANTINED,
                "findings": len(findings), "ms": ms}

    store.update(batch, status=Status.VERIFIED, verified_at=store.now_iso(),
                 verified_texts=reviewed)
    _metric("BatchesVerified", 1)
    if sospechas:
        _metric("TextsWithLongDigits", sospechas)

    log.info("lote verificado", extra={
        "ctx_batch_id": batch, "ctx_texts": reviewed,
        "ctx_unicos": len(vistos), "ctx_sospechas": sospechas, "ctx_ms": ms})
    return {"batch_id": batch, "status": Status.VERIFIED, "textos": reviewed, "ms": ms}
