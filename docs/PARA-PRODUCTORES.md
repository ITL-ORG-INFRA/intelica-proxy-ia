# Cómo mandar lotes al proxy

Guía para quien envía trabajo al proxy. No hace falta saber nada de lo que pasa
por dentro.

## En una frase

Dejas un JSON en un bucket de S3. Al rato aparecen los resultados en otro bucket.

## 1. Preparar el lote

> **La entrada es JSON, no JSONL.** Un `.json` con un array `requests`. JSONL es
> el formato de los **resultados**, no el de la entrada. (Si vienes de la Batch
> API de OpenAI, allí sí se sube un `.jsonl`; la de Anthropic no acepta ficheros,
> recibe un cuerpo JSON.)

```json
{
  "requests": [
    {
      "custom_id": "fila-1",
      "params": {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "messages": [
          {"role": "user", "content": "Resume este expediente: ..."}
        ]
      }
    }
  ],
  "metadata": {"source": "conciliacion-mensual"}
}
```

Reglas que conviene saber antes, porque el proxy es **estricto a propósito**:

| Regla | Por qué |
|---|---|
| Sólo se admiten las claves de arriba | Deny-by-default: cualquier otra rechaza la petición |
| `custom_id` único y opaco | Viaja a Anthropic. Sólo alfanumérico, guion y guion bajo. **No pongas un DNI ni un número de cuenta ahí** |
| Modelos de la lista blanca | `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5-20251001` |
| Sólo texto | Nada de imágenes, PDFs ni base64 |
| Máximo 100.000 peticiones y 100 MB por lote | Si no cabe, pártelo |

## 2. Subirlo

Un lote de un solo fichero va entero a `raw`, bajo `input/`:

```bash
aws s3 cp lote.json s3://itl-0003-proxy-ia-dev-s3-raw-03/input/lote.json --region eu-south-2
```

Y ya está. No hay que llamar a ninguna API ni avisar a nadie: dejar el fichero
dispara todo el proceso.

### Si el lote son varios ficheros

Entonces hacen falta **dos buckets distintos**, y el orden importa:

| Qué | Dónde | Cuándo |
|---|---|---|
| Las partes | `s3://itl-0003-proxy-ia-dev-s3-raw-03/input/<lote>/parte-NN.json` | primero |
| El manifiesto | `s3://itl-0003-proxy-ia-dev-s3-clean-03/input/<lote>/_MANIFEST.json` | **al final** |

El manifiesto es la señal de «ya está todo»: hasta que llega, nadie sabe
cuántas partes tiene el lote. Por eso va el último — subirlo antes no rompe
nada, pero el lote se queda esperando al siguiente barrido en vez de enviarse
en el acto.

Va a `clean` y no a `raw` porque **no lleva datos**: sólo la lista de ficheros
que componen el lote. Dejarlo fuera del entorno protegido es lo que permite que
el submitter —que no puede leer `raw`, y no debe— lo lea por sí mismo.

El nombre tiene que ser **exactamente** `_MANIFEST.json`. Ni `MANIFEST.json`, ni
`_MANIFEST.json.bak`: nada más cierra un lote.

```json
{
  "batch": "lote-agosto",
  "files": ["parte-01.json", "parte-02.json"],
  "total_requests": 4000
}
```

La lista `files` tiene que coincidir **exactamente** con lo que subiste. Si
sobra un nombre, el lote espera una parte que no va a llegar; si falta uno, se
envía incompleto sin que nadie lo note. Ninguno de los dos errores da un
mensaje claro, así que mejor generarlo desde lo que hay en la carpeta —que es
lo que hace `./scripts/subir-lote.sh`.

> **Lo único que puedes escribir en `clean` es el manifiesto, bajo `input/`.**
> El resto de `clean` lo escribe el proxy: `clean/` son los lotes ya
> sanitizados y `status/` los partes de estado. Escribir datos ahí a mano
> saltaría el filtro entero, que es justo lo que este sistema existe para
> impedir.

## 3. Ver qué pasó con tu lote

A los pocos segundos aparece un **parte de estado**:

```bash
aws s3 ls s3://itl-0003-proxy-ia-dev-s3-clean-03/status/ --region eu-south-2
aws s3 cp s3://itl-0003-proxy-ia-dev-s3-clean-03/status/<batch_id>.json - --region eu-south-2
```

Si todo fue bien:

```json
{
  "batch_id": "b_a1b2c3...",
  "status": "clean",
  "request_counts": {"received": 500, "clean": 500, "rejected": 0}
}
```

Si algo se bloqueó:

```json
{
  "status": "quarantined",
  "request_counts": {"received": 500, "clean": 0, "rejected": 3},
  "reason": "gate: 3/500 rechazadas (0.60%) supera el umbral (1.0% o 100 absolutas)",
  "rejections": [
    {"index": 41, "reason": "contenido",
     "findings": [{"layer": 3, "type": "pan",
                    "where": "requests[41].params.messages[0].content",
                    "detail": "visa/16d/contiguo"}]}
  ],
  "what_to_do": [
    "Se detecto un numero de tarjeta en texto libre. Quitalo del origen: el proxy no lo enmascara, lo bloquea."
  ]
}
```

El parte te dice **qué petición**, **qué capa** la paró y **qué hacer**. Nunca
incluye el valor que la disparó: te dice que en `requests[41]` había una Visa de
16 dígitos, no cuál era.

## 4. Recoger los resultados

Anthropic procesa el lote en menos de 24 horas (la mayoría acaba en menos de
una). Cuando termina, los resultados aparecen solos:

```bash
aws s3 cp s3://itl-0003-proxy-ia-dev-s3-results-03/results/<batch_id>.jsonl . --region eu-south-2
```

Es un JSONL: una línea por petición, con el `custom_id` que tú pusiste.

