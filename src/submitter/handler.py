"""λ SUBMITTER — el unico rol que habla con Anthropic.

No tiene permiso de lectura sobre raw ni sobre quarantine: si alguien roba
esta credencial, no saca una tarjeta. Lee de clean, que ya paso el sanitizer y
el verificador.

Corre por horario, no por evento, porque la admision puede decir que no. Un
lote retenido no se pierde: se queda esperando y el siguiente tick lo reintenta.
"""
import json
import time
from typing import Any, Dict

import boto3

import store
from anthropic_batches import AnthropicError, crear_lote
from config import (
    CLEAN_BUCKET, ENVIRONMENT, INFLIGHT_LIMIT, SUBMIT_MAX_POR_TICK, require,
)
from logs import get_logger
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Submitter"


def _metrica(nombre: str, valor: float, unidad: str = "Count") -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": nombre, "Value": valor, "Unit": unidad,
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        pass


def _frenado() -> float:
    """Segundos que Anthropic pidio esperar la ultima vez, si es que lo pidio."""
    cabeceras = store.load_ratelimit()
    try:
        return float(cabeceras.get("retry-after", 0))
    except (TypeError, ValueError):
        return 0.0


def lambda_handler(evento: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("CLEAN_BUCKET", "BATCHES_TABLE", "ANTHROPIC_SECRET_ARN")
    arranque = time.monotonic()

    espera = _frenado()
    if espera > 0:
        # Auto-frenado: Anthropic ya dijo que esperasemos. Insistir solo
        # empeora la ventana de rate limit.
        log.warning("tick omitido por retry-after", extra={"ctx_retry_after": espera})
        _metrica("TicksFrenados", 1)
        return {"omitido": "retry-after", "segundos": espera}

    candidatos = store.by_statuses(Status.ESPERANDO_ADMISION, limit=50)
    # Los mas antiguos primero: un lote retenido no puede quedarse atras para
    # siempre porque vayan llegando otros nuevos.
    candidatos.sort(key=lambda b: b.get("created_at", ""))

    enviados, retenidos, fallidos = 0, 0, 0
    en_vuelo = store.inflight()

    for lote in candidatos:
        if enviados >= SUBMIT_MAX_POR_TICK:
            break
        batch_id = lote["batch_id"]
        cuantas = int(lote.get("request_count", 0))

        if lote.get("es_canario"):
            # El canario prueba el sanitizer, no la Batch API. Si llega hasta
            # aqui es que el gate fallo, y enviarlo solo agravaria el fallo.
            store.update(batch_id, status=Status.FALLIDO,
                         motivo="canario detenido en el submitter: el gate no lo bloqueo")
            log.error("canario detenido en el submitter", extra={"ctx_batch_id": batch_id})
            _metrica("CanarioDetenidoEnSubmitter", 1)
            continue

        if not store.try_admit(batch_id, cuantas, INFLIGHT_LIMIT):
            if lote.get("status") != Status.RETENIDO:
                store.update(batch_id, status=Status.RETENIDO,
                             motivo=f"cola en vuelo llena ({en_vuelo}/{INFLIGHT_LIMIT})")
            retenidos += 1
            continue

        try:
            clean_key = lote.get("clean_key")
            cuerpo = _s3.get_object(Bucket=CLEAN_BUCKET, Key=clean_key)["Body"].read()
            documento = json.loads(cuerpo)

            lote_remoto, cabeceras = crear_lote(documento["requests"])
            store.save_ratelimit(cabeceras)

            store.update(
                batch_id,
                status=Status.ENVIADO,
                anthropic_batch_id=lote_remoto["id"],
                enviado_en=store.now_iso(),
                expira_en=lote_remoto.get("expires_at"),
            )
            enviados += 1
            en_vuelo += cuantas
            log.info("lote enviado", extra={
                "ctx_batch_id": batch_id, "ctx_anthropic_id": lote_remoto["id"],
                "ctx_peticiones": cuantas, "ctx_en_vuelo": en_vuelo})

        except AnthropicError as exc:
            # La reserva se devuelve: si no se envio, no ocupa cola.
            store.release(batch_id, cuantas)
            fallidos += 1
            if exc.retry_after:
                store.save_ratelimit({"retry-after": str(exc.retry_after)})
            store.update(batch_id, status=Status.VERIFICADO,
                         motivo=f"envio fallido: {exc.codigo}")
            log.error("fallo al enviar", extra={
                "ctx_batch_id": batch_id, "ctx_codigo": exc.codigo})
            if exc.codigo == "rate_limited":
                break  # no se insiste en el mismo tick
        except Exception:  # noqa: BLE001
            store.release(batch_id, cuantas)
            fallidos += 1
            store.update(batch_id, status=Status.VERIFICADO, motivo="fallo interno del envio")
            log.exception("fallo interno al enviar", extra={"ctx_batch_id": batch_id})

    _metrica("LotesEnviados", enviados)
    _metrica("LotesRetenidos", retenidos)
    _metrica("PeticionesEnVuelo", en_vuelo)
    _metrica("OcupacionCola", (en_vuelo / INFLIGHT_LIMIT * 100) if INFLIGHT_LIMIT else 0, "Percent")

    resumen = {"enviados": enviados, "retenidos": retenidos, "fallidos": fallidos,
               "en_vuelo": en_vuelo, "ms": round((time.monotonic() - arranque) * 1000)}
    log.info("tick de envio", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen
