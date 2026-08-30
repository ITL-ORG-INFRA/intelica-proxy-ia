"""Capa 1 sobre la superficie que se abrio para el trabajo real.

El envelope acepta ahora dos cosas que antes rechazaba: `output_config` (la
salida estructurada) y `cache_control` (lo que hace que un system repetido se
cobre una vez). Ensanchar una lista blanca es exactamente el momento en que se
abren agujeros, asi que esto fija lo que NO debe pasar por los huecos nuevos.

El caso que da sentido al fichero es el esquema de salida: no es dato inerte,
es una instruccion. Un esquema con una propiedad 'cvv' le esta pidiendo al
modelo que extraiga el CVV del documento. Bloquearlo no es celo, es el objetivo.

Los numeros de tarjeta son de prueba, publicados por las marcas.
Ver docs/SOBRE-LOS-PANES-DE-PRUEBA.md.

    .venv/bin/python tests/envelope_test.py
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src", "sanitizer"))

from detectors import scan_text            # noqa: E402
from envelope import InvalidEnvelope, normalize_request  # noqa: E402

FAILURES = []


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


def request(params_extra=None, text="Extrae los eventos de facturacion."):
    params = {"model": "claude-sonnet-4-5", "max_tokens": 1000,
              "messages": [{"role": "user", "content": text}]}
    params.update(params_extra or {})
    return {"custom_id": "ARG-2AB1006T-16b4a3b9", "params": params}


def run_layers(pet):
    """Devuelve (salida, hallazgos) tras las capas 1 a 5. Propaga EnvelopeInvalido."""
    out, texts, findings = normalize_request(pet, 0, set(), 4096)
    for path, text in texts:
        findings.extend(scan_text(text, path))
    return out, findings


def rejects_envelope(pet):
    try:
        run_layers(pet)
        return False
    except InvalidEnvelope:
        return True


def schema_of(propiedades):
    return {"output_config": {"format": {
        "type": "json_schema",
        "schema": {"type": "object", "properties": propiedades},
    }}}


# ---------------------------------------------------------------------------
print("\n[1] la forma real del trabajo cruza")

out, findings = run_layers(request({
    "system": [{"type": "text", "text": "Eres un extractor de guias de tarifas.",
                "cache_control": {"type": "ephemeral"}}],
    **schema_of({"fee_code": {"type": "string"},
               "amount": {"type": "string", "description": "el valor tal cual"}}),
}))
ck("una peticion con output_config y cache_control pasa", not findings,
   [h.as_dict() for h in findings])
ck("el cache_control llega a la salida",
   out["params"]["system"][0].get("cache_control") == {"type": "ephemeral"},
   "sin el, un system de 4 KB se cobra en cada peticion en vez de una vez")
ck("el output_config llega a la salida",
   "fee_code" in out["params"]["output_config"]["format"]["schema"]["properties"])
ck("ttl explicito admitido",
   run_layers(request({"system": [{"type": "text", "text": "x",
                               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}))[1] == [])

# ---------------------------------------------------------------------------
print("\n[2] el esquema de salida no puede pedir datos prohibidos")

for field in ("cvv", "card_number", "pan", "cvc2", "track2"):
    _, findings = run_layers(request(schema_of({field: {"type": "string"}})))
    ck(f"un esquema que pide '{field}' rechaza la peticion",
       any(h.layer == 2 for h in findings),
       "el esquema es una instruccion al modelo, no metadata inerte")

_, findings = run_layers(request(schema_of({"fee_code": {"type": "string"}})))
ck("un esquema legitimo no se rechaza", not findings,
   [h.as_dict() for h in findings])

# ---------------------------------------------------------------------------
print("\n[3] el esquema entero se escanea: viaja a Anthropic")

PAN = "4111111111111111"

cases = [
    ("en una description",
     schema_of({"nota": {"type": "string", "description": f"por ejemplo {PAN}"}})),
    # 'region' y no 'tarjeta': ese nombre ya lo tumbaria la capa 2 y el enum
    # ni se llegaria a mirar, con lo que la prueba no probaria nada.
    ("en un enum",
     schema_of({"region": {"type": "string", "enum": ["europe", PAN]}})),
    ("en el nombre de una propiedad",
     schema_of({f"campo_{PAN}": {"type": "string"}})),
    ("en format.name",
     {"output_config": {"format": {"type": "json_schema", "name": f"n{PAN}",
                                   "schema": {"type": "object"}}}}),
    ("en format.description",
     {"output_config": {"format": {"type": "json_schema",
                                   "description": f"para {PAN}",
                                   "schema": {"type": "object"}}}}),
]
for name, extra in cases:
    _, findings = run_layers(request(extra))
    ck(f"un PAN {name} se detecta",
       any(h.layer == 3 for h in findings))

_, findings = run_layers(request(schema_of(
    {"nota": {"type": "string", "description": "banda: ;4111111111111111=25121010000000000000?"}})))
ck("SAD en el esquema es bloqueo duro",
   any(h.hard for h in findings),
   "los datos de autenticacion tumban el lote entero, esten donde esten")

# ---------------------------------------------------------------------------
print("\n[4] los huecos nuevos siguen siendo deny-by-default")

ck("cache_control con clave desconocida se rechaza",
   rejects_envelope(request({"system": [{"type": "text", "text": "x",
       "cache_control": {"type": "ephemeral", "cvv": "123"}}]})))
ck("cache_control con tipo desconocido se rechaza",
   rejects_envelope(request({"system": [{"type": "text", "text": "x",
       "cache_control": {"type": "persistent"}}]})))
ck("cache_control con ttl invalido se rechaza",
   rejects_envelope(request({"system": [{"type": "text", "text": "x",
       "cache_control": {"type": "ephemeral", "ttl": "99y"}}]})))
ck("output_config con clave desconocida se rechaza",
   rejects_envelope(request({"output_config": {"format": {"type": "text"},
                                                "extra": 1}})))
ck("format con clave desconocida se rechaza",
   rejects_envelope(request({"output_config": {"format": {"type": "text",
                                                           "callback": "http://x"}}})))
ck("un tipo de formato inventado se rechaza",
   rejects_envelope(request({"output_config": {"format": {"type": "xml"}}})))
ck("json_schema sin schema se rechaza",
   rejects_envelope(request({"output_config": {"format": {"type": "json_schema"}}})))
ck("un bloque de contenido con clave desconocida se rechaza",
   rejects_envelope(request({"messages": [{"role": "user", "content": [
       {"type": "text", "text": "x", "source": {"data": "..."}}]}]})))

# ---------------------------------------------------------------------------
print("\n[5] el esquema no es un sitio donde agotar la Lambda")

hondo = {"type": "object"}
cursor = hondo
for _ in range(40):
    cursor["items"] = {"type": "object"}
    cursor = cursor["items"]
ck("un esquema demasiado anidado se rechaza",
   rejects_envelope(request({"output_config": {"format": {
       "type": "json_schema", "schema": hondo}}})))

ancho = {"type": "object", "properties": {f"c{i}": {"type": "string"}
                                          for i in range(3000)}}
ck("un esquema con demasiados nodos se rechaza",
   rejects_envelope(request({"output_config": {"format": {
       "type": "json_schema", "schema": ancho}}})))

print()
if FAILURES:
    print(f"FALLAN {len(FAILURES)}: " + ", ".join(FAILURES))
    sys.exit(1)
print("TODO OK")
