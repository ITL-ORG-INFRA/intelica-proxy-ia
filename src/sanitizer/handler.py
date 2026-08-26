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
from typing import Any, Dict, List, Tuple

import boto3

import store
from config import (
    ALLOWED_MODELS, CANARY_PREFIX, CLEAN_BUCKET, DEFAULT_MAX_TOKENS, ENVIRONMENT,
    GATE_REJECT_ABS, GATE_REJECT_PCT, MAX_RAW_BYTES, MAX_REQUESTS_PER_BATCH,
    QUARANTINE_BUCKET, require,
)
from detectors import Hallazgo, escanear_texto
from envelope import EnvelopeInvalido, leer_raiz, normalizar_peticion
from logs import get_logger
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Sanitizer"


def _metrica(nombre: str, valor: float, unidad: str = "Count", **dims: str) -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": nombre,
            "Value": valor,
            "Unit": unidad,
            "Dimensions": [{"Name": k, "Value": v} for k, v in
                           {"Entorno": ENVIRONMENT, **dims}.items()],
        }])
    except Exception:  # noqa: BLE001 — una metrica no publicada no tumba el lote
        log.warning("no se pudo publicar la metrica", extra={"ctx_metrica": nombre})


def _origen(evento: Dict[str, Any]) -> Tuple[str, str, str]:
    """Saca (bucket, key, etag) del evento, venga de EventBridge o de S3."""
    if evento.get("source") == "aws.s3" or "detail" in evento:
        detalle = evento.get("detail", {})
        return (detalle.get("bucket", {}).get("name", ""),
                detalle.get("object", {}).get("key", ""),
                detalle.get("object", {}).get("etag", ""))
    registros = evento.get("Records") or []
    if registros:
        s3e = registros[0].get("s3", {})
        return (s3e.get("bucket", {}).get("name", ""),
                s3e.get("object", {}).get("key", ""),
                s3e.get("object", {}).get("eTag", ""))
    raise ValueError("evento no reconocido: ni EventBridge ni notificacion de S3")


def _id_lote(bucket: str, key: str, etag: str) -> str:
    """Id determinista: una reentrega del mismo objeto no crea un lote nuevo."""
    semilla = f"{bucket}/{key}/{etag}".encode("utf-8")
    return "b_" + hashlib.sha256(semilla).hexdigest()[:24]


class EscaneoCacheado:
    """El system block es ~72% del payload y es identico en todas las
    peticiones del lote. Escanearlo una vez por peticion es tirar el tiempo:
    se cachea por hash del texto."""

    def __init__(self) -> None:
        self._cache: Dict[str, List[Hallazgo]] = {}
        self.aciertos = 0
        self.escaneos = 0

    def escanear(self, texto: str, donde: str) -> List[Hallazgo]:
        clave = hashlib.sha256(texto.encode("utf-8")).hexdigest()
        if clave in self._cache:
            self.aciertos += 1
            # Los hallazgos se reetiquetan con la ruta de ESTA aparicion.
            return [Hallazgo(h.capa, h.tipo, donde, h.detalle, h.duro)
                    for h in self._cache[clave]]
        self.escaneos += 1
        hallazgos = escanear_texto(texto, donde)
        self._cache[clave] = [Hallazgo(h.capa, h.tipo, "", h.detalle, h.duro)
                              for h in hallazgos]
        return hallazgos


