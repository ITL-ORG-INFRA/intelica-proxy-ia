# Prompt para el agente de Terraform — disparo por manifiesto

> Copia todo lo que hay debajo de la línea y pásaselo a tu agente en el repo de
> Terraform. Es un cambio incremental sobre lo que ya está desplegado.

---

Necesito cambiar cómo se dispara el submitter de **`intelica-proxy-ia`**. Es un
cambio incremental: el resto de la infraestructura no se toca.

## Qué cambia y por qué

Hasta ahora cada fichero que aterrizaba en el bucket `raw` era un lote
independiente, y el submitter corría por horario buscando trabajo en DynamoDB.

El flujo real es otro: los productores suben **varias partes** de un mismo lote y
cierran con un fichero `_MANIFEST.json` que se sube **al final**. Ese manifiesto
es la señal de "ya está todo", y es lo que debe disparar el envío.

```
raw/input/lote-2026-08-27/parte-01.json  ─┐
raw/input/lote-2026-08-27/parte-02.json  ─┼─► sanitizer (uno por fichero)
raw/input/lote-2026-08-27/parte-03.json  ─┘        │
                                                     └─► clean/ o quarantine/

raw/input/lote-2026-08-27/_MANIFEST.json ─────────────► submitter
                                                            ¿están todas las
                                                            partes sanitizadas?
                                                            sí → POST a Anthropic
                                                            no → espera
```

El código de las Lambdas ya está desplegado y soporta esto. Lo que falta es que
la infraestructura entregue los eventos correctos.

## 1. Dos filtros de sufijo, disjuntos

La regla de EventBridge `${prefijo}-raw-creado` dispara hoy el sanitizer **con
cualquier objeto** del bucket raw. Hay que partirla en dos, con filtros que no se
solapen:

| Regla | Patrón | Destino |
|---|---|---|
| `${prefijo}-raw-datos` | bucket = raw, `object.key` **con sufijo** `.json` **y sin sufijo** `_MANIFEST.json` | Lambda `sanitizer` |
| `${prefijo}-raw-manifiesto` | bucket = raw, `object.key` **con sufijo** `_MANIFEST.json` | Lambda `submitter` |

**El solapamiento importa.** `_MANIFEST.json` también acaba en `.json`, así que un
filtro de sufijo `.json` a secas se lo lleva también y el sanitizer lo procesaría.
EventBridge no tiene un "termina en X pero no en Y" directo: usa
`anything-but` con `suffix`, o filtra por prefijo del nombre. En patrón de
EventBridge:

```json
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": { "name": ["<bucket-raw>"] },
    "object": {
      "key": [{ "suffix": ".json" }, { "anything-but": { "suffix": "_MANIFEST.json" } }]
    }
  }
}
```

Si esa combinación no evalúa como esperas, verifícala con
`aws events test-event-pattern` antes de aplicar — no la des por buena. El código
de la Lambda tiene una guarda defensiva que ignora el manifiesto si le llega, así
que un filtro mal puesto no rompe nada, pero deja el sanitizer invocándose de más.

La regla del manifiesto es directa:

```json
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": { "name": ["<bucket-raw>"] },
    "object": { "key": [{ "suffix": "_MANIFEST.json" }] }
  }
}
```

Añade el `aws_lambda_permission` correspondiente para que EventBridge pueda
invocar el submitter, acotado al ARN de esa regla.

## 2. El horario del submitter se queda

**No quites la regla `${prefijo}-submitter`.** Sigue haciendo falta.

El manifiesto puede llegar **antes** de que el sanitizer acabe con alguna parte
—los ficheros se procesan en paralelo y uno grande tarda más—. En ese caso el
evento del manifiesto no puede enviar nada: deja el lote en `awaiting_parts` y
sale. Sin el barrido por horario, ese lote se queda quieto para siempre y nadie
se entera.

El tick del submitter ahora hace dos cosas: lo que hacía antes, y barrer los
lotes pendientes. `rate(5 minutes)` sigue estando bien.

## 3. Permisos del rol del submitter

El submitter necesita **leer el bucket raw**, sólo para el manifiesto:

```hcl
{
  Sid      = "LeerManifiestos"
  Effect   = "Allow"
  Action   = ["s3:GetObject"]
  Resource = ["arn:aws:s3:::<bucket-raw>/*/_MANIFEST.json"]
}
```

**Esto toca la frontera del diseño, así que hay que hacerlo con cuidado.** El rol
del submitter tiene hoy un `Deny` explícito sobre `arn:aws:s3:::<bucket-raw>/*`, y
ese Deny es el corazón del modelo: quien habla con Anthropic no lee datos de
tarjeta.

Un `Deny` gana siempre sobre un `Allow`, así que hay que **acotar el Deny** en vez
de quitarlo:

```hcl
{
  Sid    = "JamasCHD"
  Effect = "Deny"
  Action = ["s3:*"]
  NotResource = [
    "arn:aws:s3:::<bucket-raw>/*/_MANIFEST.json"
  ]
  # ... más el resto de recursos del CDE que ya denegaba
}
```

O más simple y más seguro: mantener el `Deny` sobre `<bucket-raw>/*` tal como
está y **añadir una condición** que exceptúe sólo esa clave. Elige la forma que
mejor exprese la intención en este repo, pero cumple estas dos:

- El submitter **sólo** puede leer objetos cuya clave acaba en `_MANIFEST.json`.
- El submitter sigue **sin poder** leer las partes de datos ni el bucket
  `quarantine`, ni descifrar con **CMK-raw**.

Ese último punto es el que de verdad cierra el argumento: un manifiesto es una
lista de nombres de fichero y un número. No contiene datos de tarjeta. Las partes
sí, y ésas siguen fuera de su alcance.

Si prefieres no tocar el Deny en absoluto, la alternativa es que el **manifiesto
se suba a un bucket distinto** —fuera del CDE— en lugar de junto a las partes. Es
más limpio desde el punto de vista de la frontera, pero cambia el flujo de trabajo
del productor, así que dilo antes de hacerlo.

## 4. Nada más

No hace falta:

- **SQS.** Se valoró y se descartó: la espera la resuelve el barrido por horario,
  que ya existe. Una cola añadiría una pieza para un problema que ya está cubierto.
- Cambios en DynamoDB. La tabla actual sirve: los nuevos items (`batch#...` y
  `part#...`) usan la misma clave de partición y el mismo GSI `status-index`.
- Cambios en los buckets, las claves KMS, las alarmas ni el resto de Lambdas.

## Cómo comprobar que quedó bien

```bash
aws events test-event-pattern \
  --event-pattern file://patron-datos.json \
  --event file://evento-manifiesto.json \
  --region eu-south-2
```

Debe devolver `false` para el patrón de datos con un evento de `_MANIFEST.json`, y
`true` con un evento de `parte-01.json`. Al revés para el patrón del manifiesto.

Y después de aplicar, con un lote de prueba real:

```bash
aws s3 cp parte-01.json     s3://<bucket-raw>/input/lote-prueba/parte-01.json
aws s3 cp _MANIFEST.json    s3://<bucket-raw>/input/lote-prueba/_MANIFEST.json
```

```bash
aws dynamodb get-item --table-name <tabla> \
  --key '{"batch_id":{"S":"batch#input/lote-prueba"}}' --region eu-south-2
```

El item debe existir con `status = enviado` y un `batch_ids` no vacío. Si dice
`awaiting_parts`, el sanitizer aún no acabó y el siguiente tick lo recogerá; si
dice `quarantined`, alguna parte fue rechazada.
