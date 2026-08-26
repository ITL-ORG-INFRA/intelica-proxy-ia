"""λ RECONCILIADOR — polling con cabeza.

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
from anthropic_batches import AnthropicError, listar_lotes
from config import BATCH_EXPIRY_HOURS, ENVIRONMENT, require
from logs import get_logger
from store import Status

log = get_logger(__name__)
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Reconciliador"

#: (edad_minima_min, edad_maxima_min, cada_cuantos_min preguntar)
CADENCIA = [
    (0, 5, None),      # recien enviado: no molestar
    (5, 60, 5),        # la mayoria acaba en esta franja
    (60, 24 * 60, 15),  # ya va largo: bajar el ritmo
]


def _metrica(nombre: str, valor: float, unidad: str = "Count") -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": nombre, "Value": valor, "Unit": unidad,
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        pass


def _minutos_desde(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        momento = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - momento).total_seconds() / 60


def toca_preguntar(lote: Dict[str, Any]) -> bool:
    """Decide si este lote merece una consulta ahora mismo."""
    edad = _minutos_desde(lote.get("enviado_en", ""))
    if edad >= BATCH_EXPIRY_HOURS * 60:
        return True  # hay que cerrarlo y alertar
    desde_ultima = _minutos_desde(lote.get("consultado_en", "") or lote.get("enviado_en", ""))
    for minimo, maximo, cada in CADENCIA:
        if minimo <= edad < maximo:
            return cada is not None and desde_ultima >= cada
    return desde_ultima >= 15


def lambda_handler(evento: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("BATCHES_TABLE", "ANTHROPIC_SECRET_ARN")
    arranque = time.monotonic()

    en_vuelo = store.by_statuses(Status.EN_VUELO, limit=200)
    if not en_vuelo:
        return {"omitido": "no hay lotes en vuelo"}

    pendientes = [b for b in en_vuelo if toca_preguntar(b)]
    if not pendientes:
        # Nadie toca. Esto es la cadencia adaptativa haciendo su trabajo:
        # el tick corre, no gasta ni una peticion.
        log.info("tick sin consulta", extra={"ctx_en_vuelo": len(en_vuelo)})
        _metrica("TicksSinConsulta", 1)
        return {"omitido": "ninguno toca todavia", "en_vuelo": len(en_vuelo)}

    # --- UNA llamada para todos ---------------------------------------------
    remotos: Dict[str, Dict[str, Any]] = {}
    try:
        after_id = None
        for _ in range(5):  # tope de paginas, por si acaso
            lotes, hay_mas, cabeceras = listar_lotes(limit=100, after_id=after_id)
            store.save_ratelimit(cabeceras)
            for remoto in lotes:
                remotos[remoto["id"]] = remoto
            if not hay_mas or not lotes:
                break
            after_id = lotes[-1]["id"]
    except AnthropicError as exc:
        if exc.retry_after:
            store.save_ratelimit({"retry-after": str(exc.retry_after)})
        _metrica("ConsultasFallidas", 1)
        log.error("no se pudo listar", extra={"ctx_codigo": exc.codigo})
        return {"error": exc.codigo}

    _metrica("ConsultasRealizadas", 1)

    terminados, expirados, sin_noticias = 0, 0, 0
    ahora = store.now_iso()

    for lote in pendientes:
        batch_id = lote["batch_id"]
        remoto_id = lote.get("anthropic_batch_id")
        remoto = remotos.get(remoto_id)

        if not remoto:
            sin_noticias += 1
            store.update(batch_id, consultado_en=ahora)
            continue

        estado = remoto.get("processing_status")
        contadores = remoto.get("request_counts") or {}

        if estado == "ended":
            store.update(batch_id, status=Status.TERMINADO, consultado_en=ahora,
                         terminado_en=remoto.get("ended_at"), request_counts=contadores,
                         results_url_disponible=bool(remoto.get("results_url")))
            terminados += 1
            log.info("lote terminado en Anthropic", extra={
                "ctx_batch_id": batch_id, "ctx_anthropic_id": remoto_id})
            continue

        if _minutos_desde(lote.get("enviado_en", "")) >= BATCH_EXPIRY_HOURS * 60:
            # A las 24 h la Batch API expira lo que no acabo. No avisa: hay que
            # detectarlo aqui, cerrarlo y alertar.
            store.release(batch_id, int(lote.get("request_count", 0)))
            store.update(batch_id, status=Status.EXPIRADO, consultado_en=ahora,
                         motivo=f"sin terminar tras {BATCH_EXPIRY_HOURS} h")
            expirados += 1
            _metrica("LotesExpirados", 1)
            log.error("lote expirado", extra={"ctx_batch_id": batch_id})
            continue

        store.update(batch_id, consultado_en=ahora, request_counts=contadores)

    _metrica("LotesTerminados", terminados)
    resumen = {"consultados": len(pendientes), "terminados": terminados,
               "expirados": expirados, "sin_noticias": sin_noticias,
               "en_vuelo": len(en_vuelo),
               "ms": round((time.monotonic() - arranque) * 1000)}
    log.info("tick de reconciliacion", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen
