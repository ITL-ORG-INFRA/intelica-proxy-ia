"""λ SANITIZER — el proxy.

Lee el lote de S3 raw (dentro del CDE), le pasa las seis capas y decide. Si
pasa, lo escribe en S3 clean. Si no, se queda en cuarentena y no cruza.

Este rol NO tiene salida a internet ni permiso sobre el bucket clean mas alla
de escribir. Es la mitad de la frontera: el sanitizer ve CHD y no puede
hablar con fuera; el submitter habla con fuera y no puede ver CHD.

MVP: una sola Lambda, sin Distributed Map. El lote entero tiene que caber en
memoria, y por eso MAX_RAW_BYTES es un limite real, no decorativo.
"""
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

import boto3

import store
from config import (
    ALLOWED_MODELS, CANARY_PREFIX, CLEAN_BUCKET, DEFAULT_MAX_TOKENS, ENVIRONMENT,
    GATE_REJECT_ABS, GATE_REJECT_PCT, MAX_RAW_BYTES, MAX_REQUESTS_PER_BATCH,
    QUARANTINE_BUCKET, require,
)
from detectors import Finding, scan_text
from envelope import InvalidEnvelope, read_root, normalize_request
from logs import get_logger, scrub
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Sanitizer"

#: sufijo del fichero que cierra un lote. Lo procesa el submitter, no este.
MANIFEST = "_MANIFEST.json"


def _record_part(folder: str, key: str, cleaned: bool,
                     request_count: int = 0, clean_key: str = "") -> None:
    """Anota la parte en el lote al que pertenece.

    Si la carpeta esta vacia, el fichero no vive en un lote (se subio a pelo a
    la raiz) y no hay nada que contar: sigue funcionando como antes.
    """
    if not folder:
        return
    try:
        store.record_part(folder, key, cleaned=cleaned,
                              request_count=request_count, clean_key=clean_key)
    except store.AlreadyCounted:
        # Reentrega de S3. El conteo ya estaba hecho; no es un error.
        log.info("parte ya contada", extra={"ctx_key": key})
    except Exception:  # noqa: BLE001 — el verdict ya esta escrito y es lo que importa
        log.exception("no se pudo registrar la parte", extra={"ctx_key": key})


def _metric(name: str, value: float, unidad: str = "Count", **dims: str) -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": name,
            "Value": value,
            "Unit": unidad,
            "Dimensions": [{"Name": k, "Value": v} for k, v in
                           {"Entorno": ENVIRONMENT, **dims}.items()],
        }])
    except Exception:  # noqa: BLE001 — una metrica no publicada no tumba el lote
        log.warning("no se pudo publicar la metrica", extra={"ctx_metrica": name})


def _unquote(key: str) -> str:
    """Decodifica una clave de notificacion nativa de S3.

    unquote_plus y no unquote: S3 codifica el espacio como '+' en ese formato.
    """
    return unquote_plus(key) if key else ""


def _source(event: Dict[str, Any]) -> Tuple[str, str, str]:
    """Saca (bucket, key, etag) del evento, venga de EventBridge o de S3.

    La clave se decodifica SOLO en la rama de notificacion nativa de S3.
    EventBridge entrega detail.object.key tal cual; las notificaciones de S3
    (formato Records) la codifican, con '+' en lugar de espacio.

    Decodificar la de EventBridge convierte un '+' literal —perfectamente
    valido en una clave de S3— en un espacio, y a partir de ahi la clave no
    existe: el head_object devuelve 404 y el lote se pierde con un error que
    apunta a un fichero que nadie subio.
    """
    if event.get("source") == "aws.s3" or "detail" in event:
        detail = event.get("detail", {})
        return (detail.get("bucket", {}).get("name", ""),
                detail.get("object", {}).get("key", ""),      # sin decodificar
                detail.get("object", {}).get("etag", ""))
    registros = event.get("Records") or []
    if registros:
        s3e = registros[0].get("s3", {})
        return (s3e.get("bucket", {}).get("name", ""),
                _unquote(s3e.get("object", {}).get("key", "")),  # aqui SI
                s3e.get("object", {}).get("eTag", ""))
    raise ValueError("evento no reconocido: ni EventBridge ni notificacion de S3")


def _batch_id(bucket: str, key: str, etag: str) -> str:
    """Id determinista: una reentrega del mismo objeto no crea un lote nuevo."""
    semilla = f"{bucket}/{key}/{etag}".encode("utf-8")
    return "b_" + hashlib.sha256(semilla).hexdigest()[:24]


