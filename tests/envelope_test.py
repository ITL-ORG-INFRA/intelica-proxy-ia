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

from detectors import escanear_texto            # noqa: E402
from envelope import EnvelopeInvalido, normalizar_peticion  # noqa: E402

FALLOS = []


def ck(nombre, condicion, detalle=""):
    print(("  OK   " if condicion else "  FALLA ") + nombre
          + ("" if condicion else f"  <- {detalle}"))
    if not condicion:
        FALLOS.append(nombre)


def peticion(params_extra=None, texto="Extrae los eventos de facturacion."):
    params = {"model": "claude-sonnet-4-5", "max_tokens": 1000,
              "messages": [{"role": "user", "content": texto}]}
    params.update(params_extra or {})
    return {"custom_id": "ARG-2AB1006T-16b4a3b9", "params": params}


def pasar(pet):
    """Devuelve (salida, hallazgos) tras las capas 1 a 5. Propaga EnvelopeInvalido."""
    salida, textos, hallazgos = normalizar_peticion(pet, 0, set(), 4096)
    for ruta, texto in textos:
        hallazgos.extend(escanear_texto(texto, ruta))
    return salida, hallazgos


def rechaza_envelope(pet):
    try:
        pasar(pet)
        return False
    except EnvelopeInvalido:
        return True


def esquema(propiedades):
    return {"output_config": {"format": {
        "type": "json_schema",
        "schema": {"type": "object", "properties": propiedades},
    }}}


# ---------------------------------------------------------------------------
print("\n[1] la forma real del trabajo cruza")

salida, hallazgos = pasar(peticion({
    "system": [{"type": "text", "text": "Eres un extractor de guias de tarifas.",
                "cache_control": {"type": "ephemeral"}}],
    **esquema({"fee_code": {"type": "string"},
               "amount": {"type": "string", "description": "el valor tal cual"}}),
}))
ck("una peticion con output_config y cache_control pasa", not hallazgos,
   [h.como_dict() for h in hallazgos])
ck("el cache_control llega a la salida",
   salida["params"]["system"][0].get("cache_control") == {"type": "ephemeral"},
   "sin el, un system de 4 KB se cobra en cada peticion en vez de una vez")
ck("el output_config llega a la salida",
   "fee_code" in salida["params"]["output_config"]["format"]["schema"]["properties"])
ck("ttl explicito admitido",
   pasar(peticion({"system": [{"type": "text", "text": "x",
                               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}))[1] == [])

# ---------------------------------------------------------------------------
print("\n[2] el esquema de salida no puede pedir datos prohibidos")

for campo in ("cvv", "card_number", "pan", "cvc2", "track2"):
    _, hallazgos = pasar(peticion(esquema({campo: {"type": "string"}})))
    ck(f"un esquema que pide '{campo}' rechaza la peticion",
       any(h.capa == 2 for h in hallazgos),
       "el esquema es una instruccion al modelo, no metadata inerte")

_, hallazgos = pasar(peticion(esquema({"fee_code": {"type": "string"}})))
ck("un esquema legitimo no se rechaza", not hallazgos,
   [h.como_dict() for h in hallazgos])

# ---------------------------------------------------------------------------
print("\n[3] el esquema entero se escanea: viaja a Anthropic")

PAN = "4111111111111111"

casos = [
    ("en una description",
     esquema({"nota": {"type": "string", "description": f"por ejemplo {PAN}"}})),
    # 'region' y no 'tarjeta': ese nombre ya lo tumbaria la capa 2 y el enum
    # ni se llegaria a mirar, con lo que la prueba no probaria nada.
    ("en un enum",
     esquema({"region": {"type": "string", "enum": ["europe", PAN]}})),
    ("en el nombre de una propiedad",
     esquema({f"campo_{PAN}": {"type": "string"}})),
    ("en format.name",
     {"output_config": {"format": {"type": "json_schema", "name": f"n{PAN}",
                                   "schema": {"type": "object"}}}}),
    ("en format.description",
     {"output_config": {"format": {"type": "json_schema",
                                   "description": f"para {PAN}",
                                   "schema": {"type": "object"}}}}),
]
for nombre, extra in casos:
    _, hallazgos = pasar(peticion(extra))
    ck(f"un PAN {nombre} se detecta",
       any(h.capa == 3 for h in hallazgos))

_, hallazgos = pasar(peticion(esquema(
    {"nota": {"type": "string", "description": "banda: ;4111111111111111=25121010000000000000?"}})))
ck("SAD en el esquema es bloqueo duro",
   any(h.duro for h in hallazgos),
   "los datos de autenticacion tumban el lote entero, esten donde esten")

# ---------------------------------------------------------------------------
print("\n[4] los huecos nuevos siguen siendo deny-by-default")

ck("cache_control con clave desconocida se rechaza",
   rechaza_envelope(peticion({"system": [{"type": "text", "text": "x",
       "cache_control": {"type": "ephemeral", "cvv": "123"}}]})))
ck("cache_control con tipo desconocido se rechaza",
   rechaza_envelope(peticion({"system": [{"type": "text", "text": "x",
       "cache_control": {"type": "persistent"}}]})))
ck("cache_control con ttl invalido se rechaza",
   rechaza_envelope(peticion({"system": [{"type": "text", "text": "x",
       "cache_control": {"type": "ephemeral", "ttl": "99y"}}]})))
ck("output_config con clave desconocida se rechaza",
   rechaza_envelope(peticion({"output_config": {"format": {"type": "text"},
                                                "extra": 1}})))
ck("format con clave desconocida se rechaza",
   rechaza_envelope(peticion({"output_config": {"format": {"type": "text",
                                                           "callback": "http://x"}}})))
ck("un tipo de formato inventado se rechaza",
   rechaza_envelope(peticion({"output_config": {"format": {"type": "xml"}}})))
ck("json_schema sin schema se rechaza",
   rechaza_envelope(peticion({"output_config": {"format": {"type": "json_schema"}}})))
ck("un bloque de contenido con clave desconocida se rechaza",
   rechaza_envelope(peticion({"messages": [{"role": "user", "content": [
       {"type": "text", "text": "x", "source": {"data": "..."}}]}]})))

# ---------------------------------------------------------------------------
print("\n[5] el esquema no es un sitio donde agotar la Lambda")

hondo = {"type": "object"}
cursor = hondo
for _ in range(40):
    cursor["items"] = {"type": "object"}
    cursor = cursor["items"]
ck("un esquema demasiado anidado se rechaza",
   rechaza_envelope(peticion({"output_config": {"format": {
       "type": "json_schema", "schema": hondo}}})))

ancho = {"type": "object", "properties": {f"c{i}": {"type": "string"}
                                          for i in range(3000)}}
ck("un esquema con demasiados nodos se rechaza",
   rechaza_envelope(peticion({"output_config": {"format": {
       "type": "json_schema", "schema": ancho}}})))

print()
if FALLOS:
    print(f"FALLAN {len(FALLOS)}: " + ", ".join(FALLOS))
    sys.exit(1)
print("TODO OK")
