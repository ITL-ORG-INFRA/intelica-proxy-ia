"""Logging en JSON con una regla dura: por aqui no pasa payload.

"sin payload en logs" no es una buena intencion, es un control PCI. Confiar en
que nadie escriba logger.info(mensaje_del_usuario) no es un control; que el
logger sea incapaz de emitirlo, si.

Por eso el contexto solo admite escalares, las cadenas se truncan y cualquier
tirada larga de digitos se sustituye antes de salir. Un PAN no llega a
CloudWatch ni por descuido.
"""
import json
import logging
import os
import re
import sys
from typing import Any

#: mas largo que esto no es un identificador, es contenido
MAX_VALUE_CHARS = 256

#: 12 digitos ya no es un contador ni un timestamp; el PAN mas corto tiene 13
_DIGIT_RUN = re.compile(r"\d[\d \-]{10,}\d")

#: nombres que nunca deben aparecer como contexto, aunque el valor sea inocuo
_FORBIDDEN = {
    "payload", "body", "content", "message_content", "messages", "text",
    "prompt", "system", "pan", "cc", "cvv", "track", "track1", "track2",
    "expiry", "pin", "raw",
}


def scrub(value: Any) -> Any:
    """Deja pasar solo lo que no puede ser CHD."""
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if value is None:
        return None
    text = str(value)
    text = _DIGIT_RUN.sub("[DIGITS]", text)
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS] + "…[truncado]"
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if not key.startswith("ctx_"):
                continue
            field = key[4:]
            payload[field] = "[PROHIBIDO]" if field in _FORBIDDEN else scrub(value)
        if record.exc_info:
            # La traza tambien se limpia: una excepcion puede traer el dato
            # que la provoco dentro del mensaje.
            payload["error"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    return logger
