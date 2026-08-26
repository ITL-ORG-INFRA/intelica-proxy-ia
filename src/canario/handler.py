"""λ CANARIO — la unica prueba falsable de que el sanitizer funciona.

Corre una vez por hora en dos fases:

    1. Comprueba el canario de la hora anterior. Si NO acabo en cuarentena,
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
from casos import CASOS
from config import CANARY_PREFIX, ENVIRONMENT, RAW_BUCKET, require
from logs import get_logger
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Canario"


def _metrica(nombre: str, valor: float) -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": nombre, "Value": valor, "Unit": "Count",
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        log.warning("no se pudo publicar la metrica", extra={"ctx_metrica": nombre})


def lambda_handler(evento: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("RAW_BUCKET", "BATCHES_TABLE")
    arranque = time.monotonic()

    veredicto = _comprobar_anterior()
    plantado = _plantar()

    resumen = {**veredicto, "plantado": plantado,
               "ms": round((time.monotonic() - arranque) * 1000)}
    log.info("canario", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen


def _comprobar_anterior() -> Dict[str, Any]:
    """Mira como acabo el ultimo canario plantado."""
    anterior = store.get("__canario__")
    if not anterior or not anterior.get("ultimo_lote"):
        _metrica("CanarioSinReferencia", 1)
        return {"veredicto": "sin canario anterior con el que comparar"}

    lote_id = anterior["ultimo_lote"]
    lote = store.get(lote_id)

    if not lote:
        # El sanitizer ni lo registro: o no se disparo, o murio antes.
        _metrica("CanarioNoProcesado", 1)
        log.error("el canario anterior no fue procesado: el sanitizer no se disparo",
                  extra={"ctx_batch_id": lote_id})
        return {"veredicto": "FALLO", "motivo": "el canario no llego al sanitizer"}

    estado = lote.get("status")
    if estado == Status.CUARENTENA:
        _metrica("CanarioBloqueado", 1)
        return {"veredicto": "OK", "batch_id": lote_id, "estado": estado}

    # Cualquier otro estado significa que unos PANes de prueba pasaron el gate.
    _metrica("CanarioNoBloqueado", 1)
    log.error("EL SANITIZER NO BLOQUEO EL CANARIO", extra={
        "ctx_batch_id": lote_id, "ctx_estado": estado})
    return {"veredicto": "FALLO", "batch_id": lote_id, "estado": estado,
            "motivo": "PANes de prueba superaron el gate"}


def _plantar() -> str:
    """Deja un lote nuevo con PANes de prueba en raw."""
    marca = store.now_iso().replace(":", "").replace("-", "")
    clave = f"{CANARY_PREFIX}{marca}.json"

    documento = {
        "requests": [
            {"custom_id": f"canario-{i}", "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": texto}],
            }}
            for i, (_nombre, texto) in enumerate(CASOS)
        ],
        "metadata": {"canario": True, "plantado_en": store.now_iso()},
    }

    _s3.put_object(
        Bucket=RAW_BUCKET, Key=clave,
        Body=json.dumps(documento, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        Metadata={"canario": "true"})

    respuesta = _s3.head_object(Bucket=RAW_BUCKET, Key=clave)
    etag = respuesta["ETag"].strip('"')

    # El id se calcula igual que en el sanitizer para poder buscarlo despues.
    import hashlib
    lote_id = "b_" + hashlib.sha256(
        f"{RAW_BUCKET}/{clave}/{etag}".encode("utf-8")).hexdigest()[:24]

    store.update("__canario__", ultimo_lote=lote_id, ultima_clave=clave,
                 plantado_en=store.now_iso(), casos=len(CASOS))
    log.info("canario plantado", extra={"ctx_batch_id": lote_id, "ctx_casos": len(CASOS)})
    return lote_id