def lambda_handler(evento: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("RAW_BUCKET", "CLEAN_BUCKET", "QUARANTINE_BUCKET", "BATCHES_TABLE")
    arranque = time.monotonic()

    bucket, key, etag = _origen(evento)
    if not bucket or not key:
        raise ValueError("el evento no trae bucket/key")
    key = _unquote(key)
    lote = _id_lote(bucket, key, etag)

    log.info("lote recibido", extra={"ctx_batch_id": lote, "ctx_key": key})

    cabecera = _s3.head_object(Bucket=bucket, Key=key)
    tamano = cabecera["ContentLength"]
    if tamano > MAX_RAW_BYTES:
        # Fail-closed: si no cabe en memoria no se puede escanear entero, y un
        # lote a medio escanear no se envia. El upgrade es Distributed Map.
        return _cuarentena(lote, bucket, key, tamano, [], 0,
                           f"el objeto son {tamano} bytes y el maximo es {MAX_RAW_BYTES}")

    crudo = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        documento = json.loads(crudo)
    except json.JSONDecodeError as exc:
        return _cuarentena(lote, bucket, key, tamano, [], 0, f"JSON invalido: {exc}")
    finally:
        del crudo

    try:
        peticiones, metadata, hallazgos_raiz = leer_raiz(documento)
    except EnvelopeInvalido as exc:
        return _cuarentena(lote, bucket, key, tamano, [], 0, f"envelope invalido: {exc}")

    total = len(peticiones)
    if total > MAX_REQUESTS_PER_BATCH:
        return _cuarentena(lote, bucket, key, tamano, [], total,
                           f"{total} peticiones; el maximo es {MAX_REQUESTS_PER_BATCH}")

    es_canario = key.startswith(CANARY_PREFIX)
    store.create(lote, raw_key=f"{bucket}/{key}", request_count=total,
                 es_canario=es_canario)

    cache = EscaneoCacheado()
    limpias: List[Dict[str, Any]] = []
    rechazos: List[Dict[str, Any]] = []
    todos: List[Hallazgo] = list(hallazgos_raiz)
    ids_vistos = set()

    for indice, peticion in enumerate(peticiones):
        ruta = f"requests[{indice}]"
        try:
            normalizada, textos, hallazgos = normalizar_peticion(
                peticion, indice, ALLOWED_MODELS, DEFAULT_MAX_TOKENS)
        except EnvelopeInvalido as exc:
            rechazos.append({"indice": indice, "motivo": "envelope", "detalle": str(exc)})
            todos.append(Hallazgo(1, "estructura", ruta, str(exc)[:120]))
            continue

        custom_id = normalizada["custom_id"]
        if custom_id in ids_vistos:
            rechazos.append({"indice": indice, "motivo": "envelope",
                             "detalle": f"custom_id repetido: {custom_id}"})
            continue
        ids_vistos.add(custom_id)

        for donde, texto in textos:
            hallazgos.extend(cache.escanear(texto, donde))

        duros = [h for h in hallazgos if h.duro]
        if duros:
            # SAD nunca es almacenable, ni cifrado. Encontrarlo no es "una
            # peticion mala": es que el productor esta mandando datos que no
            # deberia tener. Se aborta el lote entero sin mirar el resto.
            todos.extend(hallazgos)
            _metrica("BloqueoDuro", 1)
            log.error("bloqueo duro: SAD detectado",
                      extra={"ctx_batch_id": lote, "ctx_donde": duros[0].donde,
                             "ctx_detalle": duros[0].detalle})
            return _cuarentena(lote, bucket, key, tamano, todos, total,
                               f"bloqueo duro en {ruta}: {duros[0].detalle}", duro=True)

        if hallazgos:
            todos.extend(hallazgos)
            rechazos.append({"indice": indice, "motivo": "contenido",
                             "hallazgos": [h.como_dict() for h in hallazgos]})
            continue

        limpias.append(normalizada)

    # --- GATE ---------------------------------------------------------------
    rechazadas = len(rechazos)
    porcentaje = (rechazadas / total * 100) if total else 0.0
    _metrica("PeticionesRechazadas", rechazadas)
    _metrica("PorcentajeRechazo", porcentaje, "Percent")

    if rechazadas and (porcentaje >= GATE_REJECT_PCT or rechazadas >= GATE_REJECT_ABS):
        # El gate no mira peticiones sueltas, mira el lote. Muchos rechazos no
        # son errores dispersos: son un productor mandando CHD de forma
        # sistematica, y dejar pasar "solo las buenas" seria normalizarlo.
        return _cuarentena(
            lote, bucket, key, tamano, todos, total,
            f"gate: {rechazadas}/{total} rechazadas ({porcentaje:.2f}%) "
            f"supera el umbral ({GATE_REJECT_PCT}% o {GATE_REJECT_ABS} absolutas)")

    if not limpias:
        return _cuarentena(lote, bucket, key, tamano, todos, total,
                           "no queda ninguna peticion limpia")

    # --- a la zona limpia ---------------------------------------------------
    clean_key = f"clean/{lote}.json"
    cuerpo = json.dumps({
        "batch_id": lote,
        "requests": limpias,
        "metadata": metadata,
        "sanitizado_en": store.now_iso(),
    }, ensure_ascii=False).encode("utf-8")

    _s3.put_object(Bucket=CLEAN_BUCKET, Key=clean_key, Body=cuerpo,
                   ContentType="application/json",
                   Metadata={"batch-id": lote, "requests": str(len(limpias))})

    store.update(lote, status=Status.LIMPIO, clean_key=clean_key,
                 request_count=len(limpias), rechazadas=rechazadas,
                 sanitizado_en=store.now_iso())

    ms = round((time.monotonic() - arranque) * 1000)
    _metrica("PeticionesLimpias", len(limpias))
    log.info("lote sanitizado", extra={
        "ctx_batch_id": lote, "ctx_limpias": len(limpias), "ctx_rechazadas": rechazadas,
        "ctx_escaneos": cache.escaneos, "ctx_cache_aciertos": cache.aciertos, "ctx_ms": ms,
    })
    return {"batch_id": lote, "estado": Status.LIMPIO,
            "limpias": len(limpias), "rechazadas": rechazadas, "ms": ms}


def _cuarentena(lote: str, bucket: str, key: str, tamano: int,
                hallazgos: List[Hallazgo], total: int, motivo: str,
                duro: bool = False) -> Dict[str, Any]:
    """Deja constancia en cuarentena y no cruza nada.

    El informe guarda PUNTEROS al objeto raw, no una copia. Copiar el payload
    aqui seria duplicar CHD para poder investigarlo: el original sigue en raw,
    dentro del CDE y con su propio ciclo de vida, que es donde debe mirarse.
    """
    informe = {
        "batch_id": lote,
        "motivo": motivo,
        "bloqueo_duro": duro,
        "origen": {"bucket": bucket, "key": key, "bytes": tamano},
        "peticiones": total,
        "hallazgos": [h.como_dict() for h in hallazgos][:1000],
        "resumen_por_capa": _resumen(hallazgos),
        "cuarentena_en": store.now_iso(),
    }
    _s3.put_object(
        Bucket=QUARANTINE_BUCKET,
        Key=f"quarantine/{lote}.json",
        Body=json.dumps(informe, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    try:
        store.update(lote, status=Status.CUARENTENA, motivo=motivo[:500])
    except Exception:  # noqa: BLE001 — el lote puede no estar registrado aun
        store.create(lote, raw_key=f"{bucket}/{key}", request_count=total,
                     status=Status.CUARENTENA, motivo=motivo[:500])

    _metrica("LotesEnCuarentena", 1)
    log.error("lote en cuarentena", extra={
        "ctx_batch_id": lote, "ctx_motivo": motivo, "ctx_duro": duro,
        "ctx_hallazgos": len(hallazgos)})
    return {"batch_id": lote, "estado": Status.CUARENTENA, "motivo": motivo}


def _resumen(hallazgos: List[Hallazgo]) -> Dict[str, int]:
    resumen: Dict[str, int] = {}
    for h in hallazgos:
        clave = f"capa{h.capa}_{h.tipo}"
        resumen[clave] = resumen.get(clave, 0) + 1
    return resumen


def _unquote(key: str) -> str:
    from urllib.parse import unquote_plus
    return unquote_plus(key)
