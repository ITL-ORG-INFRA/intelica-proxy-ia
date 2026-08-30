"""λ RECONCILER — polling con cabeza.

No hay webhooks en la Batches API: el polling es el unico mecanismo. Y no es
el cuello de botella, siempre que se haga bien:

    UNA llamada por tick, no una por lote.
    GET /v1/messages/batches?limit=100 devuelve el estado de hasta 100 lotes
    en una sola peticion. Con tick de 5 min son 288 peticiones al dia, 0,2 RPM
    sobre un presupuesto de 1.000 RPM: el 0,02%. Da igual que haya 3 lotes o 90.

Encima de eso, cadencia adaptativa: la mayoria de los lotes acaban en menos de
una hora, asi que no tiene sentido preguntar por uno que se envio hace 90
segundos, ni preguntar cada 5 minutos por uno que lleva 14 horas.
"""
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3

import store
from anthropic_batches import AnthropicError, list_batches
from config import BATCH_EXPIRY_HOURS, ENVIRONMENT, require
from logs import get_logger
from store import Status

log = get_logger(__name__)
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Reconciler"

#: (edad_minima_min, edad_maxima_min, cada_cuantos_min preguntar)
CADENCE = [
    (0, 5, None),      # recien enviado: no molestar
    (5, 60, 5),        # la mayoria acaba en esta franja
    (60, 24 * 60, 15),  # ya va largo: bajar el ritmo
]


def _metric(name: str, value: float, unidad: str = "Count") -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": name, "Value": value, "Unit": unidad,
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        pass


def _minutes_since(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        momento = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - momento).total_seconds() / 60


def should_poll(batch: Dict[str, Any]) -> bool:
    """Decide si este lote merece una consulta ahora mismo."""
    edad = _minutes_since(batch.get("submitted_at", ""))
    if edad >= BATCH_EXPIRY_HOURS * 60:
        return True  # hay que cerrarlo y alertar
    desde_ultima = _minutes_since(batch.get("polled_at", "") or batch.get("submitted_at", ""))
    for minimo, maximo, cada in CADENCE:
        if minimo <= edad < maximo:
            return cada is not None and desde_ultima >= cada
    return desde_ultima >= 15


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("BATCHES_TABLE", "ANTHROPIC_SECRET_ARN")
    started = time.monotonic()

    in_flight = store.by_statuses(Status.IN_FLIGHT, limit=200)
    if not in_flight:
        return {"skipped": "no hay lotes en vuelo"}

    pending_items = [b for b in in_flight if should_poll(b)]
    if not pending_items:
        # Nadie toca. Esto es la cadencia adaptativa haciendo su trabajo:
        # el tick corre, no gasta ni una peticion.
        log.info("tick sin consulta", extra={"ctx_in_flight": len(in_flight)})
        _metric("TicksWithoutPoll", 1)
        return {"skipped": "ninguno toca todavia", "in_flight": len(in_flight)}

    # --- UNA llamada para todos ---------------------------------------------
    remotes: Dict[str, Dict[str, Any]] = {}
    try:
        after_id = None
        for _ in range(5):  # tope de paginas, por si acaso
            batches, has_more, headers = list_batches(limit=100, after_id=after_id)
            store.save_ratelimit(headers)
            for remote in batches:
                remotes[remote["id"]] = remote
            if not has_more or not batches:
                break
            after_id = batches[-1]["id"]
    except AnthropicError as exc:
        if exc.retry_after:
            store.save_ratelimit({"retry-after": str(exc.retry_after)})
        _metric("PollsFailed", 1)
        log.error("no se pudo listar", extra={"ctx_code": exc.code})
        return {"error": exc.code}

    _metric("PollsPerformed", 1)

    completed, expired, unknown = 0, 0, 0
    now = store.now_iso()

    for batch in pending_items:
        batch_id = batch["batch_id"]
        remote_id = batch.get("anthropic_batch_id")
        remote = remotes.get(remote_id)

        if not remote:
            unknown += 1
            store.update(batch_id, polled_at=now)
            continue

        state = remote.get("processing_status")
        counters = remote.get("request_counts") or {}

        if state == "ended":
            store.update(batch_id, status=Status.COMPLETED, polled_at=now,
                         ended_at=remote.get("ended_at"), request_counts=counters,
                         results_url_available=bool(remote.get("results_url")))
            completed += 1
            log.info("lote terminado en Anthropic", extra={
                "ctx_batch_id": batch_id, "ctx_anthropic_id": remote_id})
            continue

        if _minutes_since(batch.get("submitted_at", "")) >= BATCH_EXPIRY_HOURS * 60:
            # A las 24 h la Batch API expira lo que no acabo. No avisa: hay que
            # detectarlo aqui, cerrarlo y alertar.
            store.release(batch_id, int(batch.get("request_count", 0)))
            store.update(batch_id, status=Status.EXPIRED, polled_at=now,
                         reason=f"sin terminar tras {BATCH_EXPIRY_HOURS} h")
            expired += 1
            _metric("BatchesExpired", 1)
            log.error("lote expirado", extra={"ctx_batch_id": batch_id})
            continue

        store.update(batch_id, polled_at=now, request_counts=counters)

    _metric("BatchesCompleted", completed)
    resumen = {"polled": len(pending_items), "completed": completed,
               "expired": expired, "unknown": unknown,
               "in_flight": len(in_flight),
               "ms": round((time.monotonic() - started) * 1000)}
    log.info("tick de reconciliacion", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen
