"""Capa 1 — envelope estricto, deny-by-default. Y capa 2 — nombres de campo.

Deny-by-default quiere decir que la lista es de lo que SE PERMITE. Cualquier
clave que no este en ella no se ignora ni se pasa: rechaza la peticion. Una
lista de lo prohibido siempre se queda corta; una de lo permitido, no.

De aqui sale ademas la lista de textos a escanear, cada uno con su ruta, para
que las capas 3-5 sepan decir donde estaba lo que encontraron.
"""
from typing import Any, Dict, List, Tuple

from detectors import Finding, forbidden_field
from normalize import normalize

#: claves admitidas en la raiz del fichero que deja el productor
_ROOT = {"requests", "metadata"}

#: claves admitidas en cada peticion
_REQUEST = {"custom_id", "params"}

#: parametros admitidos de la Messages API. Lo que no este aqui, fuera.
_PARAMS = {
    "model", "max_tokens", "messages", "system",
    "temperature", "top_p", "top_k", "stop_sequences",
    "output_config",
}

#: claves admitidas en un mensaje
_MESSAGE = {"role", "content"}

#: solo texto. image y document son capa 5. cache_control se admite porque
#: es lo que hace que un system de 4 KB repetido en 2.000 peticiones se cobre
#: una vez: quitarlo no rompe nada visible, solo multiplica la factura.
_BLOCK = {"type", "text", "cache_control"}

#: cache_control tambien es deny-by-default: es un objeto de dos claves con
#: valores de un conjunto cerrado, no un sitio donde colar texto libre.
_CACHE_CONTROL = {"type", "ttl"}
_CACHE_TYPES = {"ephemeral"}
_CACHE_TTL = {"5m", "1h"}

#: salida estructurada. El productor declara el esquema que quiere de vuelta.
_OUTPUT_CONFIG = {"format"}
_FORMAT = {"type", "schema", "name", "description"}
_FORMAT_TYPES = {"json_schema", "text"}

#: topes del esquema de salida. Un JSON Schema legitimo no se acerca a esto;
#: uno que si, es un intento de agotar la Lambda, no una peticion.
MAX_SCHEMA_NODES = 2000
MAX_SCHEMA_DEPTH = 20

_ROLES = {"user", "assistant"}

MAX_CUSTOM_ID = 64


class InvalidEnvelope(Exception):
    """El fichero no tiene la forma pactada. No es rechazo de una peticion:
    es que el lote entero no se puede ni leer."""


def _text_from_content(content: Any, path: str,
                        texts: List[Tuple[str, str]],
                        findings: List[Finding]) -> Any:
    """Normaliza el contenido de un mensaje y recoge lo escaneable."""
    if isinstance(content, str):
        clean_text = normalize(content)
        texts.append((path, clean_text))
        return clean_text

    if isinstance(content, list):
        out = []
        for i, block in enumerate(content):
            sub = f"{path}[{i}]"
            if not isinstance(block, dict):
                raise InvalidEnvelope(f"{sub}: se esperaba un objeto")
            extra = set(block) - _BLOCK
            if extra:
                raise InvalidEnvelope(f"{sub}: claves no permitidas {sorted(extra)}")
            type = block.get("type")
            if type != "text":
                # image / document / tool_result no cruzan la frontera.
                findings.append(Finding(5, "binary", sub, f"bloque '{type}'"))
                continue
            text = block.get("text")
            if not isinstance(text, str):
                raise InvalidEnvelope(f"{sub}.text: se esperaba texto")
            clean_text = normalize(text)
            texts.append((sub, clean_text))
            nuevo: Dict[str, Any] = {"type": "text", "text": clean_text}
            if "cache_control" in block:
                nuevo["cache_control"] = _check_cache_control(
                    block["cache_control"], f"{sub}.cache_control")
            out.append(nuevo)
        return out

    raise InvalidEnvelope(f"{path}: content debe ser texto o lista de bloques")


def _check_cache_control(value: Any, path: str) -> Dict[str, Any]:
    """cache_control con lista blanca. Solo forma, no hay texto que escanear."""
    if not isinstance(value, dict):
        raise InvalidEnvelope(f"{path}: se esperaba un objeto")
    extra = set(value) - _CACHE_CONTROL
    if extra:
        raise InvalidEnvelope(f"{path}: claves no permitidas {sorted(extra)}")
    type = value.get("type")
    if type not in _CACHE_TYPES:
        raise InvalidEnvelope(f"{path}.type: solo {sorted(_CACHE_TYPES)}")
    out = {"type": type}
    if "ttl" in value:
        if value["ttl"] not in _CACHE_TTL:
            raise InvalidEnvelope(f"{path}.ttl: solo {sorted(_CACHE_TTL)}")
        out["ttl"] = value["ttl"]
    return out


