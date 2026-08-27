# Cómo mandar lotes al proxy

Guía para quien envía trabajo al proxy. No hace falta saber nada de lo que pasa
por dentro.

## En una frase

Dejas un JSON en un bucket de S3. Al rato aparecen los resultados en otro bucket.

## 1. Preparar el lote

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
  "metadata": {"origen": "conciliacion-mensual"}
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

```bash
aws s3 cp lote.json s3://intelica-proxy-ia-dev-raw-<cuenta>/entrada/lote.json --region eu-south-2
```

Y ya está. No hay que llamar a ninguna API ni avisar a nadie: dejar el fichero
dispara todo el proceso.

## 3. Ver qué pasó con tu lote

A los pocos segundos aparece un **parte de estado**:

```bash
aws s3 ls s3://intelica-proxy-ia-dev-clean-<cuenta>/estado/ --region eu-south-2
aws s3 cp s3://intelica-proxy-ia-dev-clean-<cuenta>/estado/<batch_id>.json - --region eu-south-2
```

Si todo fue bien:

```json
{
  "batch_id": "b_a1b2c3...",
  "estado": "limpio",
  "peticiones": {"recibidas": 500, "limpias": 500, "rechazadas": 0}
}
```

Si algo se bloqueó:

```json
{
  "estado": "cuarentena",
  "peticiones": {"recibidas": 500, "limpias": 0, "rechazadas": 3},
  "motivo": "gate: 3/500 rechazadas (0.60%) supera el umbral (1.0% o 100 absolutas)",
  "rechazos": [
    {"indice": 41, "motivo": "contenido",
     "hallazgos": [{"capa": 3, "tipo": "pan",
                    "donde": "requests[41].params.messages[0].content",
                    "detalle": "visa/16d/contiguo"}]}
  ],
  "que_hacer": [
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
aws s3 cp s3://intelica-proxy-ia-dev-results-<cuenta>/results/<batch_id>.jsonl . --region eu-south-2
```

Es un JSONL: una línea por petición, con el `custom_id` que tú pusiste.

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
      "Sid": "DejarLotes",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::intelica-proxy-ia-dev-raw-<cuenta>/entrada/*"
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
        "arn:aws:s3:::intelica-proxy-ia-dev-clean-<cuenta>",
        "arn:aws:s3:::intelica-proxy-ia-dev-clean-<cuenta>/estado/*",
        "arn:aws:s3:::intelica-proxy-ia-dev-results-<cuenta>",
        "arn:aws:s3:::intelica-proxy-ia-dev-results-<cuenta>/*"
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

Dos cosas que **no** incluye, y no es un olvido:

- **No puedes releer lo que subiste.** El bucket `raw` es de sólo escritura para
  ti. Lo que subes puede contener datos de tarjeta —de eso va todo esto—, y una
  vez dentro del entorno protegido no sale. Lo que sí puedes ver es tu lote ya
  sanitizado, en `clean/`.
- **No tienes `kms:Decrypt` sobre la clave del entorno protegido.** Sólo sobre la
  de la zona limpia. Es lo que mantiene tu identidad fuera del alcance de la
  auditoría PCI, que te conviene a ti tanto como a nosotros.

## Si algo no funciona

**`AccessDenied` al subir, y los permisos de S3 parecen bien.** Casi siempre
falta `kms:GenerateDataKey`. El error no menciona KMS por ninguna parte.

**Subí el fichero y no aparece ningún parte de estado.** El nombre no puede
llevar caracteres raros y tiene que ir bajo `entrada/`. Si persiste, avisa a
infraestructura: puede que el disparador esté caído.

**El parte dice `cuarentena` y no entiendo por qué.** Mira `rechazos[].hallazgos`
y `que_hacer`. Si el motivo es `envelope invalido`, es el esquema; si es una capa
3, 4 o 5, es contenido.
