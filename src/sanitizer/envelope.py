"""Capa 1 — envelope estricto, deny-by-default. Y capa 2 — nombres de campo.

Deny-by-default quiere decir que la lista es de lo que SE PERMITE. Cualquier
clave que no este en ella no se ignora ni se pasa: rechaza la peticion. Una
lista de lo prohibido siempre se queda corta; una de lo permitido, no.

De aqui sale ademas la lista de textos a escanear, cada uno con su ruta, para
que las capas 3-5 sepan decir donde estaba lo que encontraron.
"""
from typing import Any, Dict, List, Tuple

from detectors import Hallazgo, campo_prohibido
from normalize import normalize

#: claves admitidas en la raiz del fichero que deja el productor
_RAIZ = {"requests", "metadata"}

#: claves admitidas en cada peticion
_PETICION = {"custom_id", "params"}

#: parametros admitidos de la Messages API. Lo que no este aqui, fuera.
_PARAMS = {
    "model", "max_tokens", "messages", "system",
    "temperature", "top_p", "top_k", "stop_sequences",
}

#: claves admitidas en un mensaje
_MENSAJE = {"role", "content"}

#: solo texto. image y document son capa 5.
_BLOQUE = {"type", "text"}

_ROLES = {"user", "assistant"}

MAX_CUSTOM_ID = 64


class EnvelopeInvalido(Exception):
    """El fichero no tiene la forma pactada. No es rechazo de una peticion:
    es que el lote entero no se puede ni leer."""


def _texto_de_contenido(contenido: Any, ruta: str,
                        textos: List[Tuple[str, str]],
                        hallazgos: List[Hallazgo]) -> Any:
    """Normaliza el contenido de un mensaje y recoge lo escaneable."""
    if isinstance(contenido, str):
        limpio = normalize(contenido)
        textos.append((ruta, limpio))
        return limpio

    if isinstance(contenido, list):
        salida = []
        for i, bloque in enumerate(contenido):
            sub = f"{ruta}[{i}]"
            if not isinstance(bloque, dict):
                raise EnvelopeInvalido(f"{sub}: se esperaba un objeto")
            sobra = set(bloque) - _BLOQUE
            if sobra:
                raise EnvelopeInvalido(f"{sub}: claves no permitidas {sorted(sobra)}")
            tipo = bloque.get("type")
            if tipo != "text":
                # image / document / tool_result no cruzan la frontera.
                hallazgos.append(Hallazgo(5, "binario", sub, f"bloque '{tipo}'"))
                continue
            texto = bloque.get("text")
            if not isinstance(texto, str):
                raise EnvelopeInvalido(f"{sub}.text: se esperaba texto")
            limpio = normalize(texto)
            textos.append((sub, limpio))
            salida.append({"type": "text", "text": limpio})
        return salida

    raise EnvelopeInvalido(f"{ruta}: content debe ser texto o lista de bloques")


def _revisar_claves_anidadas(obj: Any, ruta: str, hallazgos: List[Hallazgo]) -> Any:
    """Capa 2 sobre estructuras libres (metadata): el valor se destruye.

    A diferencia del texto libre, aqui el nombre del campo es inequivoco: si
    alguien manda {"cvv": "123"} no hay que interpretar nada. Se destruye el
    valor y se anota, sin tumbar la peticion.
    """
    if isinstance(obj, dict):
        salida = {}
        for clave, valor in obj.items():
            sub = f"{ruta}.{clave}"
            if campo_prohibido(str(clave)):
                hallazgos.append(Hallazgo(2, "campo", sub, f"campo '{clave}' destruido"))
                continue
            salida[clave] = _revisar_claves_anidadas(valor, sub, hallazgos)
        return salida
    if isinstance(obj, list):
        return [_revisar_claves_anidadas(v, f"{ruta}[{i}]", hallazgos)
                for i, v in enumerate(obj)]
    return obj