class CachedScan:
    """El system block es ~72% del payload y es identico en todas las
    peticiones del lote. Escanearlo una vez por peticion es tirar el tiempo:
    se cachea por hash del texto."""

    def __init__(self) -> None:
        self._cache: Dict[str, List[Finding]] = {}
        self.hits = 0
        self.scans = 0

    def scan(self, text: str, where: str) -> List[Finding]:
        key_ = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key_ in self._cache:
            self.hits += 1
            # Los hallazgos se reetiquetan con la ruta de ESTA aparicion.
            return [Finding(h.layer, h.type, where, h.detail, h.hard)
                    for h in self._cache[key_]]
        self.scans += 1
        findings = scan_text(text, where)
        self._cache[key_] = [Finding(h.layer, h.type, "", h.detail, h.hard)
                              for h in findings]
        return findings


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("RAW_BUCKET", "CLEAN_BUCKET", "QUARANTINE_BUCKET", "BATCHES_TABLE")
    started = time.monotonic()

    bucket, key, etag = _source(event)
    if not bucket or not key:
        raise ValueError("el evento no trae bucket/key")
    # El manifiesto lo procesa el submitter. Terraform filtra por sufijo, pero
    # un filtro se puede desconfigurar y esta guarda cuesta dos lineas: si el
    # sanitizer lo tratara como lote, lo mandaria a cuarentena por no tener
    # 'requests' y el envio nunca se dispararia.
    if key.endswith(MANIFEST):
        log.info("manifiesto ignorado por el sanitizer", extra={"ctx_key": key})
        return {"skipped": "es un manifiesto", "key": key}

    batch = _batch_id(bucket, key, etag)
    folder = store.batch_of(key)

    log.info("lote recibido", extra={"ctx_batch_id": batch, "ctx_key": key})

    cabecera = _s3.head_object(Bucket=bucket, Key=key)
    size = cabecera["ContentLength"]
    if size > MAX_RAW_BYTES:
        # Fail-closed: si no cabe en memoria no se puede escanear entero, y un
        # lote a medio escanear no se envia. El upgrade es Distributed Map.
        return _quarantine(batch, bucket, key, size, [], 0,
                           f"el objeto son {size} bytes y el maximo es {MAX_RAW_BYTES}")

    crudo = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        documento = json.loads(crudo)
    except json.JSONDecodeError as exc:
        return _quarantine(batch, bucket, key, size, [], 0, f"JSON invalido: {exc}")
    finally:
        del crudo

    try:
        requests, metadata, root_findings = read_root(documento)
    except InvalidEnvelope as exc:
        return _quarantine(batch, bucket, key, size, [], 0, f"envelope invalido: {exc}")

    total = len(requests)
    if total > MAX_REQUESTS_PER_BATCH:
        return _quarantine(batch, bucket, key, size, [], total,
                           f"{total} peticiones; el maximo es {MAX_REQUESTS_PER_BATCH}")

    is_canary = key.startswith(CANARY_PREFIX)
    store.create(batch, raw_key=f"{bucket}/{key}", request_count=total,
                 is_canary=is_canary)

    cache = CachedScan()
    clean_requests: List[Dict[str, Any]] = []
    rejections: List[Dict[str, Any]] = []
    all_found: List[Finding] = list(root_findings)
    ids_vistos = set()

    for index, request in enumerate(requests):
        path = f"requests[{index}]"
        try:
            normalizada, texts, findings = normalize_request(
                request, index, ALLOWED_MODELS, DEFAULT_MAX_TOKENS)
        except InvalidEnvelope as exc:
            rejections.append({"index": index, "reason": "envelope", "detail": str(exc)})
            all_found.append(Finding(1, "estructura", path, str(exc)[:120]))
            continue

        custom_id = normalizada["custom_id"]
        if custom_id in ids_vistos:
            rejections.append({"index": index, "reason": "envelope",
                             "detail": f"custom_id repetido: {custom_id}"})
            continue
        ids_vistos.add(custom_id)

        for where, text in texts:
            findings.extend(cache.scan(text, where))

        hard_ones = [h for h in findings if h.hard]
        if hard_ones:
            # SAD nunca es almacenable, ni cifrado. Encontrarlo no es "una
            # peticion mala": es que el productor esta mandando datos que no
            # deberia tener. Se aborta el lote entero sin mirar el resto.
            all_found.extend(findings)
            # El canary planta un track2 a proposito: que lo bloqueemos es la
            # buena noticia, no una incidencia. Si compartiera metrica con los
            # productores, la alarma sonaria cada dia por un exito y en dos
            # semanas nadie la miraria — que es justo cuando deja de servir.
            if is_canary:
                _metric("CanaryHardBlock", 1)
            else:
                _metric("HardBlock", 1)
            log.error("bloqueo duro: SAD detectado",
                      extra={"ctx_batch_id": batch, "ctx_where": hard_ones[0].where,
                             "ctx_detail": hard_ones[0].detail})
            return _quarantine(batch, bucket, key, size, all_found, total,
                               f"bloqueo duro en {path}: {hard_ones[0].detail}",
                               hard=True, rejections=rejections)

        if findings:
            all_found.extend(findings)
            rejections.append({"index": index, "reason": "contenido",
                             "findings": [h.as_dict() for h in findings]})
            continue

        clean_requests.append(normalizada)

    # --- GATE ---------------------------------------------------------------
    rejected_count = len(rejections)
    porcentaje = (rejected_count / total * 100) if total else 0.0
    _metric("RequestsRejected", rejected_count)
    _metric("RejectionRate", porcentaje, "Percent")

    if rejected_count and (porcentaje >= GATE_REJECT_PCT or rejected_count >= GATE_REJECT_ABS):
        # El gate no mira peticiones sueltas, mira el lote. Muchos rechazos no
        # son errores dispersos: son un productor mandando CHD de forma
        # sistematica, y dejar pasar "solo las buenas" seria normalizarlo.
        return _quarantine(
            batch, bucket, key, size, all_found, total,
            f"gate: {rejected_count}/{total} rechazadas ({porcentaje:.2f}%) "
            f"supera el umbral ({GATE_REJECT_PCT}% o {GATE_REJECT_ABS} absolutas)",
            rejections=rejections)

    if not clean_requests:
        return _quarantine(batch, bucket, key, size, all_found, total,
                           "no queda ninguna peticion limpia", rejections=rejections)

    # --- a la zona limpia ---------------------------------------------------
    clean_key = f"clean/{batch}.json"
    body = json.dumps({
        "batch_id": batch,
        "requests": clean_requests,
        "metadata": metadata,
        "sanitized_at": store.now_iso(),
    }, ensure_ascii=False).encode("utf-8")

    _s3.put_object(Bucket=CLEAN_BUCKET, Key=clean_key, Body=body,
                   ContentType="application/json",
                   Metadata={"batch-id": batch, "request-count": str(len(clean_requests))})

    store.update(batch, status=Status.CLEAN_, clean_key=clean_key,
                 request_count=len(clean_requests), rejected_count=rejected_count,
                 sanitized_at=store.now_iso())

    _write_status(batch, Status.CLEAN_, key, total, len(clean_requests),
                     rejections, all_found, reason="")
    _record_part(folder, key, cleaned=True,
                     request_count=len(clean_requests), clean_key=clean_key)

    ms = round((time.monotonic() - started) * 1000)
    _metric("RequestsClean", len(clean_requests))
    log.info("lote sanitizado", extra={
        "ctx_batch_id": batch, "ctx_clean": len(clean_requests), "ctx_rejected": rejected_count,
        "ctx_scans": cache.scans, "ctx_cache_hits": cache.hits, "ctx_ms": ms,
    })
    return {"batch_id": batch, "status": Status.CLEAN_,
            "clean": len(clean_requests), "rejected": rejected_count, "ms": ms}


