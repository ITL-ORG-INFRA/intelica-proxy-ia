"""Configuracion por variables de entorno.

Cada Lambda necesita un subconjunto distinto: por eso nada es obligatorio al
importar. Cada handler declara lo suyo con require() en su arranque, para que
un despliegue mal parametrizado falle en frio y no a mitad de un lote.
"""
import os
from typing import Optional


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def require(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        raise RuntimeError("faltan variables de entorno: " + ", ".join(missing))


# --- nombres retirados -----------------------------------------------------
#: nombre viejo -> nombre actual. Se comprueban al importar, no bajo demanda.
#:
#: Esto no es cosmetica. Si Terraform se queda con el nombre viejo, el codigo
#: nuevo no lo lee y CAE EN EL VALOR POR DEFECTO sin decir nada: se configuro
#: SUBMIT_MAX_POR_TICK = 20 y el submitter envia 2 por tick para siempre. No
#: hay error, no hay log, no hay alarma — solo un sistema que va veinte veces
#: mas lento de lo que alguien cree haber configurado.
#:
#: Fallar en frio es la unica forma de que eso se vea. Un arranque roto se
#: diagnostica en un minuto; un valor por defecto silencioso, en una tarde.
_RETIRED = {
    "SUBMIT_MAX_POR_TICK": "SUBMIT_MAX_PER_TICK",
    "FETCH_MAX_POR_TICK": "FETCH_MAX_PER_TICK",
}


def reject_retired(environ=None) -> None:
    """Revienta si el entorno trae un nombre de variable ya retirado."""
    env = os.environ if environ is None else environ
    found = [(old, new) for old, new in sorted(_RETIRED.items())
             if env.get(old, "").strip()]
    if not found:
        return
    raise RuntimeError(
        "variables de entorno retiradas (renombralas en Terraform): "
        + ", ".join(f"{old} -> {new}" for old, new in found))


reject_retired()


ENVIRONMENT = _env("ENVIRONMENT", "dev")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
AWS_REGION = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION", "eu-south-2")

# --- almacenamiento --------------------------------------------------------
# raw y quarantine viven dentro del CDE y se cifran con CMK-raw.
# clean y results estan fuera y usan CMK-clean. Ningun rol toca las dos.
RAW_BUCKET = _env("RAW_BUCKET")
QUARANTINE_BUCKET = _env("QUARANTINE_BUCKET")
CLEAN_BUCKET = _env("CLEAN_BUCKET")
RESULTS_BUCKET = _env("RESULTS_BUCKET")
BATCHES_TABLE = _env("BATCHES_TABLE")

# --- anthropic -------------------------------------------------------------
ANTHROPIC_SECRET_ARN = _env("ANTHROPIC_SECRET_ARN")
ANTHROPIC_VERSION = _env("ANTHROPIC_VERSION", "2023-06-01")
ANTHROPIC_BASE_URL = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

_models = _env("ALLOWED_MODELS")
ALLOWED_MODELS = {m.strip() for m in _models.split(",") if m.strip()}

# --- gate de sanitizacion --------------------------------------------------
#: porcentaje de rechazos a partir del cual se aborta el lote entero.
#: Un lote con muchos rechazos no es un lote con errores sueltos: es un
#: productor que esta mandando CHD de forma sistematica.
GATE_REJECT_PCT = float(_env("GATE_REJECT_PCT", "1.0"))

#: rechazos absolutos que abortan el lote aunque el porcentaje no se alcance
GATE_REJECT_ABS = _int("GATE_REJECT_ABS", 100)

MAX_REQUESTS_PER_BATCH = _int("MAX_REQUESTS_PER_BATCH", 100_000)
MAX_RAW_BYTES = _int("MAX_RAW_BYTES", 200_000_000)
DEFAULT_MAX_TOKENS = _int("DEFAULT_MAX_TOKENS", 4096)

# --- admision (cola en vuelo) ----------------------------------------------
#: tope de peticiones encoladas en Anthropic. El start tier son 200.000.
#: Pasarse no da error: da expiraciones silenciosas a las 24 h.
INFLIGHT_LIMIT = _int("INFLIGHT_LIMIT", 200_000)

#: cuantos lotes se envian como maximo en un tick. Subir de golpe dispara 429
#: aunque el RPM medio este muy por debajo: la rampa tiene que ser gradual.
SUBMIT_MAX_PER_TICK = _int("SUBMIT_MAX_PER_TICK", 2)

#: cuantos lotes terminados se bajan por tick (cada uno puede ser enorme)
FETCH_MAX_PER_TICK = _int("FETCH_MAX_PER_TICK", 2)

# --- polling ---------------------------------------------------------------
BATCH_EXPIRY_HOURS = _int("BATCH_EXPIRY_HOURS", 24)
RESULTS_TTL_DAYS = _int("RESULTS_TTL_DAYS", 30)

#: prefijo de los lotes que planta el canary. Nunca deben cruzar a clean.
CANARY_PREFIX = _env("CANARY_PREFIX", "canary/")
