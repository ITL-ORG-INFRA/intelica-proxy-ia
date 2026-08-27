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


# ---------------------------------------------------------------------------
# Lotes de varias partes, disparados por manifiesto
#
# Un "lote" es una carpeta de raw con varios ficheros de datos y un
# _MANIFEST.json que se sube AL FINAL. El manifiesto es la senal de "ya esta
# todo": hasta que llega, no se sabe cuantas partes tiene el lote.
#
# Dos items:
#   lote#<prefijo>   el lote y por donde va
#   parte#<key>      una parte concreta, para no contarla dos veces
# ---------------------------------------------------------------------------

class EstadoLote:
    ESPERANDO_PARTES = "esperando_partes"   # falta que el sanitizer acabe alguna
    LISTO = "listo"                         # todas limpias, se puede enviar
    ENVIADO = "enviado"
    CUARENTENA = "cuarentena"               # alguna parte fue rechazada
    FALLIDO = "fallido"

    #: los que el barrido programado tiene que reintentar
    PENDIENTES = (ESPERANDO_PARTES, LISTO)


class YaContada(Exception):
    """La parte ya estaba registrada: reentrega de S3, no un error."""


class YaEnviado(Exception):
    """Otra invocacion se llevo el envio de este lote."""


def lote_de(key: str) -> str:
    """El lote es la carpeta que contiene la parte.

    entrada/lote-2026-08-27/parte-01.json  ->  entrada/lote-2026-08-27
    """
    return key.rsplit("/", 1)[0] if "/" in key else ""


def registrar_parte(lote: str, key: str, limpia: bool,
                    request_count: int = 0, clean_key: str = "") -> None:
    """Anota una parte y suma al contador del lote, exactamente una vez.

    El PutItem condicional es lo que hace idempotente el conteo: los eventos de
    S3 son at-least-once, y sin esto una reentrega inflaria el contador y el
    lote parece completo cuando no lo esta.
    """
    try:
        _table.put_item(
            Item={"batch_id": f"parte#{key}",
                  "lote": lote,
                  "status": "limpia" if limpia else "rechazada",
                  "created_at": now_iso(),
                  "request_count": request_count,
                  "clean_key": clean_key,
                  "ttl": _ttl()},
            ConditionExpression="attribute_not_exists(batch_id)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise YaContada(key) from exc
        raise

    campo = "partes_limpias" if limpia else "partes_rechazadas"
    _table.update_item(
        Key={"batch_id": f"lote#{lote}"},
        UpdateExpression=f"ADD {campo} :uno SET updated_at = :t, lote = :l",
        ExpressionAttributeValues={":uno": 1, ":t": now_iso(), ":l": lote})


def registrar_manifiesto(lote: str, ficheros: List[str],
                         total_requests: int = 0) -> None:
    """Anota que el manifiesto llego y cuantas partes se esperan."""
    _table.update_item(
        Key={"batch_id": f"lote#{lote}"},
        UpdateExpression=("SET manifiesto_visto = :si, partes_esperadas = :n, "
                          "ficheros = :f, total_requests = :tr, "
                          "updated_at = :t, lote = :l, "
                          "created_at = if_not_exists(created_at, :t)"),
        ExpressionAttributeValues={":si": True, ":n": len(ficheros), ":f": ficheros,
                                   ":tr": total_requests, ":t": now_iso(), ":l": lote})


def estado_parte(key: str) -> Optional[Dict[str, Any]]:
    """La parte registrada para esa clave de raw, con su clean_key."""
    item = _table.get_item(Key={"batch_id": f"parte#{key}"}).get("Item")
    return to_plain(item) if item else None


def estado_lote(lote: str) -> Optional[Dict[str, Any]]:
    item = _table.get_item(Key={"batch_id": f"lote#{lote}"}).get("Item")
    return to_plain(item) if item else None


def veredicto_lote(lote: str) -> str:
    """Que se puede hacer con el lote ahora mismo.

    Devuelve uno de: 'sin_manifiesto', 'esperando_partes', 'cuarentena', 'listo'.
    """
    item = estado_lote(lote)
    if not item:
        return "sin_manifiesto"
    if not item.get("manifiesto_visto"):
        # Hay partes procesadas pero nadie ha dicho todavia cuantas son en total.
        return "sin_manifiesto"

    esperadas = int(item.get("partes_esperadas", 0))
    limpias = int(item.get("partes_limpias", 0))
    rechazadas = int(item.get("partes_rechazadas", 0))

    if rechazadas:
        # Un lote es una unidad. Mandar solo las partes limpias seria
        # normalizar que entren datos que no deben entrar.
        return "cuarentena"
    if limpias < esperadas:
        return "esperando_partes"
    return "listo"


def marcar_lote(lote: str, estado: str, **extra: Any) -> None:
    campos = {"status": estado, "updated_at": now_iso(), "lote": lote, **extra}
    nombres, valores, sets = {}, {}, []
    for indice, (clave, valor) in enumerate(campos.items()):
        nombres[f"#f{indice}"] = clave
        valores[f":v{indice}"] = valor
        sets.append(f"#f{indice} = :v{indice}")
    _table.update_item(
        Key={"batch_id": f"lote#{lote}"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=nombres,
        ExpressionAttributeValues=valores)


def reclamar_envio(lote: str) -> None:
    """Reserva el envio del lote. Lanza YaEnviado si otro se lo llevo.

    Hace falta porque hay DOS caminos que pueden decidir enviar: el manifiesto
    al aterrizar, y la ultima parte al terminar de sanitizarse. Si coinciden en
    el mismo instante, sin esta condicional el lote se envia dos veces y se
    paga dos veces.
    """
    try:
        _table.update_item(
            Key={"batch_id": f"lote#{lote}"},
            UpdateExpression="SET #s = :enviando, reclamado_en = :t",
            ConditionExpression="attribute_not_exists(#s) OR #s IN (:esperando, :listo)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":enviando": "enviando", ":t": now_iso(),
                ":esperando": EstadoLote.ESPERANDO_PARTES, ":listo": EstadoLote.LISTO})
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise YaEnviado(lote) from exc
        raise


def lotes_pendientes(limit: int = 50) -> List[Dict[str, Any]]:
    """Lotes que el barrido programado debe reintentar.

    Cubre el caso en que el manifiesto llego ANTES de que el sanitizer acabara:
    ahi el evento del manifiesto no pudo enviar, y hace falta que alguien
    vuelva a mirar.
    """
    return by_statuses(EstadoLote.PENDIENTES, limit=limit)