def _check_schema(obj: Any, path: str, texts: List[Tuple[str, str]],
                     findings: List[Finding], count: List[int],
                     hondo: int = 0) -> Any:
    """Recorre el JSON Schema de salida: lo escanea y lo devuelve tal cual.

    Un esquema de salida no es metadata inerte: es una instruccion. Si trae una
    propiedad llamada 'cvv', le esta pidiendo al modelo que extraiga el CVV del
    documento y lo devuelva en ese hueco. Por eso aqui el nombre de campo
    prohibido RECHAZA la peticion en vez de destruir el valor: destruirlo
    cambiaria en silencio el contrato que el productor cree tener.

    Y como el esquema entero viaja a Anthropic, cada cadena que lleva —claves,
    descripciones, enums— pasa por las capas 3-5 igual que el contenido.
    """
    if hondo > MAX_SCHEMA_DEPTH:
        raise InvalidEnvelope(f"{path}: esquema con mas de {MAX_SCHEMA_DEPTH} niveles")
    count[0] += 1
    if count[0] > MAX_SCHEMA_NODES:
        raise InvalidEnvelope(f"{path}: esquema con mas de {MAX_SCHEMA_NODES} nodos")

    if isinstance(obj, dict):
        out = {}
        for key_, value in obj.items():
            if not isinstance(key_, str):
                raise InvalidEnvelope(f"{path}: claves de texto")
            sub = f"{path}.{key_}"
            if forbidden_field(key_):
                findings.append(Finding(
                    2, "field", sub, f"el esquema de salida pide '{key_}'"))
                continue
            cleaned = normalize(key_)
            texts.append((sub, cleaned))
            out[cleaned] = _check_schema(value, sub, texts, findings,
                                              count, hondo + 1)
        return out
    if isinstance(obj, list):
        return [_check_schema(v, f"{path}[{i}]", texts, findings, count, hondo + 1)
                for i, v in enumerate(obj)]
    if isinstance(obj, str):
        clean_text = normalize(obj)
        texts.append((path, clean_text))
        return clean_text
    if isinstance(obj, (bool, int, float)) or obj is None:
        return obj
    raise InvalidEnvelope(f"{path}: tipo no admitido en un esquema")