def _quarantine(batch: str, bucket: str, key: str, size: int,
                findings: List[Finding], total: int, reason: str,
                hard: bool = False,
                rejections: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Deja constancia en cuarentena y no cruza nada.

    El informe guarda PUNTEROS al objeto raw, no una copia. Copiar el payload
    aqui seria duplicar CHD para poder investigarlo: el original sigue en raw,
    dentro del CDE y con su propio ciclo de vida, que es donde debe mirarse.
    """
    informe = {
        "batch_id": batch,
        "reason": reason,
        "hard_block": hard,
        "source": {"bucket": bucket, "key": key, "bytes": size},
        "request_count": total,
        "findings": [h.as_dict() for h in findings][:1000],
        "summary_by_layer": _summary(findings),
        "quarantined_at": store.now_iso(),
    }
    _s3.put_object(
        Bucket=QUARANTINE_BUCKET,
        Key=f"quarantine/{batch}.json",
        Body=json.dumps(informe, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    try:
        store.update(batch, status=Status.QUARANTINED, reason=reason[:500])
    except Exception:  # noqa: BLE001 — el lote puede no estar registrado aun
        store.create(batch, raw_key=f"{bucket}/{key}", request_count=total,
                     status=Status.QUARANTINED, reason=reason[:500])

    # Los rechazos viajan al parte: sin ellos el productor lee "rechazadas: 0"
    # junto a un motivo que dice "1/3 rechazadas", y no sabe que peticion mirar.
    _write_status(batch, Status.QUARANTINED, key, total, 0,
                     rejections or [], findings, reason)

    # La parte rechazada tambien se cuenta: es lo que hace que el lote pase a
    # CUARENTENA en vez de quedarse esperando una parte que nunca va a llegar
    # limpia. Un lote a medias es peor que un lote rechazado.
    _record_part(store.batch_of(key), key, cleaned=False)

    if key.startswith(CANARY_PREFIX):
        _metric("CanaryQuarantined", 1)
    else:
        _metric("BatchesQuarantined", 1)
    log.error("lote en cuarentena", extra={
        "ctx_batch_id": batch, "ctx_reason": reason, "ctx_hard": hard,
        "ctx_findings": len(findings)})
    return {"batch_id": batch, "status": Status.QUARANTINED, "reason": reason}


#: que hacer segun lo que disparo. Sin esto el productor recibe un verdict y
#: ninguna pista, y acaba escribiendo a infraestructura en cada rechazo.
_WHAT_TO_DO = {
    "pan": "Se detecto un numero de tarjeta en texto libre. Quitalo del origen: "
           "el proxy no lo enmascara, lo bloquea.",
    "sad_track": "Se detectaron datos de banda magnetica. Nunca son almacenables, "
                 "ni cifrados. Revisa de donde salen esos registros.",
    "sad_cvv": "Se detecto un CVV junto a palabras que lo identifican. "
               "Los codigos de verificacion no pueden salir del entorno.",
    "sad_pin": "Se detecto un PIN en contexto. Revisa el origen de los datos.",
    "field": "Un campo con nombre reservado (pan, cvv, track...) fue destruido. "
             "Renombralo o quitalo del payload.",
    "binary": "Se detecto contenido binario o base64. Solo se admite texto.",
    "estructura": "El lote no cumple el esquema. Solo se permiten las claves "
                  "documentadas: custom_id, params.model, params.max_tokens, "
                  "params.messages, params.system.",
}


def _write_status(batch: str, state: str, key: str, received_count: int,
                     clean_requests: int, rejections: List[Dict[str, Any]],
                     findings: List[Finding], reason: str) -> None:
    """Deja un parte de estado que el productor SI puede leer.

    Va al bucket clean, bajo 'status/', por dos razones:

      · clean esta fuera del CDE, asi que leerlo no exige la clave del CDE ni
        mete a quien lo lea en el alcance PCI.
      · el sanitizer ya escribe ahi, asi que no hace falta ampliarle permisos.

    El prefijo 'status/' no dispara al verifier, que escucha en 'clean/'.

    Lo que va dentro son conteos, capas y paths — nunca valores. Ademas se pasa
    todo por el mismo scrub que protege los logs: este documento cruza la
    frontera, y conviene que sea incapaz de llevar un PAN aunque alguien
    introduzca un mensaje de error descuidado mas adelante.
    """
    rejection_detail = []
    for rejection in rejections[:200]:
        entry = {"index": rejection.get("index"), "reason": rejection.get("reason")}
        if rejection.get("detail"):
            entry["detail"] = scrub(rejection["detail"])
        if rejection.get("findings"):
            entry["findings"] = [
                {"layer": h["layer"], "type": h["type"],
                 "where": scrub(h["where"]), "detail": scrub(h["detail"])}
                for h in rejection["findings"]
            ]
        rejection_detail.append(entry)

    types = {h.type for h in findings}
    consejos = [_WHAT_TO_DO[t] for t in sorted(types) if t in _WHAT_TO_DO]

    part = {
        "batch_id": batch,
        "status": state,
        "source": key,
        "request_counts": {
            "received": received_count,
            "clean": clean_requests,
            "rejected": len(rejections),
        },
        "reason": scrub(reason) if reason else "",
        "rejections": rejection_detail,
        "summary_by_layer": _summary(findings),
        "what_to_do": consejos,
        "updated_at": store.now_iso(),
    }
    if len(rejections) > 200:
        part["nota"] = f"se listan 200 de {len(rejections)} rechazos"

    try:
        _s3.put_object(
            Bucket=CLEAN_BUCKET, Key=f"status/{batch}.json",
            Body=json.dumps(part, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
            Metadata={"batch-id": batch, "status": state})
    except Exception:  # noqa: BLE001 — informar no puede tumbar la sanitizacion
        log.warning("no se pudo escribir el parte de estado",
                    extra={"ctx_batch_id": batch})


def _summary(findings: List[Finding]) -> Dict[str, int]:
    resumen: Dict[str, int] = {}
    for h in findings:
        key_ = f"layer{h.layer}_{h.type}"
        resumen[key_] = resumen.get(key_, 0) + 1
    return resumen
