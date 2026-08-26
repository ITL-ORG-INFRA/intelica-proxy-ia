"""λ VERIFICADOR — segunda opinion sobre la zona limpia.

Lee lo que el sanitizer dio por bueno y lo vuelve a mirar con OTRO algoritmo
(deteccion2). Si encuentra algo, no es "una peticion mala": es que el
sanitizer fallo, y eso significa que hay CHD fuera del CDE.

Ante un hallazgo aqui la respuesta es borrar el objeto de clean y alarmar. Se
pierde la evidencia en clean a proposito: el original sigue en raw, dentro del
CDE, que es donde se investiga. Lo que no se puede es dejar un PAN reposando
en un bucket que esta fuera del entorno protegido.
"""
import json
import time
from typing import Any, Dict, List

import boto3

import store
from config import CLEAN_BUCKET, ENVIRONMENT, QUARANTINE_BUCKET, require
from deteccion2 import buscar_panes, hay_digitos_sospechosos
from logs import get_logger
from store import Status

log = get_logger(__name__)
_s3 = boto3.client("s3")
_cw = boto3.client("cloudwatch")

NAMESPACE = "IntelicaProxyIA/Verificador"

#: la ventana deslizante es ruidosa por diseno; con los primeros basta para decidir
MAX_HALLAZGOS_REPORTADOS = 50


def _metrica(nombre: str, valor: float) -> None:
    try:
        _cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
            "MetricName": nombre, "Value": valor, "Unit": "Count",
            "Dimensions": [{"Name": "Entorno", "Value": ENVIRONMENT}],
        }])
    except Exception:  # noqa: BLE001
        log.warning("no se pudo publicar la metrica", extra={"ctx_metrica": nombre})


def _textos(documento: Dict[str, Any]):
    """Recorre el documento limpio sacando todo lo que sea texto."""
    for i, peticion in enumerate(documento.get("requests", [])):
        params = peticion.get("params", {})
        sistema = params.get("system")
        if isinstance(sistema, str):
            yield f"requests[{i}].params.system", sistema
        elif isinstance(sistema, list):
            for j, bloque in enumerate(sistema):
                if isinstance(bloque, dict) and isinstance(bloque.get("text"), str):
                    yield f"requests[{i}].params.system[{j}]", bloque["text"]
        for j, mensaje in enumerate(params.get("messages", [])):
            contenido = mensaje.get("content")
            base = f"requests[{i}].params.messages[{j}].content"
            if isinstance(contenido, str):
                yield base, contenido
            elif isinstance(contenido, list):
                for k, bloque in enumerate(contenido):
                    if isinstance(bloque, dict) and isinstance(bloque.get("text"), str):
                        yield f"{base}[{k}]", bloque["text"]


def lambda_handler(evento: Dict[str, Any], context: Any) -> Dict[str, Any]:
    require("CLEAN_BUCKET", "QUARANTINE_BUCKET", "BATCHES_TABLE")
    arranque = time.monotonic()

    detalle = evento.get("detail", {})
    bucket = detalle.get("bucket", {}).get("name") or CLEAN_BUCKET
    key = detalle.get("object", {}).get("key", "")
    if not key:
        raise ValueError("el evento no trae la clave del objeto")

    cuerpo = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    documento = json.loads(cuerpo)
    lote = documento.get("batch_id", key)

    hallazgos: List[Dict[str, Any]] = []
    sospechas = 0
    revisados = 0
    vistos = set()

    for donde, texto in _textos(documento):
        revisados += 1
        # El system block se repite; verificarlo una vez es suficiente.
        clave = hash(texto)
        if clave in vistos:
            continue
        vistos.add(clave)

        for encontrado in buscar_panes(texto):
            hallazgos.append({"donde": donde, **encontrado})
            if len(hallazgos) >= MAX_HALLAZGOS_REPORTADOS:
                break
        if hay_digitos_sospechosos(texto):
            sospechas += 1
        if len(hallazgos) >= MAX_HALLAZGOS_REPORTADOS:
            break

    ms = round((time.monotonic() - arranque) * 1000)

    if hallazgos:
        # Esto es un fallo del sanitizer, no del productor. Severidad maxima.
        _metrica("FalloDelSanitizer", 1)
        _metrica("PanesEnZonaLimpia", len(hallazgos))
        log.error("PAN detectado en la zona limpia: el sanitizer fallo", extra={
            "ctx_batch_id": lote, "ctx_hallazgos": len(hallazgos),
            "ctx_donde": hallazgos[0]["donde"], "ctx_marca": hallazgos[0]["marca"]})

        _s3.put_object(
            Bucket=QUARANTINE_BUCKET, Key=f"quarantine/verificador/{lote}.json",
            Body=json.dumps({
                "batch_id": lote,
                "motivo": "el verificador encontro PAN en datos ya sanitizados",
                "severidad": "fallo_de_control",
                "clean_key_borrado": f"{bucket}/{key}",
                "hallazgos": hallazgos,
                "detectado_en": store.now_iso(),
            }, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json")

        # Fuera de la zona limpia, ya.
        _s3.delete_object(Bucket=bucket, Key=key)

        store.update(lote, status=Status.CUARENTENA, clean_key=None,
                     motivo="verificador: PAN en zona limpia")
        return {"batch_id": lote, "estado": Status.CUARENTENA,
                "hallazgos": len(hallazgos), "ms": ms}

    store.update(lote, status=Status.VERIFICADO, verificado_en=store.now_iso(),
                 textos_verificados=revisados)
    _metrica("LotesVerificados", 1)
    if sospechas:
        _metrica("TextosConDigitosLargos", sospechas)

    log.info("lote verificado", extra={
        "ctx_batch_id": lote, "ctx_textos": revisados,
        "ctx_unicos": len(vistos), "ctx_sospechas": sospechas, "ctx_ms": ms})
    return {"batch_id": lote, "estado": Status.VERIFICADO, "textos": revisados, "ms": ms}
