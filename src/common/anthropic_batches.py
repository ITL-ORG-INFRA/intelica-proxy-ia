"""Cliente de la Message Batches API.

Dos cosas que no son evidentes y que este modulo se toma en serio:

1. Se leen SIEMPRE las cabeceras de la respuesta. anthropic-ratelimit-* y
   retry-after son la unica forma de saber cuanto margen queda; sin ellas solo
   se puede reaccionar al 429, que es tarde.

2. El reconciliador pide max_retries=0. Reintentar dentro de la Lambda cuando
   ya vas justo de limite es echar gasolina: es mejor fallar el tick y que el
   siguiente, ya frenado, lo recoja.
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

import anthropic

from config import ANTHROPIC_BASE_URL, ANTHROPIC_VERSION
from secret_store import anthropic_api_key

_clientes: Dict[int, anthropic.Anthropic] = {}


def client(max_retries: int = 2) -> anthropic.Anthropic:
    """Cliente reutilizado entre invocaciones del mismo contenedor."""
    if max_retries not in _clientes:
        _clientes[max_retries] = anthropic.Anthropic(
            api_key=anthropic_api_key(),
            base_url=ANTHROPIC_BASE_URL,
            default_headers={"anthropic-version": ANTHROPIC_VERSION},
            max_retries=max_retries,
            timeout=30.0,
        )
    return _clientes[max_retries]


class AnthropicError(Exception):
    def __init__(self, mensaje: str, status: int = 502, codigo: str = "upstream_error",
                 retry_after: Optional[float] = None):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status = status
        self.codigo = codigo
        self.retry_after = retry_after


def _traducir(exc: Exception) -> AnthropicError:
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        cabeceras = getattr(getattr(exc, "response", None), "headers", {}) or {}
        espera = cabeceras.get("retry-after")
        try:
            espera = float(espera) if espera else None
        except (TypeError, ValueError):
            espera = None
        if status in (401, 403):
            # Es NUESTRA credencial la que falla, no la del que pidio el lote.
            return AnthropicError("el proxy no pudo autenticarse contra Anthropic",
                                  502, "upstream_auth_error")
        if status == 429:
            return AnthropicError("Anthropic esta limitando el trafico", 429,
                                  "rate_limited", espera)
        if 400 <= status < 500:
            return AnthropicError(str(exc)[:500], 400, "invalid_upstream_request")
        return AnthropicError(str(exc)[:500], 502, "upstream_error", espera)
    if isinstance(exc, anthropic.APITimeoutError):
        return AnthropicError("Anthropic no respondio a tiempo", 504, "upstream_timeout")
    if isinstance(exc, anthropic.APIConnectionError):
        return AnthropicError("no se pudo conectar con Anthropic", 502, "upstream_unreachable")
    return AnthropicError(str(exc)[:500], 502, "upstream_error")


def _cabeceras(respuesta: Any) -> Dict[str, str]:
    crudas = getattr(respuesta, "headers", {}) or {}
    try:
        return {k.lower(): v for k, v in crudas.items()}
    except AttributeError:
        return {}


def crear_lote(requests: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Crea el lote. Devuelve (lote, cabeceras)."""
    try:
        crudo = client().messages.batches.with_raw_response.create(requests=requests)
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc) from exc
    return crudo.parse().model_dump(mode="json"), _cabeceras(crudo)


def listar_lotes(limit: int = 100, after_id: Optional[str] = None
                 ) -> Tuple[List[Dict[str, Any]], bool, Dict[str, str]]:
    """UNA llamada devuelve el estado de hasta 100 lotes.

    Aqui esta la diferencia entre un polling que no se nota y uno que te
    quema el presupuesto: 1 llamada por tick, no 1 por lote.
    """
    parametros: Dict[str, Any] = {"limit": limit}
    if after_id:
        parametros["after_id"] = after_id
    try:
        crudo = client(max_retries=0).messages.batches.with_raw_response.list(**parametros)
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc) from exc
    pagina = crudo.parse()
    lotes = [b.model_dump(mode="json") for b in pagina.data]
    return lotes, bool(getattr(pagina, "has_more", False)), _cabeceras(crudo)


def recuperar_lote(batch_id: str) -> Dict[str, Any]:
    try:
        return client().messages.batches.retrieve(batch_id).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc) from exc


def cancelar_lote(batch_id: str) -> Dict[str, Any]:
    try:
        return client().messages.batches.cancel(batch_id).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc) from exc


def stream_resultados(batch_id: str) -> Iterable[Dict[str, Any]]:
    """Itera los resultados uno a uno, sin materializar el JSONL entero."""
    try:
        for entrada in client().messages.batches.results(batch_id):
            yield entrada.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc) from exc
