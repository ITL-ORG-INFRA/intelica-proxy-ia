# Ejemplo completo: subir un lote de varias partes

Paso a paso para mandar trabajo al proxy cuando el lote se parte en varios
ficheros. Es el flujo que usa el equipo de Cuotas con las guías de tarifas de
Mastercard.

> **Estado:** el disparo por `_MANIFEST.json` necesita un cambio en el repo de
> Terraform que **todavía no está aplicado** (ver
> [PROMPT-TERRAFORM-MANIFIESTO.md](PROMPT-TERRAFORM-MANIFIESTO.md)). Hasta
> entonces, cada `.json` se procesa por separado y subir el manifiesto no
> dispara el envío. El resto de esta guía ya es válido.

---

## El mapa

```
raw/input/lote-2026-08-27/
├── parte-01.json   ← el prompt va aquí, en cada request
├── parte-02.json   ← y aquí
├── parte-03.json   ← y aquí
├── parte-04.json   ← y aquí
├── parte-05.json   ← y aquí
├── parte-06.json   ← y aquí
└── _MANIFEST.json  ← sólo la lista de ficheros. SE SUBE AL FINAL
```

**El prompt va en los seis ficheros de datos, nunca en el manifiesto.** El
manifiesto es una señal de "ya está todo", no un contenedor.

---

## Paso 1 — Preparar cada parte

Cada `parte-NN.json` es un objeto con un array `requests`. Cada elemento del
array es una petición completa y autosuficiente:

```json
{
  "requests": [
    {
      "custom_id": "ARG-2AB1006T-16b4a3b9",
      "params": {
        "model": "claude-sonnet-4-5",
        "max_tokens": 2000,
        "system": [
          {
            "type": "text",
            "text": "You are an information extraction system for Mastercard pricing guides.\n\nYou will receive ONE text chunk from a PDF. Extract only billing events that ...",
            "cache_control": { "type": "ephemeral" }
          }
        ],
        "output_config": {
          "format": {
            "type": "json_schema",
            "schema": { "type": "object", "properties": { "...": {} }, "required": [], "additionalProperties": false }
          }
        },
        "messages": [
          { "role": "user", "content": "--- PAGE 27 ---\n2AB1006T    Authorization Acquirer Fee Domestic—Max\nAA\nMastercard bills the acquirer weekly for ..." }
        ]
      }
    },
    {
      "custom_id": "ARG-2AB1007T-8c21f0e4",
      "params": { "...": "el MISMO system, otro content" }
    }
  ]
}
```

### Dónde va cada cosa

| Campo | Qué lleva | ¿Cambia entre peticiones? |
|---|---|---|
| `params.system[].text` | **El prompt.** Las instrucciones al modelo | **No.** Idéntico en todas |
| `params.messages[].content` | El dato: el trozo de PDF | **Sí.** Uno por chunk |
| `custom_id` | Tu identificador para cruzar el resultado | Sí, y **único en todo el lote** |
| `params.output_config` | El esquema JSON que fuerza la salida | No |
| `params.max_tokens` | Tope de la respuesta | No |

### El prompt se repite, y está bien

Sí, las 3.785 bytes del `system` van en cada una de las ~9.400 peticiones. No es
un desperdicio: es cómo funciona la Batch API. Cada petición es autosuficiente
porque Anthropic las procesa por separado, posiblemente en máquinas distintas y
en cualquier orden.

Lo que evita pagarlo 9.400 veces es `cache_control: {"type": "ephemeral"}`:
Anthropic reconoce el bloque repetido y lo cobra una vez.

> **Byte a byte idéntico.** Un espacio de diferencia entre `parte-03.json` y las
> demás rompe la caché de esas peticiones y se paga el prompt completo por cada
> una. Con el `system` siendo el 67 % del payload, la diferencia no es menor.
> Si tu generador lo construye desde una única constante, ya estás cubierto.
>
> Y si cambias el prompt, hazlo **entre lotes**, nunca a mitad de uno.

### Reglas del envelope

El proxy es estricto a propósito: la lista es de lo que **se permite**, y
cualquier otra cosa rechaza la petición.

| Permitido en `params` | |
|---|---|
| `model` | de la lista blanca: `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5-20251001` |
| `max_tokens` | entero positivo |
| `system` | texto o lista de bloques `{type, text, cache_control}` |
| `messages` | lista de `{role, content}`, con `role` = `user` o `assistant` |
| `output_config` | `format.type` = `json_schema` o `text`; el esquema, libre |
| `temperature`, `top_p`, `top_k`, `stop_sequences` | opcionales |

