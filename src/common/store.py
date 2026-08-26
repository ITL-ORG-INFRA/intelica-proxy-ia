"""Estado de los lotes en DynamoDB.

Una tabla, tres tipos de item:

    batch_id = "msgb_..."      un lote y por donde va
    batch_id = "__inflight__"  contador atomico de peticiones encoladas
    batch_id = "__ratelimit__" ultimo estado de los limites de Anthropic

El contador aparte no es un capricho. El limite que muerde no es el polling,
es la cola en vuelo: pasarse del tier no devuelve un error, devuelve
expiraciones silenciosas a las 24 h. Hace falta poder preguntar "cuantas
peticiones tengo encoladas ahora mismo" y sumar sin carreras.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from config import BATCHES_TABLE, RESULTS_TTL_DAYS

_table = boto3.resource("dynamodb").Table(BATCHES_TABLE)

INFLIGHT_ITEM = "__inflight__"
RATELIMIT_ITEM = "__ratelimit__"


class Status:
    """Por donde va el lote. El orden es el del diagrama."""

    RECIBIDO = "recibido"          # esta en raw, el sanitizer aun no lo vio
    LIMPIO = "limpio"              # paso el gate, esta en clean
    VERIFICADO = "verificado"      # el verificador (2a implementacion) lo confirmo
    RETENIDO = "retenido"          # admision denegada: no cabe en la cola en vuelo
    ENVIADO = "enviado"            # POSTeado a Anthropic, procesando
    TERMINADO = "terminado"        # Anthropic acabo, faltan los resultados
    ENTREGADO = "entregado"        # resultados sanitizados y en S3 results
    CUARENTENA = "cuarentena"      # el gate lo aborto, nunca cruza
    EXPIRADO = "expirado"          # >24 h sin terminar
    FALLIDO = "fallido"

    #: lotes que la admision debe reintentar en cada tick
    ESPERANDO_ADMISION = (VERIFICADO, RETENIDO)

    #: lotes vivos en Anthropic, los que consumen cola
    EN_VUELO = (ENVIADO, TERMINADO)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ttl(days: int = 0) -> int:
    return int((datetime.now(timezone.utc)
                + timedelta(days=days or RESULTS_TTL_DAYS)).timestamp())


def to_plain(value: Any) -> Any:
    """DynamoDB devuelve Decimal; JSON no sabe serializarlo."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    return value


# --- ciclo de vida del lote ------------------------------------------------

def create(batch_id: str, raw_key: str, request_count: int, **extra: Any) -> Dict[str, Any]:
    item = {
        "batch_id": batch_id,
        "status": Status.RECIBIDO,
        "raw_key": raw_key,
        "request_count": request_count,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "inflight_counted": False,
        "ttl": _ttl(),
        **extra,
    }
    try:
        _table.put_item(Item=item, ConditionExpression="attribute_not_exists(batch_id)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Reentrega de S3/EventBridge sobre un lote ya visto: no es un error.
            return get(batch_id) or item
        raise
    return item


def get(batch_id: str) -> Optional[Dict[str, Any]]:
    item = _table.get_item(Key={"batch_id": batch_id}).get("Item")
    return to_plain(item) if item else None


def update(batch_id: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    names, values, sets = {}, {}, []
    for index, (key, value) in enumerate(fields.items()):
        names[f"#f{index}"] = key
        values[f":v{index}"] = value
        sets.append(f"#f{index} = :v{index}")
    _table.update_item(
        Key={"batch_id": batch_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def by_status(status: str, limit: int = 100) -> List[Dict[str, Any]]:
    response = _table.query(
        IndexName="status-index",
        KeyConditionExpression=Key("status").eq(status),
        ScanIndexForward=True,  # los mas antiguos primero: llevan mas esperando
        Limit=limit,
    )
    return [to_plain(i) for i in response.get("Items", [])]


def by_statuses(statuses, limit: int = 100) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for status in statuses:
        found.extend(by_status(status, limit=limit))
        if len(found) >= limit:
            break
    return found[:limit]


# --- cola en vuelo ---------------------------------------------------------

def inflight() -> int:
    item = _table.get_item(Key={"batch_id": INFLIGHT_ITEM}).get("Item") or {}
    return int(item.get("requests", 0))


def try_admit(batch_id: str, request_count: int, limit: int) -> bool:
    """Reserva sitio en la cola en vuelo. Devuelve False si no cabe.

    La condicion se evalua sobre el valor previo, asi que dos submitters
    concurrentes no pueden colarse los dos: DynamoDB solo deja pasar a uno.
    """
    headroom = limit - request_count
    if headroom < 0:
        return False  # el lote solo ya no cabe en el tier

    try:
        _table.update_item(
            Key={"batch_id": INFLIGHT_ITEM},
            UpdateExpression="ADD #r :n",
            ConditionExpression="attribute_not_exists(#r) OR #r <= :headroom",
            ExpressionAttributeNames={"#r": "requests"},
            ExpressionAttributeValues={":n": request_count, ":headroom": headroom},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise

    # Marcar el lote como contado permite devolver la reserva una sola vez.
    update(batch_id, inflight_counted=True)
    return True


def release(batch_id: str, request_count: int) -> bool:
    """Devuelve la reserva cuando el lote deja de estar en vuelo.

    Se apoya en la bandera del lote para no descontar dos veces si el
    reconciliador pasa dos veces por el mismo lote terminado.
    """
    try:
        _table.update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET inflight_counted = :false, updated_at = :now",
            ConditionExpression="inflight_counted = :true",
            ExpressionAttributeValues={":false": False, ":true": True, ":now": now_iso()},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False  # ya estaba descontado
        raise

    _table.update_item(
        Key={"batch_id": INFLIGHT_ITEM},
        UpdateExpression="ADD #r :n",
        ExpressionAttributeNames={"#r": "requests"},
        ExpressionAttributeValues={":n": -request_count},
    )
    return True


# --- limites de Anthropic --------------------------------------------------

def save_ratelimit(headers: Dict[str, str]) -> None:
    """Guarda lo que Anthropic dice de sus propios limites, para auto-frenar."""
    interesting = {
        k.lower(): v for k, v in (headers or {}).items()
        if k.lower().startswith("anthropic-ratelimit") or k.lower() == "retry-after"
    }
    if not interesting:
        return
    _table.put_item(Item={
        "batch_id": RATELIMIT_ITEM,
        "headers": interesting,
        "updated_at": now_iso(),
    })


def load_ratelimit() -> Dict[str, Any]:
    item = _table.get_item(Key={"batch_id": RATELIMIT_ITEM}).get("Item") or {}
    return to_plain(item.get("headers", {}))


# --- cursores --------------------------------------------------------------

def encode_cursor(last_key: Optional[Dict[str, Any]]) -> Optional[str]:
    if not last_key:
        return None
    raw = json.dumps(to_plain(last_key), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> Dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("cursor invalido") from exc
