# Ejemplo completo: subir un lote de varias partes

Paso a paso para mandar trabajo al proxy cuando el lote se parte en varios
ficheros. Es el flujo que usa el equipo de Cuotas con las guías de tarifas de
Mastercard.

> **Estado:** este repositorio confirma el contrato, pero Terraform vive en
> otro repositorio. Antes de la primera prueba comprueba en AWS que la regla
> `itl-0003-proxy-ia-<entorno>-evb-manifest-03` está aplicada sobre el bucket
> **clean**, con prefijo `input/` y sufijo `_MANIFEST.json`. Sin esa regla las
> partes se sanitizan, pero subir el manifiesto no dispara el envío.

---

## El mapa

Las partes y el manifiesto van a **buckets distintos**, bajo la **misma**
carpeta `input/lote-2026-08-27/`:

```
raw/input/lote-2026-08-27/
├── parte-01.json   ← el prompt va aquí, en cada request
├── parte-02.json   ← y aquí
├── parte-03.json   ← y aquí
├── parte-04.json   ← y aquí
├── parte-05.json   ← y aquí
└── parte-06.json   ← y aquí

clean/input/lote-2026-08-27/
└── _MANIFEST.json  ← sólo la lista de ficheros. SE SUBE AL FINAL
```

El manifiesto va a `clean` porque no lleva datos: sólo nombres de fichero. Eso
permite que el submitter lo lea sin tener acceso a `raw` — y no tenerlo es
precisamente lo que hace que robar su credencial no saque una tarjeta.

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
| `model` | Tiene que estar en `ALLOWED_MODELS` del entorno; consulta la lista desplegada antes de generar el lote |
| `max_tokens` | entero positivo |
| `system` | texto o lista de bloques `{type, text, cache_control}` |
| `messages` | lista de `{role, content}`, con `role` = `user` o `assistant` |
| `output_config` | `format.type` = `json_schema` o `text`; el esquema, libre |
| `temperature`, `top_p`, `top_k`, `stop_sequences` | opcionales |

### Modelos actuales y lista blanca del proxy

Anthropic ya ofrece Claude Sonnet 5 (`claude-sonnet-5`) y Claude Opus 5
(`claude-opus-5`). Que un modelo exista en Anthropic **no significa que este
proxy lo admita automáticamente**: el sanitizer compara `params.model` con la
variable `ALLOWED_MODELS` desplegada por Terraform.

La configuración de Terraform documentada actualmente en este repositorio aún
propone esta lista:

```text
claude-opus-4-5,claude-sonnet-4-5,claude-haiku-4-5-20251001
```

Por tanto, mientras no se actualice y despliegue `ALLOWED_MODELS`, un lote con
`claude-sonnet-5` o `claude-opus-5` acaba en cuarentena con `modelo no
permitido`. Los ejemplos de esta guía conservan `claude-sonnet-4-5` porque es
la lista que el contrato de infraestructura todavía declara, no porque sea la
última versión disponible en Anthropic.

Antes de habilitar Claude 5 hay que probar además sus diferencias de contrato:

- Sonnet 5 y Opus 5 usan thinking adaptativo por defecto; `max_tokens` incluye
  tanto el thinking como la respuesta.
- Sonnet 5 rechaza `temperature`, `top_p` y `top_k` cuando se envían con valores
  no predeterminados. El envelope del proxy todavía permite esos campos de
  forma general.
- Hay que ejecutar los lotes de aceptación y confirmar Message Batches antes
  de ampliar la lista en `dev`, y después promover el mismo cambio a `qa`.

La lista efectiva es la configuración de la Lambda, no esta guía. Se puede
consultar de forma autenticada así:

```bash
aws lambda get-function-configuration \
  --function-name itl-0003-proxy-ia-dev-lambda-sanitizer-03 \
  --region eu-south-2 \
  | jq -r '.Environment.Variables.ALLOWED_MODELS'
```

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

> **Forma recomendada de enviar un lote.** Usa
> `./scripts/subir-lote.sh dev ./mi-carpeta`. El script valida todos los JSON,
> pasa el filtro local, comprueba los `custom_id`, genera el manifiesto desde
> los archivos que realmente va a subir, envía primero las partes a `raw` y
> finalmente el manifiesto a `clean`. Así se evita que la lista `files`, los
> destinos o el orden queden desincronizados por un comando manual.
>
> Ejecuta primero `./scripts/subir-lote.sh dev ./mi-carpeta --dry-run` para
> comprobar el lote y los destinos sin escribir en AWS. Los pasos manuales de
> las secciones siguientes explican el contrato y sirven para integrar otro
> productor, pero no son el camino habitual para una subida desde local.
>
> Los dos pasos siguientes explican qué construye, para que puedas replicarlo en
> tu propio generador.

## Paso 2 — Preparar el manifiesto

```json
{
  "batch": "lote-2026-08-27",
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
| `batch` | Nombre legible del lote |
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
RAW=s3://itl-0003-proxy-ia-dev-s3-raw-03/input/$LOTE
CLEAN=s3://itl-0003-proxy-ia-dev-s3-clean-03/input/$LOTE
```

```bash
aws s3 cp parte-01.json $RAW/parte-01.json --region eu-south-2
```

O las seis de golpe, sin arrastrar el manifiesto:

```bash
aws s3 cp . $RAW/ --recursive --exclude "*" --include "parte-*.json" --region eu-south-2
```

---

## Paso 4 — Subir el manifiesto, al final y a `clean`

```bash
aws s3 cp _MANIFEST.json $CLEAN/_MANIFEST.json --region eu-south-2
```

**Este es el que dispara el envío.** Si lo subes antes de que el sanitizer acabe
con alguna parte no se pierde nada: el lote queda en `awaiting_parts` y el
barrido lo recoge cuando terminen.