> **Cruza los resultados por `custom_id`, nunca por posición.** Anthropic
> devuelve los resultados en **orden arbitrario** — no en el que los mandaste. Y
> el proxy además descarta cualquier resultado que no pase el segundo pase de
> sanitización, así que el fichero puede tener menos líneas que peticiones
> enviaste.
>
> Emparejar por número de línea no da un error: da datos correctos asignados a
> la fila equivocada, y eso no se detecta hasta que alguien lo mira. Por eso el
> `custom_id` es obligatorio y tiene que ser único.

```python
import json
respuestas = {}
with open("resultados.jsonl", encoding="utf-8") as f:
    for linea in f:
        entrada = json.loads(linea)
        respuestas[entrada["custom_id"]] = entrada["result"]

# Y ahora sí, contra tus datos de origen:
for fila in mis_filas:
    resultado = respuestas.get(fila.custom_id)
    if resultado is None:
        pass  # descartada por el filtro o no procesada: revisa el parte de estado
```

Cada entrada trae `result.type`: `succeeded`, `errored`, `canceled` o `expired`.
Sólo en `succeeded` hay contenido, en `result.message.content`.

## Lo que el proxy bloquea

Es un componente PCI: su trabajo es que ningún dato de tarjeta salga hacia
Anthropic. Bloquea, no enmascara — si detecta algo, tu petición no se envía.

| Se detecta | Qué pasa |
|---|---|
| Número de tarjeta en texto libre | Se rechaza esa petición |
| Datos de banda, CVV o PIN | **Se aborta el lote entero** |
| Campos llamados `pan`, `cvv`, `track`… | Se destruye el valor |
| Imágenes, PDFs, base64 | Se rechaza esa petición |
| Claves fuera del esquema | Se rechaza esa petición |

Y hay un **gate de lote**: si se rechaza más del 1 % de las peticiones, no pasa
ninguna. No es castigo — muchos rechazos suelen indicar que la fuente de datos
tiene un problema, y mandar "sólo las buenas" lo normalizaría.

> Un detalle que sorprende: el proxy normaliza el texto antes de mirarlo. Un
> número escrito con caracteres de ancho completo, con espacios invisibles o
> partido con guiones raros se detecta igual. No busques la forma de que pase:
> el objetivo es que el dato no salga de tu origen.

## Permisos que necesitas

Pídeselos a infraestructura. Son estos y sólo estos:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DejarLasPartes",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::itl-0003-proxy-ia-dev-s3-raw-03/input/*"
    },
    {
      "Sid": "CerrarElLoteConElManifiesto",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::itl-0003-proxy-ia-dev-s3-clean-03/input/*"
    },
    {
      "Sid": "CifrarAlSubir",
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Encrypt"],
      "Resource": "arn:aws:kms:eu-south-2:<cuenta>:key/<id-CMK-raw>"
    },
    {
      "Sid": "VerEstadoYResultados",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::itl-0003-proxy-ia-dev-s3-clean-03",
        "arn:aws:s3:::itl-0003-proxy-ia-dev-s3-clean-03/status/*",
        "arn:aws:s3:::itl-0003-proxy-ia-dev-s3-results-03",
        "arn:aws:s3:::itl-0003-proxy-ia-dev-s3-results-03/*"
      ]
    },
    {
      "Sid": "DescifrarLoQueLees",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:eu-south-2:<cuenta>:key/<id-CMK-clean>"
    }
  ]
}
```

Tres cosas que **no** incluye, y ninguna es un olvido:

- **No puedes releer lo que subiste.** El bucket `raw` es de sólo escritura para
  ti. Lo que subes puede contener datos de tarjeta —de eso va todo esto—, y una
  vez dentro del entorno protegido no sale. Lo que sí puedes ver es tu lote ya
  sanitizado, en `clean/`.
- **No tienes `kms:Decrypt` sobre la clave del entorno protegido.** Sólo sobre la
  de la zona limpia. Es lo que mantiene tu identidad fuera del alcance de la
  auditoría PCI, que te conviene a ti tanto como a nosotros.
- **En `clean` sólo puedes escribir bajo `input/`,** que es donde va el
  manifiesto. No hay `PutObject` sobre `clean/*` ni sobre `status/*`: ahí
  escribe el proxy. Si pudieras dejar datos directamente en `clean/`, estarías
  metiendo en la zona limpia algo que no ha pasado por el filtro — y el
  submitter lo enviaría a Anthropic sin mirarlo.

## Si algo no funciona

**`AccessDenied` al subir, y los permisos de S3 parecen bien.** Casi siempre
falta `kms:GenerateDataKey`. El error no menciona KMS por ninguna parte.

**Subí el fichero y no aparece ningún parte de estado.** El nombre no puede
llevar caracteres raros y tiene que ir bajo `input/`. Si persiste, avisa a
infraestructura: puede que el disparador esté caído.

**Subí el manifiesto y el lote no se envía.** Comprueba tres cosas: que está en
el bucket `clean` (no en `raw`), que la carpeta del lote es la misma en los dos
buckets (`input/<lote>/`), y que se llama exactamente `_MANIFEST.json`. El
estado del lote lo dice:

```bash
aws dynamodb get-item --table-name itl-0003-proxy-ia-dev-ddb-batches-03 \
  --key '{"batch_id":{"S":"batch#input/<lote>"}}' --region eu-south-2
```

`awaiting_parts` = alguna parte sigue en el sanitizer · `quarantined` = alguna
parte fue rechazada y no se envía ninguna · `submitted` = ya está en Anthropic.

**El parte dice `quarantined` y no entiendo por qué.** Mira `rejections[].findings`
y `what_to_do`. Si el motivo es `envelope invalido`, es el esquema; si es una capa
3, 4 o 5, es contenido.
