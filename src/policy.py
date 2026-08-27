"""Política del sanitizador: datos, no lógica.

Todo lo configurable vive aquí para que el upgrade a SSM/AppConfig sea cambiar
de dónde se carga este objeto, sin tocar detect.py ni sanitize.py.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace

# Rutas permitidas dentro de UNA línea JSONL (un objeto request).
# Deny-by-default: cualquier clave que no esté aquí es un hallazgo SCHEMA.
ALLOWED_PATHS: frozenset[str] = frozenset({
    "$.custom_id",
    "$.params",
    "$.params.model",
    "$.params.max_tokens",
    "$.params.system",
    "$.params.system[]",
    "$.params.system[].type",
    "$.params.system[].text",
    "$.params.system[].cache_control",
    "$.params.system[].cache_control.type",
    "$.params.messages",
    "$.params.messages[]",
    "$.params.messages[].role",
    "$.params.messages[].content",
    "$.params.output_config",
    "$.params.output_config.format",
    "$.params.output_config.format.type",
    "$.params.output_config.format.schema",
})

# Subárboles cuya ESTRUCTURA no se puede enumerar (un JSON Schema es libre).
# Dentro de ellos no se aplica la allowlist, pero sus cadenas SÍ se escanean.
EXEMPT_SUBTREES: frozenset[str] = frozenset({
    "$.params.output_config.format.schema",
})

# Nombres de campo que destruyen el valor sin mirar su formato.
SENSITIVE_FIELDS: frozenset[str] = frozenset({
    "pan", "card", "cardnumber", "card_number", "cardno", "card_no",
    "cc", "ccnum", "ccnumber", "creditcard", "credit_card",
    "primary_account_number", "account_number", "acct_number",
    "cvv", "cvv2", "cvc", "cvc2", "cid", "csc", "security_code",
    "track", "track1", "track2", "track_data", "magstripe",
    "expiry", "exp_date", "expiration", "expiration_date",
    "exp_month", "exp_year",
    "pin", "pin_block", "pinblock",
    "cardholder", "cardholder_name", "holder_name", "titular",
})


@dataclass(frozen=True)
class Policy:
    allowed_paths: frozenset[str] = ALLOWED_PATHS
    exempt_subtrees: frozenset[str] = EXEMPT_SUBTREES
    sensitive_fields: frozenset[str] = SENSITIVE_FIELDS

    # MVP: cualquier hallazgo aborta el lote. Es semántica de tripwire:
    # en este corpus el número esperado de hallazgos es cero.
    max_findings: int = 0

    # MVP: solo bloqueo. Sin redacción -> sin surrogate, sin HMAC, sin KMS.
    redact: bool = False

    # content debe ser str. Bloques image/document -> BLOQUEADO (hoy no aplica,
    # pero cuesta tres líneas y protege si alguien mete PDFs más adelante).
    allow_content_blocks: bool = False

    # Capa 0 extendida: decodificar base64/urlencode un nivel. Upgrade.
    decode_nested: bool = False

    def relaxed(self, **kw) -> "Policy":
        """Para tests: Policy().relaxed(max_findings=5)."""
        return replace(self, **kw)


DEFAULT = Policy()
