"""λ FETCHER + SANITIZER — la vuelta.

Baja el JSONL de resultados en streaming y le pasa un SEGUNDO pase de
sanitizacion antes de dejarlo en S3.

Por que sanitizar lo que vuelve, si lo que salio ya estaba limpio: porque el
control tiene que ser simetrico. Si el sanitizer de ida fallo, la unica
oportunidad de enterarse antes de que el dato llegue al consumidor es mirarlo
a la vuelta. Y un modelo puede generar una tirada de digitos que valide Luhn
por su cuenta. Un resultado con PAN no se escribe: se descarta y se alarma.
"""
import json
import os
import shutil
import time
from typing import Any, Dict, Iterable, List, Tuple

import boto3

import store
from anthropic_batches import AnthropicError, stream_resultados
from config import ENVIRONMENT, FETCH_MAX_POR_TICK, RESULTS_BUCKET, require
from detectors import escanear_texto
from logs import get_logger
from normalize import normalize
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Fetcher"

MARGEN_MS = 90_000
MIN_MS_POR_LOTE = 120_000


def _metrica(nombre: str, valor: float, unidad: str = "Count") -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": nombre, "Value": valor, "Unit": unidad,
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}]}])
    except Exception:  # noqa: BLE001
        pass


#: claves que admitimos en una entrada de resultado. Lo demas no se escribe.
_RESULTADO = {"custom_id", "result"}


def _textos_del_resultado(entrada: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
    resultado = entrada.get("result") or {}
    mensaje = resultado.get("message") or {}
    for i, bloque in enumerate(mensaje.get("content") or []):
        if isinstance(bloque, dict) and isinstance(bloque.get("text"), str):
            yield f"content[{i}]", bloque["text"]
    error = resultado.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        # Un mensaje de error de Anthropic puede citar la entrada que fallo.
        yield "error.message", error["message"]


def _valida_schema(entrada: Dict[str, Any]) -> str:
    if not isinstance(entrada, dict):
        return "la entrada no es un objeto"
    sobra = set(entrada) - _RESULTADO
    if sobra:
        return f"claves inesperadas: {sorted(sobra)}"
    if not isinstance(entrada.get("custom_id"), str):
        return "custom_id ausente o no es texto"
    if not isinstance(entrada.get("result"), dict):
        return "result ausente o no es objeto"
    tipo = entrada["result"].get("type")
    if tipo not in ("succeeded", "errored", "canceled", "expired"):
        return f"result.type desconocido: {tipo}"
    return ""


def lambda_handler(evento: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("RESULTS_BUCKET", "BATCHES_TABLE", "ANTHROPIC_SECRET_ARN")

    def restante_ms() -> int:
        try:
            return context.get_remaining_time_in_millis()
        except AttributeError:
            return 900_000

    terminados = store.by_status(Status.TERMINADO, limit=50)
    terminados.sort(key=lambda b: b.get("terminado_en", "") or b.get("created_at", ""))

    resumen = {"bajados": 0, "descartados_con_pan": 0, "descartados_schema": 0,
               "fallidos": 0, "pendientes": 0}

    for lote in terminados[:FETCH_MAX_POR_TICK]:
        if restante_ms() < MIN_MS_POR_LOTE:
            resumen["pendientes"] += 1
            log.warning("sin tiempo para otro lote, queda para el siguiente tick")
            break
        try:
            _procesar(lote, resumen)
        except AnthropicError as exc:
            resumen["fallidos"] += 1
            log.error("Anthropic fallo al bajar resultados", extra={
                "ctx_batch_id": lote["batch_id"], "ctx_codigo": exc.codigo})
        except Exception:  # noqa: BLE001 — un lote roto no tumba el tick
            resumen["fallidos"] += 1
            log.exception("fallo bajando el lote", extra={"ctx_batch_id": lote["batch_id"]})

    log.info("tick de descarga", extra={f"ctx_{k}": v for k, v in resumen.items()})
    return resumen


def _procesar(lote: Dict[str, Any], resumen: Dict[str, int]) -> None:
    batch_id = lote["batch_id"]
    remoto_id = lote.get("anthropic_batch_id")
    arranque = time.monotonic()

    ruta_tmp = f"/tmp/{batch_id}.jsonl"
    clave = f"results/{batch_id}.jsonl"
    contadores = {"succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
    con_pan: List[Dict[str, Any]] = []
    schema_malo = 0
    escritas = 0
    bytes_escritos = 0

    presupuesto = int(shutil.disk_usage("/tmp").free * 0.9)

    try:
        with open(ruta_tmp, "w", encoding="utf-8") as fichero:
            for entrada in stream_resultados(remoto_id):
                problema = _valida_schema(entrada)
                if problema:
                    schema_malo += 1
                    continue

                tipo = entrada["result"].get("type", "errored")
                contadores[tipo] = contadores.get(tipo, 0) + 1

                # --- segundo pase -------------------------------------------
                hallazgos = []
                for donde, texto in _textos_del_resultado(entrada):
                    hallazgos.extend(escanear_texto(normalize(texto), donde))
                if hallazgos:
                    con_pan.append({
                        "custom_id": entrada["custom_id"],
                        "hallazgos": [h.como_dict() for h in hallazgos],
                    })
                    continue  # no se escribe

                linea = json.dumps(entrada, ensure_ascii=False) + "\n"
                bytes_escritos += len(linea.encode("utf-8"))
                if bytes_escritos > presupuesto:
                    raise RuntimeError(
                        f"los resultados no caben en /tmp ({presupuesto} bytes). "
                        "Sube FETCHER_EPHEMERAL_MB y vuelve a desplegar.")
                fichero.write(linea)
                escritas += 1

        _s3.upload_file(ruta_tmp, RESULTS_BUCKET, clave, ExtraArgs={
            "ContentType": "application/jsonl",
            "Metadata": {"batch-id": batch_id, "entradas": str(escritas)}})
    finally:
        if os.path.exists(ruta_tmp):
            os.remove(ruta_tmp)

    if con_pan:
        # Que vuelva un PAN significa que el control de ida fallo. Severidad alta.
        _metrica("PanEnResultados", len(con_pan))
        log.error("resultados descartados por contener PAN", extra={
            "ctx_batch_id": batch_id, "ctx_descartados": len(con_pan)})
        _s3.put_object(
            Bucket=RESULTS_BUCKET, Key=f"results/{batch_id}.descartados.json",
            Body=json.dumps({"batch_id": batch_id, "descartados": con_pan},
                            ensure_ascii=False).encode("utf-8"),
            ContentType="application/json")

    store.release(batch_id, int(lote.get("request_count", 0)))
    store.update(batch_id, status=Status.ENTREGADO, results_key=clave,
                 results_bytes=bytes_escritos, result_counts=contadores,
                 entradas_escritas=escritas, descartadas_con_pan=len(con_pan),
                 descartadas_schema=schema_malo, entregado_en=store.now_iso())

    resumen["bajados"] += 1
    resumen["descartados_con_pan"] += len(con_pan)
    resumen["descartados_schema"] += schema_malo
    _metrica("LotesEntregados", 1)
    log.info("lote entregado", extra={
        "ctx_batch_id": batch_id, "ctx_entradas": escritas,
        "ctx_bytes": bytes_escritos,
        "ctx_segundos": round(time.monotonic() - arranque, 1)})
