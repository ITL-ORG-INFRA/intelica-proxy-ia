# Prompt para el agente de Terraform — cola del supervisor

> Copia todo lo que hay debajo de la línea y pásaselo a tu agente en el repo de
> Terraform. Es un cambio incremental que **quita más piezas de las que añade**.

---

Necesito sustituir el polling por horario de **`intelica-proxy-ia`** por una cola
SQS. El código de las Lambdas se está adaptando en paralelo; esto es la parte de
infraestructura.

## Qué cambia y por qué

Hoy el reconciler y el fetcher se despiertan cada 5 minutos aunque no haya
nada que hacer. El coste es despreciable, así que el cambio **no es por ahorro**:
es por latencia —hasta 10 minutos entre que Anthropic acaba y el resultado
aparece en S3— y para que «hay trabajo pendiente» sea algo observable en vez de
un `if` dentro de una Lambda.

```
submitter ──POST──► Anthropic
    ├─ escribe DynamoDB
    └─ encola {batch_id} con DelaySeconds=300
              │
              ▼
         [ SQS ] ──► supervisor ──┬─ sigue en proceso → se reencola
                                  └─ terminó → invoca al fetcher (asíncrono)
```

Cuando no hay lotes en vuelo la cola está vacía y **no se invoca nada**.

## Se añade

**Cola SQS** `${prefijo}-supervisor`

| Ajuste | Valor | Por qué |
|---|---|---|
| `visibility_timeout_seconds` | **60** | El supervisor tarda ~2 s: pregunta a Anthropic y dispara al fetcher sin esperarlo. 60 s da margen de sobra para un arranque en frío |
| `message_retention_seconds` | **86400** (24 h) | Es exactamente lo que tarda un lote en expirar en Anthropic. Un mensaje más viejo que eso ya no sirve |
| `delay_seconds` (de la cola) | **0** | El retraso lo pone quien envía el mensaje, no la cola |

> **El visibility timeout depende de una decisión del código**: el supervisor
> invoca al fetcher de forma **asíncrona** y no espera. Si en algún momento eso
> cambiara a síncrono, 60 s sería demasiado poco — el mensaje volvería a la cola
> a mitad de una descarga y se descargaría el mismo lote dos veces. Si ves que el
> código espera al fetcher, para y avisa antes de aplicar.

**DLQ** `${prefijo}-supervisor-dlq`

- `redrive_policy` en la cola principal con `maxReceiveCount = 5`
- `message_retention_seconds`: 14 días

**Event source mapping** SQS → Lambda `${nombre}-lambda-reconciler-03`

- `batch_size`: 10
- `maximum_batching_window_in_seconds`: 0 — que no espere a juntar mensajes

**Alarma** `${prefijo}-dlq-con-mensajes` — **crítica**

- Namespace `AWS/SQS`, métrica `ApproximateNumberOfMessagesVisible`
- Dimensión: la DLQ
- `statistic: Maximum`, `period: 300`, `threshold: 1`,
  `comparison: GreaterThanOrEqualToThreshold`
- Acción: el topic SNS existente

Un mensaje en esa DLQ es **un lote que está en Anthropic, que se va a facturar, y
del que nadie va a recoger los resultados**. Es de las alarmas más importantes
del sistema.

**Regla de horario nueva** `${prefijo}-barrido` con `rate(1 hour)` → Lambda
`${nombre}-lambda-reconciler-03`

Es la red de seguridad: busca lotes en vuelo que lleven demasiado tiempo sin
consultarse, o sea, aquellos cuyo mensaje se perdió. Convierte «se perdió un
aviso» en una hora de retraso en vez de un huérfano permanente. **No la omitas
por parecer redundante**: sin ella el diseño tiene un punto único de olvido y el
fallo es silencioso.

## Se quita

| Recurso | Por qué |
|---|---|
| Regla `${nombre}-lambda-reconciler-03` `rate(5 minutes)` | La sustituye la cola |
| Regla `${prefijo}-fetcher` `rate(5 minutes)` | Lo invoca el supervisor |

**Saldo neto: dos reglas menos, una cola, una DLQ y una regla horaria.**

## Permisos

Las tres Lambdas de la zona limpia (submitter, reconciler, fetcher) comparten
`${prefijo}-rol-submitter`, así que sólo cambia ese rol:

```hcl
{
  Sid    = "EncolarYConsumir"
  Effect = "Allow"
  Action = [
    "sqs:SendMessage",
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ChangeMessageVisibility",
  ]
  Resource = [aws_sqs_queue.supervisor.arn]
}
{
  Sid      = "InvocarAlFetcher"
  Effect   = "Allow"
  Action   = ["lambda:InvokeFunction"]
  Resource = [aws_lambda_function.fetcher.arn]   # SOLO esa función
}
```

Dos cosas que no deben ampliarse al hacer esto:

- **`lambda:InvokeFunction` acotado al ARN del fetcher.** No un comodín sobre las
  funciones del proyecto. El supervisor tiene que poder despertar al fetcher y a
  nadie más.
- **El `Deny` sobre `raw`, `quarantine` y CMK-raw se queda intacto.** Nada de
  este cambio necesita tocarlo. Si al refactorizar el rol ese Deny desaparece, la
  frontera del sistema se ha roto: quien habla con Anthropic no puede leer datos
  de tarjeta.

## Cifrado

La cola lleva `batch_id` y nada más — ni contenido, ni identificadores de
cliente. Aun así, activa el cifrado en reposo con la clave gestionada por AWS
para SQS (`sqs_managed_sse_enabled = true`). No hace falta CMK propia: no hay
datos de tarjeta en los mensajes, igual que en la tabla de DynamoDB.

## Orden de despliegue

El código y la infraestructura tienen que llegar en un orden concreto o hay una
ventana en la que nada procesa:

1. **Terraform primero**, creando la cola, la DLQ, el mapeo y la regla horaria,
   **pero sin quitar todavía** las dos reglas de 5 minutos.
2. Se despliega el código nuevo desde el repo `intelica-proxy-ia`.
3. Se comprueba que un lote recorre el flujo por la cola.
4. **Terraform otra vez**, ahora sí quitando las dos reglas de 5 minutos.

Si se quitan las reglas en el paso 1, entre ese momento y el paso 2 no hay ni
polling por horario ni cola funcionando, y los lotes en vuelo se quedan parados
hasta que alguien lo note.

## Cómo comprobar que quedó bien

```bash
aws sqs get-queue-attributes --queue-url <url> --region eu-south-2 \
  --attribute-names VisibilityTimeout MessageRetentionPeriod RedrivePolicy
```

`VisibilityTimeout` debe ser 60 y `RedrivePolicy` debe apuntar a la DLQ con
`maxReceiveCount: 5`.

Y tras subir un lote de prueba, la cola debe vaciarse sola cuando el lote llegue
a `delivered`. Si quedan mensajes dando vueltas, el supervisor no los está
borrando y acabarán en la DLQ.
