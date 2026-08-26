"""Lectura cacheada de la API key de Anthropic.

Secrets Manager se cobra por llamada y anade latencia: el valor se guarda en
memoria del contenedor, asi solo se paga en el arranque en frio.
"""
import json
from typing import Optional

import boto3

from config import ANTHROPIC_SECRET_ARN

_client = boto3.client("secretsmanager")
_cached_key: Optional[str] = None


def anthropic_api_key() -> str:
    global _cached_key
    if _cached_key is None:
        raw = _client.get_secret_value(SecretId=ANTHROPIC_SECRET_ARN)["SecretString"]
        try:
            _cached_key = json.loads(raw)["api_key"]
        except (json.JSONDecodeError, KeyError, TypeError):
            # Tolera que alguien haya guardado la key como texto plano.
            _cached_key = raw.strip()
        if not _cached_key:
            raise RuntimeError("el secreto de Anthropic esta vacio")
    return _cached_key