Ojo a las dos cosas que cambian respecto a las partes:

- **Va a `clean`, no a `raw`.** Es el único objeto que un productor escribe en
  `clean`, y sólo bajo `input/`.
- **El nombre tiene que ser exactamente `_MANIFEST.json`.** `MANIFEST.json` o
  `_MANIFEST.json.bak` no cierran nada: se quedan ahí sin disparar el envío.

> No lo metas en el mismo `aws s3 cp --recursive` que las partes — ni podrías,
> van a buckets distintos. Subirlo al final es lo que hace que el caso normal
> sea envío inmediato en vez de esperar al siguiente barrido.

---

## Paso 5 — Ver qué pasó

### El parte de estado de cada parte

A los pocos segundos de subir cada `.json`:

```bash
aws s3 ls s3://itl-0003-proxy-ia-dev-s3-clean-03/status/ --region eu-south-2
```

```bash
aws s3 cp s3://itl-0003-proxy-ia-dev-s3-clean-03/status/<batch_id>.json - --region eu-south-2
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
aws dynamodb get-item --table-name itl-0003-proxy-ia-dev-ddb-batches-03 \
  --key '{"batch_id":{"S":"batch#input/lote-2026-08-27"}}' \
  --region eu-south-2
```

| `status` | Qué significa |
|---|---|
| `awaiting_parts` | El sanitizer sigue trabajando. El barrido lo recogerá |
| `ready` | Las partes están todas limpias; el submitter lo va a enviar |
| `submitted` | Ya está en Anthropic. Mira `batch_ids` |
| `quarantined` | **Alguna parte fue rechazada, así que no se envió ninguna** |
| `failed` | Manifiesto ilegible, `custom_id` duplicado entre partes, o falta una salida limpia |

---

## Paso 6 — Recoger los resultados

Anthropic procesa en menos de 24 h (la mayoría en menos de una).

```bash
aws s3 cp s3://itl-0003-proxy-ia-dev-s3-results-03/results/<batch_id>.jsonl . --region eu-south-2
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

### Prueba autenticada desde local

Primero confirma qué identidad usará la CLI. Esta llamada es de sólo lectura y
falla de inmediato si el perfil o la sesión SSO han caducado:

```bash
aws sts get-caller-identity --region eu-south-2
```

Comprueba después la infraestructura que cierra el lote. La regla debe estar
`ENABLED`, tener un target y su `EventPattern` debe nombrar el bucket `clean`,
el prefijo `input/` y el sufijo `_MANIFEST.json`:

```bash
REGLA=itl-0003-proxy-ia-dev-evb-manifest-03
aws events describe-rule --name "$REGLA" --region eu-south-2 \
  | jq '{State, EventPattern}'
aws events list-targets-by-rule --rule "$REGLA" --region eu-south-2 \
  | jq '.Targets[] | {Id, Arn}'
aws s3api get-bucket-notification-configuration \
  --bucket itl-0003-proxy-ia-dev-s3-clean-03 --region eu-south-2 \
  | jq '.EventBridgeConfiguration'
```

Haz primero el ensayo local. Calcula y enseña los dos destinos, genera el
manifiesto y ejecuta el filtro real, pero no usa credenciales ni escribe en S3:

```bash
./scripts/subir-lote.sh dev ejemplos/lote-multiparte \
  prueba-local-01 --dry-run
```

Si termina limpio, ejecuta la subida autenticada. Usa un nombre nuevo en cada
intento para no mezclar objetos con una ejecución anterior:

```bash
./scripts/subir-lote.sh dev ejemplos/lote-multiparte \
  "prueba-local-$(date +%Y%m%d-%H%M%S)"
```

El script valida las credenciales con STS antes de escribir, sube las partes a
`raw/input/<lote>/` y sólo cuando todas terminaron sube el manifiesto a
`clean/input/<lote>/_MANIFEST.json`.

### Logs de cada ejecución

Cada invocación —también un ensayo o una que falla antes de subir— crea una
carpeta independiente:

```text
dist/logs/subir-lote/20260830T231500Z-12345/
├── ejecucion.log   # flujo completo, destinos, STS/AWS y código de salida
└── filtro.log      # motivo por JSON: request, capa, tipo y recomendación
```

La ruta exacta aparece al inicio y al final de la consola. Los logs no copian
el contenido de los JSON ni el valor sensible encontrado. Para un JSON mal
formado conservan el error detallado de `jq`; para un rechazo del sanitizer
conservan el índice `requests[n]`, la capa, el tipo, la ubicación y
`what_to_do`. Se puede cambiar la raíz sin modificar el repositorio:

```bash
ITL_LOG_ROOT=/ruta/segura/logs ./scripts/subir-lote.sh dev ./mi-lote
```

---

## Si algo no funciona

**`AccessDenied` al subir y los permisos de S3 parecen bien.** Falta
`kms:GenerateDataKey` sobre la CMK del bucket raw. El error no menciona KMS por
ningún lado y se pierde media tarde ahí.

**Subí una parte y no aparece parte de estado.** Comprueba que va bajo
`input/<lote>/` y que el nombre no lleva caracteres raros. Luego:

```bash
aws logs tail /aws/lambda/itl-0003-proxy-ia-dev-lambda-sanitizer-03 --since 10m --region eu-south-2
```

**El lote se queda en `awaiting_parts`.** Falta el submitter: o los
disparadores de Terraform no están aplicados, o la regla de horario está
desactivada. Para moverlo a mano:

```bash
./scripts/ciclo-manual.sh dev
```

**El lote dice `failed` con `custom_id duplicado`.** Dos partes traen el mismo
identificador. Anthropic rechazaría el POST entero sin decir cuál, así que el
proxy lo detecta antes y lo nombra en el estado del lote.