Y el `custom_id`: **sólo alfanumérico, guion y guion bajo**, máximo 64
caracteres. Un punto lo rechaza. Tiene que ser **opaco**: viaja a Anthropic tal
cual, así que no metas un DNI ni un número de cuenta ahí.

> El `custom_id` **también se escanea**. Un PAN es alfanumérico, así que sería un
> identificador válido — y cruzaría la frontera sin que ninguna capa lo mirase si
> no se revisara. Se revisa.

> Y el **esquema de salida** también, con una vuelta de tuerca: un esquema no es
> dato, es una instrucción. Si declara una propiedad `cvv`, le está pidiendo al
> modelo que extraiga el CVV del documento. Por eso ahí un nombre de campo
> prohibido **rechaza la petición** en vez de borrar el campo en silencio: borrarlo
> cambiaría el contrato que crees tener sin decírtelo. Las descripciones, los
> `enum` y los nombres de propiedad pasan por las capas 3-5 como cualquier texto.

---

> **No lo escribas a mano.** `./scripts/subir-lote.sh dev ./mi-carpeta` genera el
> manifiesto desde lo que realmente hay en la carpeta, valida las partes, avisa
> de `custom_id` repetidos entre ellas, y sube en el orden correcto. Con
> `--solo-manifiesto` te lo enseña sin subir nada.
>
> Los dos pasos siguientes explican qué construye, para que puedas replicarlo en
> tu propio generador.

## Paso 2 — Preparar el manifiesto

```json
{
  "lote": "lote-2026-08-27",
  "files": [
    "parte-01.json",
    "parte-02.json",
    "parte-03.json",
    "parte-04.json",
    "parte-05.json",
    "parte-06.json"
  ],
  "total_requests": 9412
}
```

Tres campos y nada más:

| Campo | Para qué |
|---|---|
| `lote` | Nombre legible del lote |
| `files` | **Los nombres de las partes**, sin ruta. Es lo que el submitter usa para saber cuántas esperar |
| `total_requests` | Informativo, para cuadrar cuentas |

**Sin prompt, sin datos, sin contenido.** Y no es por simplicidad: el manifiesto
lo lee el **submitter**, que está en la zona limpia y no puede leer datos de
tarjeta. Si el manifiesto llevara contenido habría que darle acceso a datos sin
sanitizar, y la frontera del sistema se caería. Que sea sólo una lista de
nombres es lo que permite que lo lea sin romper el modelo.

---

## Paso 3 — Subir las partes

**Primero las seis, en cualquier orden.** Cada una dispara su propio sanitizer y
se procesan en paralelo.

```bash
LOTE=lote-2026-08-27
RAW=s3://intelica-proxy-ia-dev-raw-891376942769/input/$LOTE
```

```bash
aws s3 cp parte-01.json $RAW/parte-01.json --region eu-south-2
```

O las seis de golpe, sin arrastrar el manifiesto:

```bash
aws s3 cp . $RAW/ --recursive --exclude "*" --include "parte-*.json" --region eu-south-2
```

---

## Paso 4 — Subir el manifiesto, al final

```bash
aws s3 cp _MANIFEST.json $RAW/_MANIFEST.json --region eu-south-2
```

**Este es el que dispara el envío.** Si lo subes antes de que el sanitizer acabe
con alguna parte no se pierde nada: el lote queda en `esperando_partes` y el
barrido lo recoge cuando terminen.

> No lo metas en el mismo `aws s3 cp --recursive` que las partes. El orden
> importa poco para la corrección, pero subirlo al final es lo que hace que el
> caso normal sea envío inmediato en vez de esperar al siguiente barrido.

---

## Paso 5 — Ver qué pasó

### El parte de estado de cada parte

A los pocos segundos de subir cada `.json`:

```bash
aws s3 ls s3://intelica-proxy-ia-dev-clean-891376942769/status/ --region eu-south-2
```

```bash
aws s3 cp s3://intelica-proxy-ia-dev-clean-891376942769/status/<batch_id>.json - --region eu-south-2
```

```json
{
  "status": "clean",
  "request_counts": { "received": 1570, "clean": 1570, "rejected": 0 }
}
```

Si algo se bloqueó, el parte dice **qué petición**, **qué capa** la paró y **qué
hacer**, sin incluir nunca el valor que la disparó:

```json
{
  "status": "quarantined",
  "reason": "gate: 3/1570 rechazadas (0.19%) supera el umbral (1.0% o 100 absolutas)",
  "rejections": [
    { "index": 41,
      "findings": [{ "layer": 3, "type": "pan",
                      "where": "requests[41].params.messages[0].content",
                      "detail": "visa/16d/contiguo" }] }
  ],
  "what_to_do": ["Se detecto un numero de tarjeta en texto libre. Quitalo del origen: el proxy no lo enmascara, lo bloquea."]
}
```

