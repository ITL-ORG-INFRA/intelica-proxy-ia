"""λ SUBMITTER — el unico rol que habla con Anthropic.

No tiene permiso de lectura sobre raw ni sobre quarantine: si alguien roba
esta credencial, no saca una tarjeta. Lee de clean, que ya paso el sanitizer y
el verificador.

Tiene DOS disparadores, y los dos acaban en el mismo sitio:

  1. Evento de S3 sobre raw/**/_MANIFEST.json — el productor ha terminado de
     subir el lote y avisa. Si todas las partes estan sanitizadas, se envia en
     el acto.

  2. Horario — barrido de seguridad. Cubre el caso en que el manifiesto llego
     ANTES de que el sanitizer acabara con alguna parte: ahi el evento no pudo
     enviar y hace falta que alguien vuelva a mirar.

El envio se reclama con una escritura condicional, asi que si los dos caminos
coinciden en el mismo instante solo uno envia. Sin eso el lote se enviaria dos
veces y se pagaria dos veces.
"""
import json
import time
from typing import Any, Dict, List
from urllib.parse import unquote_plus

import boto3

import store
from anthropic_batches import AnthropicError, crear_lote
from config import (
    CLEAN_BUCKET, ENVIRONMENT, INFLIGHT_LIMIT, RAW_BUCKET,
    SUBMIT_MAX_POR_TICK, require,
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


MANIFIESTO = "_MANIFEST.json"


def _key_del_evento(evento: Dict[str, Any]) -> str:
    """La clave del objeto si el evento viene de S3; vacio si es un tick.

    Solo se decodifica la de la notificacion nativa de S3. EventBridge la
    entrega sin codificar, y decodificarla convertiria un '+' literal en un
    espacio: el manifiesto pasaria a apuntar a un lote que no existe, y el
    submitter lo marcaria fallido con un nombre corrupto.
    """
    if evento.get("source") == "aws.s3" or "detail" in evento:
        return evento.get("detail", {}).get("object", {}).get("key", "")
    registros = evento.get("Records") or []
    if registros and "s3" in registros[0]:
        return unquote_plus(registros[0]["s3"].get("object", {}).get("key", ""))
    return ""


def lambda_handler(evento: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("CLEAN_BUCKET", "BATCHES_TABLE", "ANTHROPIC_SECRET_ARN")
    arranque = time.monotonic()

    key = _key_del_evento(evento)
    if key.endswith(MANIFIESTO):
        return _por_manifiesto(key, arranque)

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

    # Red de seguridad del camino del manifiesto: si alguno llego antes de que
    # el sanitizer acabara, aqui se recoge.
    pendientes = barrer_pendientes()

    _metrica("LotesEnviados", enviados)
    _metrica("LotesRetenidos", retenidos)
    _metrica("PeticionesEnVuelo", en_vuelo)
    _metrica("OcupacionCola", (en_vuelo / INFLIGHT_LIMIT * 100) if INFLIGHT_LIMIT else 0, "Percent")

    resumen = {"enviados": enviados, "retenidos": retenidos, "fallidos": fallidos,
               "en_vuelo": en_vuelo,
               "lotes_barridos": pendientes["revisados"],
               "lotes_enviados": pendientes["enviados"],
               "lotes_esperando": pendientes["esperando"],
               "ms": round((time.monotonic() - arranque) * 1000)}
    log.info("tick de envio", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen


# ---------------------------------------------------------------------------
# Camino 1: llego el manifiesto
# ---------------------------------------------------------------------------

def _por_manifiesto(manifest_key: str, arranque: float) -> Dict[str, Any]:
    """El productor ha cerrado el lote. Se envia si esta completo."""
    carpeta = store.lote_de(manifest_key)
    if not carpeta:
        log.error("manifiesto en la raiz del bucket, sin lote al que pertenecer",
                  extra={"ctx_key": manifest_key})
        return {"error": "manifiesto sin carpeta de lote"}

    try:
        manifiesto = json.loads(
            _s3.get_object(Bucket=RAW_BUCKET, Key=manifest_key)["Body"].read())
    except Exception as exc:  # noqa: BLE001
        log.error("manifiesto ilegible", extra={"ctx_key": manifest_key})
        store.marcar_lote(carpeta, store.EstadoLote.FALLIDO,
                          motivo=f"manifiesto ilegible: {type(exc).__name__}")
        _metrica("ManifiestoIlegible", 1)
        return {"error": "manifiesto ilegible", "lote": carpeta}

    ficheros = manifiesto.get("files") or []
    if not isinstance(ficheros, list) or not ficheros:
        store.marcar_lote(carpeta, store.EstadoLote.FALLIDO,
                          motivo="el manifiesto no lista ficheros en 'files'")
        _metrica("ManifiestoInvalido", 1)
        return {"error": "manifiesto sin 'files'", "lote": carpeta}

    store.registrar_manifiesto(carpeta, ficheros,
                               int(manifiesto.get("total_requests", 0) or 0))
    log.info("manifiesto recibido", extra={
        "ctx_lote": carpeta, "ctx_ficheros": len(ficheros)})
    _metrica("ManifiestosRecibidos", 1)

    return _intentar_envio(carpeta, arranque)


def _intentar_envio(carpeta: str, arranque: float) -> Dict[str, Any]:
    """Envia el lote si esta listo; si no, lo deja anotado y sale."""
    veredicto = store.veredicto_lote(carpeta)

    if veredicto == "esperando_partes":
        # No es un error: el sanitizer sigue trabajando. El barrido programado
        # lo recogera cuando acabe.
        store.marcar_lote(carpeta, store.EstadoLote.ESPERANDO_PARTES)
        estado = store.estado_lote(carpeta) or {}
        log.info("lote incompleto, queda esperando", extra={
            "ctx_lote": carpeta,
            "ctx_limpias": estado.get("partes_limpias", 0),
            "ctx_esperadas": estado.get("partes_esperadas", 0)})
        return {"lote": carpeta, "estado": "esperando_partes",
                "limpias": estado.get("partes_limpias", 0),
                "esperadas": estado.get("partes_esperadas", 0)}

    if veredicto == "cuarentena":
        # Un lote es una unidad. Si una parte fue rechazada no se manda nada:
        # enviar solo las limpias seria normalizar que entren datos que no deben.
        store.marcar_lote(carpeta, store.EstadoLote.CUARENTENA,
                          motivo="alguna parte del lote fue rechazada")
        _metrica("LotesEnCuarentena", 1)
        log.error("lote en cuarentena: alguna parte fue rechazada",
                  extra={"ctx_lote": carpeta})
        return {"lote": carpeta, "estado": "cuarentena"}

    if veredicto == "sin_manifiesto":
        return {"lote": carpeta, "estado": "sin_manifiesto"}

    # --- listo ---
    try:
        store.reclamar_envio(carpeta)
    except store.YaEnviado:
        # El otro camino se lo llevo. Salir limpio es lo correcto.
        log.info("envio ya reclamado por otra invocacion", extra={"ctx_lote": carpeta})
        return {"lote": carpeta, "estado": "ya_reclamado"}

    return _enviar_lote(carpeta, arranque)


def _enviar_lote(carpeta: str, arranque: float) -> Dict[str, Any]:
    """Ensambla las partes limpias del lote y las manda a Anthropic."""
    estado = store.estado_lote(carpeta) or {}
    ficheros = estado.get("ficheros") or []

    peticiones: List[Dict[str, Any]] = []
    ids_vistos = set()
    for nombre in ficheros:
        # La ruta de la salida limpia se lee del registro de la parte, no se
        # construye. Asi el ensamblado no depende de una convencion de rutas
        # que el sanitizer podria cambiar sin que nadie relacione las dos cosas.
        registro = store.estado_parte(f"{carpeta}/{nombre}")
        clean_key = (registro or {}).get("clean_key")
        if not clean_key:
            store.marcar_lote(carpeta, store.EstadoLote.FALLIDO,
                              motivo=f"la parte {nombre} no tiene salida limpia registrada")
            log.error("parte sin salida limpia registrada",
                      extra={"ctx_lote": carpeta, "ctx_fichero": nombre})
            return {"lote": carpeta, "estado": "fallido", "falta": nombre}

        try:
            cuerpo = _s3.get_object(Bucket=CLEAN_BUCKET, Key=clean_key)["Body"].read()
        except Exception:  # noqa: BLE001
            # Los contadores ya dijeron que estaba, asi que esto no deberia
            # pasar; si pasa, fallar el lote es mejor que enviarlo incompleto.
            store.marcar_lote(carpeta, store.EstadoLote.FALLIDO,
                              motivo=f"no se pudo leer la salida limpia de {nombre}")
            log.error("salida limpia ilegible",
                      extra={"ctx_lote": carpeta, "ctx_fichero": nombre})
            return {"lote": carpeta, "estado": "fallido", "falta": nombre}

        for peticion in json.loads(cuerpo).get("requests", []):
            custom_id = peticion.get("custom_id")
            if custom_id in ids_vistos:
                # Al fusionar ficheros dos partes pueden traer el mismo id, y
                # Anthropic rechaza el POST entero sin decir cual. Se nombra.
                store.marcar_lote(carpeta, store.EstadoLote.FALLIDO,
                                  motivo=f"custom_id duplicado al fusionar: {custom_id}")
                log.error("custom_id duplicado entre partes", extra={
                    "ctx_lote": carpeta, "ctx_custom_id": custom_id,
                    "ctx_fichero": nombre})
                _metrica("CustomIdDuplicado", 1)
                return {"lote": carpeta, "estado": "fallido",
                        "custom_id_duplicado": custom_id}
            ids_vistos.add(custom_id)
            peticiones.append(peticion)

    if not peticiones:
        store.marcar_lote(carpeta, store.EstadoLote.FALLIDO,
                          motivo="el lote no tiene ninguna peticion")
        return {"lote": carpeta, "estado": "fallido", "motivo": "sin peticiones"}

    try:
        lote_remoto, cabeceras = crear_lote(peticiones)
        store.save_ratelimit(cabeceras)
    except AnthropicError as exc:
        # Se devuelve a LISTO para que el barrido lo reintente.
        store.marcar_lote(carpeta, store.EstadoLote.LISTO,
                          motivo=f"envio fallido: {exc.codigo}")
        if exc.retry_after:
            store.save_ratelimit({"retry-after": str(exc.retry_after)})
        log.error("fallo al enviar el lote", extra={
            "ctx_lote": carpeta, "ctx_codigo": exc.codigo})
        _metrica("EnviosFallidos", 1)
        return {"lote": carpeta, "estado": "reintentable", "codigo": exc.codigo}

    store.marcar_lote(carpeta, store.EstadoLote.ENVIADO,
                      batch_ids=[lote_remoto["id"]],
                      anthropic_batch_id=lote_remoto["id"],
                      request_count=len(peticiones),
                      enviado_en=store.now_iso(),
                      expira_en=lote_remoto.get("expires_at"))

    _metrica("LotesEnviados", 1)
    _metrica("PeticionesEnviadas", len(peticiones))
    log.info("lote enviado", extra={
        "ctx_lote": carpeta, "ctx_anthropic_id": lote_remoto["id"],
        "ctx_peticiones": len(peticiones),
        "ctx_ms": round((time.monotonic() - arranque) * 1000)})

    return {"lote": carpeta, "estado": "enviado",
            "anthropic_batch_id": lote_remoto["id"],
            "peticiones": len(peticiones)}


def barrer_pendientes() -> Dict[str, int]:
    """Camino 2: reintenta los lotes que quedaron esperando partes.

    Es la red de seguridad del caso "el manifiesto llego primero". Sin esto,
    un lote cuyo manifiesto se adelanto al sanitizer se queda quieto para
    siempre y nadie se entera.
    """
    resumen = {"revisados": 0, "enviados": 0, "esperando": 0, "cerrados": 0}
    for item in store.lotes_pendientes(limit=50):
        carpeta = item.get("lote") or ""
        if not carpeta:
            continue
        resumen["revisados"] += 1
        resultado = _intentar_envio(carpeta, time.monotonic())
        if resultado.get("estado") == "enviado":
            resumen["enviados"] += 1
        elif resultado.get("estado") == "esperando_partes":
            resumen["esperando"] += 1
        else:
            resumen["cerrados"] += 1
    return resumen