def normalizar_peticion(peticion: Any, indice: int,
                        modelos_permitidos: set, max_tokens_defecto: int):
    """Valida una peticion y devuelve (peticion_normalizada, textos, hallazgos).

    Lanza EnvelopeInvalido si la forma no encaja: eso no es un rechazo por
    contenido sensible, es un fichero mal construido.
    """
    ruta = f"requests[{indice}]"
    textos: List[Tuple[str, str]] = []
    hallazgos: List[Hallazgo] = []

    if not isinstance(peticion, dict):
        raise EnvelopeInvalido(f"{ruta}: se esperaba un objeto")
    sobra = set(peticion) - _PETICION
    if sobra:
        raise EnvelopeInvalido(f"{ruta}: claves no permitidas {sorted(sobra)}")

    custom_id = peticion.get("custom_id")
    if not isinstance(custom_id, str) or not custom_id or len(custom_id) > MAX_CUSTOM_ID:
        raise EnvelopeInvalido(f"{ruta}.custom_id: texto de 1 a {MAX_CUSTOM_ID} caracteres")
    # custom_id viaja a Anthropic tal cual: tiene que ser opaco, no un DNI.
    if not custom_id.replace("-", "").replace("_", "").isalnum():
        raise EnvelopeInvalido(
            f"{ruta}.custom_id: solo alfanumerico, guion y guion bajo — debe ser opaco")

    # El custom_id viaja a Anthropic tal cual, asi que hay que escanearlo como
    # cualquier otro texto. Validar solo el juego de caracteres no basta: un PAN
    # es alfanumerico, de modo que "4111111111111111" era un custom_id
    # perfectamente valido que cruzaba la frontera sin que ninguna capa lo
    # mirase. Todo lo que sale tiene que pasar por el filtro, no solo lo que
    # parece contenido.
    textos.append((f"{ruta}.custom_id", normalize(custom_id)))

    params = peticion.get("params")
    if not isinstance(params, dict):
        raise EnvelopeInvalido(f"{ruta}.params: se esperaba un objeto")
    sobra = set(params) - _PARAMS
    if sobra:
        raise EnvelopeInvalido(f"{ruta}.params: claves no permitidas {sorted(sobra)}")

    modelo = params.get("model")
    if not isinstance(modelo, str) or not modelo:
        raise EnvelopeInvalido(f"{ruta}.params.model es obligatorio")
    if modelos_permitidos and modelo not in modelos_permitidos:
        raise EnvelopeInvalido(f"{ruta}.params.model: '{modelo}' no esta permitido")

    mensajes = params.get("messages")
    if not isinstance(mensajes, list) or not mensajes:
        raise EnvelopeInvalido(f"{ruta}.params.messages: lista no vacia")

    salida_mensajes = []
    for i, mensaje in enumerate(mensajes):
        sub = f"{ruta}.params.messages[{i}]"
        if not isinstance(mensaje, dict):
            raise EnvelopeInvalido(f"{sub}: se esperaba un objeto")
        sobra = set(mensaje) - _MENSAJE
        if sobra:
            raise EnvelopeInvalido(f"{sub}: claves no permitidas {sorted(sobra)}")
        if mensaje.get("role") not in _ROLES:
            raise EnvelopeInvalido(f"{sub}.role: debe ser 'user' o 'assistant'")
        contenido = _texto_de_contenido(mensaje.get("content"), f"{sub}.content",
                                        textos, hallazgos)
        salida_mensajes.append({"role": mensaje["role"], "content": contenido})

    salida_params: Dict[str, Any] = {
        "model": modelo,
        "messages": salida_mensajes,
        "max_tokens": params.get("max_tokens", max_tokens_defecto),
    }
    if not isinstance(salida_params["max_tokens"], int) or salida_params["max_tokens"] < 1:
        raise EnvelopeInvalido(f"{ruta}.params.max_tokens: entero positivo")

    if "system" in params:
        salida_params["system"] = _texto_de_contenido(
            params["system"], f"{ruta}.params.system", textos, hallazgos)

    for opcional in ("temperature", "top_p", "top_k", "stop_sequences"):
        if opcional in params:
            salida_params[opcional] = params[opcional]

    return ({"custom_id": custom_id, "params": salida_params}, textos, hallazgos)


def leer_raiz(documento: Any) -> Tuple[List[Any], Dict[str, Any], List[Hallazgo]]:
    """Valida la raiz y devuelve (requests, metadata_saneada, hallazgos)."""
    if not isinstance(documento, dict):
        raise EnvelopeInvalido("la raiz debe ser un objeto JSON")
    sobra = set(documento) - _RAIZ
    if sobra:
        raise EnvelopeInvalido(f"raiz: claves no permitidas {sorted(sobra)}")

    peticiones = documento.get("requests")
    if not isinstance(peticiones, list) or not peticiones:
        raise EnvelopeInvalido("'requests' debe ser una lista no vacia")

    hallazgos: List[Hallazgo] = []
    metadata = _revisar_claves_anidadas(documento.get("metadata", {}), "metadata", hallazgos)
    return peticiones, metadata, hallazgos