### El estado del lote completo

```bash
aws dynamodb get-item --table-name intelica-proxy-ia-dev-batches \
  --key '{"batch_id":{"S":"batch#input/lote-2026-08-27"}}' \
  --region eu-south-2
```

| `status` | Qué significa |
|---|---|
| `esperando_partes` | El sanitizer sigue trabajando. El barrido lo recogerá |
| `enviado` | Ya está en Anthropic. Mira `batch_ids` |
| `cuarentena` | **Alguna parte fue rechazada, así que no se envió ninguna** |
| `fallido` | Manifiesto ilegible, `custom_id` duplicado entre partes, o falta una salida limpia |

---

## Paso 6 — Recoger los resultados

Anthropic procesa en menos de 24 h (la mayoría en menos de una).

```bash
aws s3 cp s3://intelica-proxy-ia-dev-results-891376942769/results/<batch_id>.jsonl . --region eu-south-2
```

Un JSONL: una línea por petición.

> **Cruza por `custom_id`, nunca por posición.** Anthropic devuelve los
> resultados en **orden arbitrario**, y el proxy además descarta cualquier
> resultado que no pase el segundo pase de sanitización — así que el fichero
> puede tener menos líneas que peticiones enviaste.
>
> Emparejar por número de línea no da error: da datos correctos asignados a la
> fila equivocada, y eso no se detecta hasta que alguien lo mira.

```python
import json

respuestas = {}
with open("resultados.jsonl", encoding="utf-8") as f:
    for linea in f:
        entrada = json.loads(linea)
        respuestas[entrada["custom_id"]] = entrada["result"]

for fila in mis_chunks:
    resultado = respuestas.get(fila.custom_id)
    if resultado is None:
        continue                      # descartada: mira el parte de estado
    if resultado["type"] != "succeeded":
        continue                      # errored | canceled | expired
    datos = resultado["message"]["content"]
```

---

## Reglas que decide el proxy, no tú

| Situación | Qué pasa |
|---|---|
| Un PAN en texto libre | Se rechaza esa petición |
| Banda magnética, CVV o PIN | **Se aborta el lote entero**, sin mirar el resto |
| Campo llamado `pan`, `cvv`, `track`… | Se destruye el valor |
| Imágenes, PDFs o base64 binario | Se rechaza esa petición |
| Clave fuera del esquema | Se rechaza esa petición |
| Más del 1 % de peticiones rechazadas | **No pasa ninguna** |
| Una parte del lote en cuarentena | **No se envía ninguna parte** |

Las dos últimas son deliberadas y conviene entenderlas: muchos rechazos no son
errores dispersos, suelen indicar que la fuente de datos tiene un problema. Y un
lote es una unidad — mandar "sólo las buenas" normalizaría que entren datos que
no deben entrar.

> El proxy **normaliza el texto antes de mirarlo**: ancho completo, espacios
> invisibles, guiones Unicode. Un número escrito de forma rara se detecta igual.
> No busques la forma de que pase — el objetivo es que el dato no salga de tu
> origen.

---

## Probarlo antes de subir nada

Sin tocar AWS, con el mismo código que corre en la Lambda:

```bash
.venv/bin/python scripts/probar_filtro.py mi-lote.json --detalle
```

Segundos por iteración, y te dice exactamente qué capa habría disparado.

Contra el entorno real, con las suites de ejemplo:

```bash
./scripts/probar-flujo.sh dev ejemplos/01-limpios
```

---

## Si algo no funciona

**`AccessDenied` al subir y los permisos de S3 parecen bien.** Falta
`kms:GenerateDataKey` sobre la CMK del bucket raw. El error no menciona KMS por
ningún lado y se pierde media tarde ahí.

**Subí una parte y no aparece parte de estado.** Comprueba que va bajo
`input/<lote>/` y que el nombre no lleva caracteres raros. Luego:

```bash
aws logs tail /aws/lambda/intelica-proxy-ia-dev-sanitizer --since 10m --region eu-south-2
```

**El lote se queda en `esperando_partes`.** Falta el submitter: o los
disparadores de Terraform no están aplicados, o la regla de horario está
desactivada. Para moverlo a mano:

```bash
./scripts/ciclo-manual.sh dev
```

**El lote dice `fallido` con `custom_id duplicado`.** Dos partes traen el mismo
identificador. Anthropic rechazaría el POST entero sin decir cuál, así que el
proxy lo detecta antes y lo nombra en el estado del lote.