def _check_output_config(value: Any, path: str, texts: List[Tuple[str, str]],
                           findings: List[Finding]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidEnvelope(f"{path}: se esperaba un objeto")
    extra = set(value) - _OUTPUT_CONFIG
    if extra:
        raise InvalidEnvelope(f"{path}: claves no permitidas {sorted(extra)}")
    fmt = value.get("format")
    if not isinstance(fmt, dict):
        raise InvalidEnvelope(f"{path}.format: se esperaba un objeto")
    extra = set(fmt) - _FORMAT
    if extra:
        raise InvalidEnvelope(f"{path}.format: claves no permitidas {sorted(extra)}")
    type = fmt.get("type")
    if type not in _FORMAT_TYPES:
        raise InvalidEnvelope(f"{path}.format.type: solo {sorted(_FORMAT_TYPES)}")

    out: Dict[str, Any] = {"type": type}
    for opcional in ("name", "description"):
        if opcional in fmt:
            if not isinstance(fmt[opcional], str):
                raise InvalidEnvelope(f"{path}.format.{opcional}: se esperaba texto")
            clean_text = normalize(fmt[opcional])
            texts.append((f"{path}.format.{opcional}", clean_text))
            out[opcional] = clean_text

    if type == "json_schema":
        schema_of = fmt.get("schema")
        if not isinstance(schema_of, dict):
            raise InvalidEnvelope(f"{path}.format.schema: obligatorio con json_schema")
        out["schema"] = _check_schema(
            schema_of, f"{path}.format.schema", texts, findings, [0])
    elif "schema" in fmt:
        raise InvalidEnvelope(f"{path}.format.schema: sobra con type '{type}'")

    return {"format": out}


def _check_nested_keys(obj: Any, path: str, findings: List[Finding]) -> Any:
    """Capa 2 sobre estructuras libres (metadata): el valor se destruye.

    A diferencia del texto libre, aqui el nombre del campo es inequivoco: si
    alguien manda {"cvv": "123"} no hay que interpretar nada. Se destruye el
    valor y se anota, sin tumbar la peticion.
    """
    if isinstance(obj, dict):
        out = {}
        for key_, value in obj.items():
            sub = f"{path}.{key_}"
            if forbidden_field(str(key_)):
                findings.append(Finding(2, "field", sub, f"campo '{key_}' destruido"))
                continue
            out[key_] = _check_nested_keys(value, sub, findings)
        return out
    if isinstance(obj, list):
        return [_check_nested_keys(v, f"{path}[{i}]", findings)
                for i, v in enumerate(obj)]
    return obj


def normalize_request(request: Any, index: int,
                        modelos_permitidos: set, max_tokens_defecto: int):
    """Valida una peticion y devuelve (peticion_normalizada, textos, hallazgos).

    Lanza EnvelopeInvalido si la forma no encaja: eso no es un rechazo por
    contenido sensible, es un fichero mal construido.
    """
    path = f"requests[{index}]"
    texts: List[Tuple[str, str]] = []
    findings: List[Finding] = []

    if not isinstance(request, dict):
        raise InvalidEnvelope(f"{path}: se esperaba un objeto")
    extra = set(request) - _REQUEST
    if extra:
        raise InvalidEnvelope(f"{path}: claves no permitidas {sorted(extra)}")

    custom_id = request.get("custom_id")
    if not isinstance(custom_id, str) or not custom_id or len(custom_id) > MAX_CUSTOM_ID:
        raise InvalidEnvelope(f"{path}.custom_id: texto de 1 a {MAX_CUSTOM_ID} caracteres")
    # custom_id viaja a Anthropic tal cual: tiene que ser opaco, no un DNI.
    if not custom_id.replace("-", "").replace("_", "").isalnum():
        raise InvalidEnvelope(
            f"{path}.custom_id: solo alfanumerico, guion y guion bajo — debe ser opaco")

    # El custom_id viaja a Anthropic tal cual, asi que hay que escanearlo como
    # cualquier otro texto. Validar solo el juego de caracteres no basta: un PAN
    # es alfanumerico, de modo que "4111111111111111" era un custom_id
    # perfectamente valido que cruzaba la frontera sin que ninguna capa lo
    # mirase. Todo lo que sale tiene que pasar por el filtro, no solo lo que
    # parece contenido.
    texts.append((f"{path}.custom_id", normalize(custom_id)))

    params = request.get("params")
    if not isinstance(params, dict):
        raise InvalidEnvelope(f"{path}.params: se esperaba un objeto")
    extra = set(params) - _PARAMS
    if extra:
        raise InvalidEnvelope(f"{path}.params: claves no permitidas {sorted(extra)}")

    modelo = params.get("model")
    if not isinstance(modelo, str) or not modelo:
        raise InvalidEnvelope(f"{path}.params.model es obligatorio")
    if modelos_permitidos and modelo not in modelos_permitidos:
        raise InvalidEnvelope(f"{path}.params.model: '{modelo}' no esta permitido")

    messages = params.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidEnvelope(f"{path}.params.messages: lista no vacia")

    out_messages = []
    for i, message in enumerate(messages):
        sub = f"{path}.params.messages[{i}]"
        if not isinstance(message, dict):
            raise InvalidEnvelope(f"{sub}: se esperaba un objeto")
        extra = set(message) - _MESSAGE
        if extra:
            raise InvalidEnvelope(f"{sub}: claves no permitidas {sorted(extra)}")
        if message.get("role") not in _ROLES:
            raise InvalidEnvelope(f"{sub}.role: debe ser 'user' o 'assistant'")
        content = _text_from_content(message.get("content"), f"{sub}.content",
                                        texts, findings)
        out_messages.append({"role": message["role"], "content": content})

    out_params: Dict[str, Any] = {
        "model": modelo,
        "messages": out_messages,
        "max_tokens": params.get("max_tokens", max_tokens_defecto),
    }
    if not isinstance(out_params["max_tokens"], int) or out_params["max_tokens"] < 1:
        raise InvalidEnvelope(f"{path}.params.max_tokens: entero positivo")

    if "system" in params:
        out_params["system"] = _text_from_content(
            params["system"], f"{path}.params.system", texts, findings)

    if "output_config" in params:
        out_params["output_config"] = _check_output_config(
            params["output_config"], f"{path}.params.output_config",
            texts, findings)

    for opcional in ("temperature", "top_p", "top_k", "stop_sequences"):
        if opcional in params:
            out_params[opcional] = params[opcional]

    return ({"custom_id": custom_id, "params": out_params}, texts, findings)


def read_root(documento: Any) -> Tuple[List[Any], Dict[str, Any], List[Finding]]:
    """Valida la raiz y devuelve (requests, metadata_saneada, hallazgos)."""
    if not isinstance(documento, dict):
        raise InvalidEnvelope("la raiz debe ser un objeto JSON")
    extra = set(documento) - _ROOT
    if extra:
        raise InvalidEnvelope(f"raiz: claves no permitidas {sorted(extra)}")

    requests = documento.get("requests")
    if not isinstance(requests, list) or not requests:
        raise InvalidEnvelope("'requests' debe ser una lista no vacia")

    findings: List[Finding] = []
    metadata = _check_nested_keys(documento.get("metadata", {}), "metadata", findings)
    return requests, metadata, findings
