# Prompt para el agente de Terraform

> Copia todo lo que hay debajo de la línea y pásaselo a tu agente en el repo de
> Terraform. Está escrito para que pueda trabajar sin ver el repo de la aplicación.

---

Necesito que traduzcas a Terraform la infraestructura de **`intelica-proxy-ia`**, un
proxy de sanitización PCI hacia la Message Batches API de Anthropic. Ya existe un
script `deploy/deploy.sh` que crea todo con AWS CLI; tu trabajo es reproducir esos
mismos recursos como código en este repo.

**Antes de escribir nada, mira cómo está organizado este repositorio** (estructura de
módulos, backend de estado, `locals` de etiquetas, versión del provider) y ajústate a
ello. Lo que sigue describe *qué* recursos hacen falta y con *qué* configuración, no
cómo organizarlos aquí.

## La convención de nombres

Todos los recursos siguen el criterio de la cuenta:

```
itl-<assetid>-<app>-<environment>-<type>-<descriptor>-<stack>
```

Para este proyecto: `assetid = 0003`, `app = proxy-ia`, `stack = 03`. Es decir
`itl-0003-proxy-ia-dev-lambda-sanitizer-03`.

En lo que sigue se escribe **`${nombre}`** como abreviatura de
`itl-0003-proxy-ia-${environment}` y la secuencia `-03` va siempre al final, de
modo que `${nombre}-lambda-sanitizer-03` es el nombre completo.

Estos nombres **ya están desplegados en dev** y el código de la aplicación los da
por buenos: los construye en un único sitio (`scripts/lib/nombres.sh`) y una
prueba los compara uno a uno contra esta lista. Cambiar cualquiera de ellos rompe
el despliegue del código, no sólo el `apply`.

## El invariante que no se puede romper

Todo el diseño se sostiene sobre una sola propiedad:

> **Ningún rol IAM tiene a la vez acceso a datos de tarjeta (CHD) y salida a internet.**

Si al refactorizar algo se pierde eso, la infraestructura deja de servir para lo que
se hizo. Hay dos zonas:

| Zona | Buckets | Clave KMS | Roles |
|---|---|---|---|
| **CDE** (datos de tarjeta) | `raw`, `quarantine` | CMK-raw | sanitizer, verifier, canary |
| **Limpia** | `clean`, `results` | CMK-clean | submitter |

El rol `submitter` es el único que habla con Anthropic, y lleva **Deny explícito**
sobre los buckets del CDE y sobre CMK-raw. Ese Deny es la pieza central: no lo
conviertas en "simplemente no le damos Allow". Tiene que ser un Deny.

## Región y contexto

- Región: **`eu-south-2`** (España). Es *opt-in*: hay que habilitarla en la cuenta.
- **Tres entornos: `dev`, `qa` y `prod`.** Cada uno es una pila completa e
  independiente — sus propios buckets, sus propias claves KMS, sus propios roles
  y sus propias Lambdas. No se comparte nada entre entornos: un bucket `clean`
  compartido entre qa y prod sería una vía para que datos de un entorno acaben
  procesándose en otro.
- Una sola cuenta AWS. Las Lambdas van **fuera de VPC** (es una decisión del MVP).
- Runtime: **`python3.13`**, arquitectura **`arm64`**.

## Recursos

### 1. KMS — 2 claves simétricas, con rotación automática

| Alias | Uso | `DataClassification` |
|---|---|---|
| `alias/${nombre}-kms-raw-03` | cifra buckets raw y quarantine | `chd` |
| `alias/${nombre}-kms-clean-03` | cifra buckets clean y results | `sin-chd` |

Política de clave: la de por defecto (root de la cuenta), para que las políticas IAM
funcionen y no haya riesgo de dejar la clave inutilizable.

### 2. S3 — 4 buckets

| Bucket | Clave | Retención | Clasificación |
|---|---|---|---|
| `${nombre}-s3-raw-03` | CMK-raw | 7 días | `chd` |
| `${nombre}-s3-quarantine-03` | CMK-raw | 90 días (prefijo `quarantine/`) | `chd` |
| `${nombre}-s3-clean-03` | CMK-clean | 30 días (prefijo `clean/`) | `sin-chd` |
| `${nombre}-s3-results-03` | CMK-clean | 30 días (prefijo `results/`) | `sin-chd` |

Todos con:
- Bloqueo total de acceso público (los cuatro flags).
- Cifrado SSE-KMS con su clave, **y `bucket_key_enabled = true`** (reduce las llamadas
  a KMS hasta un 99 %; sin esto el coste de KMS se dispara).
- Versionado activado.
- Ciclo de vida: expiración según la tabla, más borrado de versiones no vigentes a los
  7 días y aborto de multipart incompletos al día.
- *Bucket policy* que **deniega todo `s3:*` con `aws:SecureTransport = false`**.

Además, en **`raw` y `clean`** hay que activar la publicación de eventos a EventBridge
(`eventbridge = true` en la notificación del bucket). Sin eso, el sanitizer y el
verifier no se disparan nunca — y el fallo es silencioso.

### 3. DynamoDB — 1 tabla

- Nombre: `${nombre}-ddb-batches-03`, `PAY_PER_REQUEST`.
- Clave de partición: `batch_id` (String).
- GSI **`status-index`**: hash `status` (String), range `created_at` (String),
  proyección `ALL`.
- TTL activado sobre el atributo **`ttl`**.
- Point-in-time recovery activado.
- Cifrado con la clave gestionada por AWS (aquí **no** hace falta CMK: la tabla sólo
  guarda estados, contadores y punteros, nunca CHD).

### 4. Secrets Manager — 1 secreto

- Nombre: `intelica-proxy-ia-${environment}/anthropic-api-key`. **No sigue la
  convención y no debe cambiarse:** volver a meter la clave exige que la teclee
  una persona, y renombrar el secreto por simetría no compra nada.
- Contenido: JSON `{"api_key": "..."}`.
- **El valor no debe estar en Terraform.** Crea el recurso del secreto pero deja la
  versión fuera del estado (`ignore_changes` sobre el valor, o créalo vacío y que se
  rellene por fuera). Una API key en el state file es una fuga.

### 5. SNS — 1 topic + suscripción de correo

`${nombre}-sns-alarms-03`, con suscripción `email` a una variable `alarm_email`.

### 6. IAM — 4 roles

Todos con `AWSLambdaBasicExecutionRole` más una política inline. `trust policy` para
`lambda.amazonaws.com`.

**`${nombre}-role-sanitizer-03`** (clasificación `chd`):
- Allow `s3:GetObject`, `s3:ListBucket` sobre bucket raw y su contenido.
- Allow `s3:PutObject` sobre `quarantine/*` y sobre `clean/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` sobre **ambas** claves.
- Allow DynamoDB `PutItem`, `GetItem`, `UpdateItem`, `Query` sobre la tabla y sus índices.
- Allow `cloudwatch:PutMetricData` (`Resource: "*"`).
- **Deny `secretsmanager:GetSecretValue` sobre `*`.**

**`${nombre}-role-verifier-03`** (clasificación `chd`):
- Allow `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket` sobre clean.
  (El `DeleteObject` es intencionado: si encuentra un PAN en la zona limpia, lo retira.)
- Allow `s3:PutObject` sobre `quarantine/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` sobre ambas claves.
- Allow DynamoDB (igual que arriba) y `cloudwatch:PutMetricData`.
- **Deny `secretsmanager:GetSecretValue` sobre `*`.**

**`${nombre}-role-submitter-03`** (clasificación `sin-chd`) — lo usan submitter,
reconciler y fetcher:
- Allow `s3:GetObject`, `s3:ListBucket` sobre clean.
- Allow `s3:PutObject`, `s3:AbortMultipartUpload` sobre `results/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` **sólo sobre CMK-clean**.
- Allow `secretsmanager:GetSecretValue` sobre el secreto de Anthropic.
- Allow DynamoDB y `cloudwatch:PutMetricData`.
- **Deny `s3:*` sobre los buckets raw y quarantine (bucket y contenido).**
- **Deny `kms:*` sobre CMK-raw.**

**`${nombre}-role-canary-03`** (clasificación `chd`):
- Allow `s3:PutObject`, `s3:GetObject` sobre `raw/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` sobre CMK-raw.
- Allow DynamoDB y `cloudwatch:PutMetricData`.
- **Deny `secretsmanager:GetSecretValue` sobre `*`.**

### 7. Lambda — 1 layer + 6 funciones

Layer `${nombre}-lambda-deps-03`: `anthropic==0.69.0` compilado para `manylinux2014_aarch64` y
Python 3.13. `boto3` **no** va en el layer (ya está en el runtime).

Todas las funciones usan handler **`handler.lambda_handler`**, X-Ray activo
(`tracing_config = Active`) y las mismas variables de entorno (ver abajo).

| Función | Rol | Memoria | Timeout | `/tmp` | Clasif. |
|---|---|---|---|---|---|
| `${nombre}-lambda-sanitizer-03` | role-sanitizer | 3008 | 600 s | 512 | `chd` |
| `${nombre}-lambda-verifier-03` | role-verifier | 2048 | 600 s | 512 | `chd` |
| `${nombre}-lambda-canary-03` | role-canary | 512 | 120 s | 512 | `chd` |
| `${nombre}-lambda-submitter-03` | role-submitter | 1024 | 300 s | 512 | `sin-chd` |
| `${nombre}-lambda-reconciler-03` | role-submitter | 512 | 120 s | 512 | `sin-chd` |
| `${nombre}-lambda-fetcher-03` | role-submitter | 2048 | 900 s | **4096** | `sin-chd` |

**Concurrencia reservada = 1** en submitter, reconciler, fetcher y canary. Dos
ejecuciones simultáneas se pisarían el contador de la cola en vuelo en DynamoDB y el
presupuesto de peticiones de Anthropic. No es una optimización, es corrección.

La memoria del sanitizer tampoco es arbitraria: en el MVP procesa el lote entero en
memoria, sin Step Functions Distributed Map.

**Empaquetado.** Cada función se arma con `src/common/` más su propia carpeta, todo
en plano en la raíz del zip (no en subdirectorios). Dos funciones necesitan carpetas
extra:
- `verifier` = `common` + `sanitizer` + `verifier`
- `fetcher` = `common` + `sanitizer` + `fetcher`

(porque reutilizan los detectores). El resto es `common` + su carpeta.

Grupos de log `/aws/lambda/<función>` con retención de **90 días**, creados
explícitamente para que Terraform los gestione y no aparezcan sin retención.

### 8. EventBridge — 7 reglas

Tres por evento de S3:

| Regla | Destino | Patrón |
|---|---|---|
| `${nombre}-evb-raw-created-03` | sanitizer | `source: aws.s3`, `detail-type: Object Created`, bucket = raw. **Sin filtro de prefijo** (el canary escribe en `canary/` y también debe dispararlo). |
| `${nombre}-evb-clean-created-03` | verifier | ídem con bucket = clean y `object.key` con prefijo **`clean/`** |
| `${nombre}-evb-manifest-03` | submitter | bucket = clean, `object.key` con prefijo **`input/`** y sufijo **`_MANIFEST.json`** |

Los prefijos de la segunda y la tercera no son intercambiables, y es lo más fácil
de romper de todo este fichero:

- El bucket `clean` tiene **tres** prefijos. `clean/` son los lotes ya
  sanitizados, `status/` los partes de estado que el productor lee, e `input/` el
  manifiesto que cierra un lote de varias partes.
- **La regla del verifier tiene que filtrar por `clean/`.** Si se amplía a todo el
  bucket, el verifier se despierta con cada parte de estado y con cada manifiesto
  —documentos que no traen `requests`— y marcaría `verified` un `batch_id` que en
  realidad es una clave de S3. El código tiene una guarda que lo ignora, pero la
  guarda es la segunda línea de defensa, no la primera.
- **La regla del manifiesto va sobre `clean`, no sobre `raw`.** Las partes se
  suben a raw y el manifiesto a clean, bajo la misma carpeta `input/<lote>/`. El
  submitter **no tiene permiso de lectura sobre raw** —ese Deny es el invariante
  del apartado de arriba—, así que un manifiesto en raw lo dejaría sin poder
  leerlo, y el lote moriría como "manifiesto ilegible".
- **El sufijo tiene que ser exacto.** `_MANIFEST.json`, no `MANIFEST.json` ni
  `*_MANIFEST.json*`.

Y cuatro por horario:

| Regla | Expresión |
|---|---|
| `${nombre}-evb-submitter-03` | `rate(5 minutes)` |
| `${nombre}-evb-reconciler-03` | `rate(5 minutes)` |
| `${nombre}-evb-fetcher-03` | `rate(5 minutes)` |
| `${nombre}-evb-canary-03` | `rate(1 hour)` |

Cada una con su *target* a la Lambda y su `aws_lambda_permission` para
`events.amazonaws.com` acotado al ARN de la regla.

### 9. CloudWatch — 8 alarmas

Todas: `statistic = Sum`, `period = 300`, `evaluation_periods = 1`,
`comparison_operator = GreaterThanOrEqualToThreshold`,
`treat_missing_data = notBreaching`, acción → el topic SNS,
dimensión `Entorno = ${environment}`.

| Alarma | Namespace | Métrica | Umbral |
|---|---|---|---|
| `${nombre}-cw-canary-not-blocked-03` | `IntelicaProxyIA/Canary` | `CanaryNotBlocked` | 1 |
| `${nombre}-cw-canary-not-processed-03` | `IntelicaProxyIA/Canary` | `CanaryNotProcessed` | 1 |
| `${nombre}-cw-sanitizer-failure-03` | `IntelicaProxyIA/Verifier` | `SanitizerFailure` | 1 |
| `${nombre}-cw-pan-in-results-03` | `IntelicaProxyIA/Fetcher` | `PanInResults` | 1 |
| `${nombre}-cw-hard-block-03` | `IntelicaProxyIA/Sanitizer` | `HardBlock` | 1 |
| `${nombre}-cw-batches-quarantined-03` | `IntelicaProxyIA/Sanitizer` | `BatchesQuarantined` | 3 |
| `${nombre}-cw-batches-expired-03` | `IntelicaProxyIA/Reconciler` | `BatchesExpired` | 1 |

Las cuatro primeras son **críticas**: significan que un control de seguridad falló.

Y una distinta: `${nombre}-cw-queue-almost-full-03`, namespace
`IntelicaProxyIA/Submitter`, métrica `QueueOccupancy`, `statistic = Maximum`,
`evaluation_periods = 2`, umbral **80**, operador `GreaterThanThreshold`.

**Los nombres de métrica son contrato con el código.** Una alarma sobre una
métrica que nadie emite no salta: se queda en `INSUFFICIENT_DATA`, que no es un
estado de alarma. Es decir, equivocarse aquí no da un error — da una alarma que
nunca suena. La lista exacta de las que emite el código está en `SPEC.md` y hay
una prueba (`tests/contrato_test.py`) que falla si alguna desaparece.

La **dimensión sigue siendo `Entorno`** (en castellano). No se ha renombrado a
propósito: cambiarla obliga a tocar a la vez las alarmas y el código, y una
alarma cuya dimensión no case con la que se emite entra en el mismo silencio de
`INSUFFICIENT_DATA`. Si se quiere cambiar, es una ventana aparte.

## Variables de entorno (idénticas en las 6 funciones)

```
RAW_BUCKET, QUARANTINE_BUCKET, CLEAN_BUCKET, RESULTS_BUCKET
BATCHES_TABLE, ANTHROPIC_SECRET_ARN
ANTHROPIC_VERSION        = "2023-06-01"
ALLOWED_MODELS           = "claude-opus-4-5,claude-sonnet-4-5,claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS       = "4096"
GATE_REJECT_PCT          = "1.0"
GATE_REJECT_ABS          = "100"
MAX_REQUESTS_PER_BATCH   = "100000"
MAX_RAW_BYTES            = "100000000"
INFLIGHT_LIMIT           = "200000"
SUBMIT_MAX_PER_TICK      = "2"
FETCH_MAX_PER_TICK       = "2"
RESULTS_TTL_DAYS         = "30"
ENVIRONMENT, LOG_LEVEL   = "INFO"
```

Expón como variables de Terraform al menos: `environment`, `aws_region`,
`alarm_email`, `cost_center`, `gate_reject_pct`, `inflight_limit`, `allowed_models`
y las de memoria/timeout.

Algunas conviene que difieran por entorno:

| Variable | dev | qa | prod |
|---|---|---|---|
| `inflight_limit` | bajo (p.ej. 10.000) | bajo | 200.000 |
| `alarm_email` | canal de la plataforma | canal de QA | guardia real |
| `gate_reject_pct` | igual en los tres | igual | igual |

`gate_reject_pct` **no** debería relajarse en dev ni en qa: si el umbral es
distinto, lo que se prueba en qa no es lo que corre en prod, y el gate es
justo el control que interesa validar antes de producción.

## Etiquetado

**Usa la convención de etiquetas que ya tenga este repo.** Si no hay ninguna, aplica
estas a *todos* los recursos que las admitan:

```hcl
Project            = "intelica-proxy-ia"
Environment        = var.environment          # dev | qa | prod
ManagedBy          = "terraform"
DataClassification = "chd" | "sin-chd"        # según las tablas de arriba
PCIScope           = "true" | "false"         # true cuando DataClassification = chd
c_cost             = var.cost_center          # imputación de coste, p.ej. "Cuotas"
```

`DataClassification` y `PCIScope` no son decorativas: son lo que permite responder
*"enséñame qué recursos están en alcance PCI"* con una consulta en vez de con una
hoja de cálculo mantenida a mano.

`c_cost` sigue la convención de Intelica (clave en minúscula con prefijo `c_`). **Si
en este repo hay más etiquetas de esa familia (`c_owner`, `c_app`, `c_env`…),
añádelas todas y respeta su convención por encima de la de arriba** — las de estilo
`PascalCase` de esta lista son mi propuesta, la vuestra manda.

Conviene un `default_tags` en el provider para lo común, dejando `DataClassification`
y `PCIScope` por recurso, ya que varían.

**Aviso sobre `c_cost`:** que la etiqueta exista no hace que aparezca en Cost Explorer.
Hay que activarla como *cost allocation tag* en la consola de Billing (cuenta de
gestión de la organización) y tarda hasta 24 h en propagarse. Es un paso manual fuera
de Terraform; déjalo anotado en el README del módulo o quien mire el informe de costes
la primera semana no verá nada y pensará que el etiquetado falló.

## Permisos para los productores

Los equipos que mandan lotes (p.ej. Cuotas) necesitan un permission set de
Identity Center acotado. La política está en
[PARA-PRODUCTORES.md](PARA-PRODUCTORES.md); lo que importa del diseño es:

- **Escritura** en `raw/input/*` y `kms:GenerateDataKey` sobre CMK-raw.
- **Lectura** en `clean/status/*` y en `results/*`, con `kms:Decrypt` sólo sobre
  **CMK-clean**.
- **Nunca** lectura sobre `raw` ni `kms:Decrypt` sobre CMK-raw. Un productor que
  pueda releer lo que subió es una vía de salida de CHD del CDE, y mete su
  identidad en el alcance de la auditoría.

El productor ve su lote *sanitizado* en `clean/`, no el original. Cubre la
necesidad de "quiero ver lo que mandé" sin abrir el entorno protegido.

## Lo que NO hay que crear

Está fuera del MVP a propósito. No lo añadas aunque parezca que falta:

- VPC, subredes, NAT Gateway, Network Firewall, VPC endpoints.
- Step Functions (Distributed Map).
- Segunda cuenta AWS, SCPs.
- Amazon Macie (descartado por decisión de diseño: dispara *después* del envío y usa
  el mismo algoritmo, así que sus fallos están correlacionados con los nuestros).
- API Gateway, Function URL o cualquier endpoint HTTP. **El sistema no tiene API**:
  el productor deja el fichero en S3 y ya.

## Cómo comprobar que salió bien

Además de `terraform plan`, estas cuatro cosas son las que de verdad importan:

1. El rol submitter tiene **Deny** (no ausencia de Allow) sobre raw, quarantine y CMK-raw.
2. Los buckets raw y clean tienen la notificación a EventBridge activada.
3. Las cuatro Lambdas de horario tienen `reserved_concurrent_executions = 1`.
4. El valor del secreto de Anthropic **no** aparece en el state file.

Si algo de la traducción te obliga a elegir entre elegancia del código y mantener el
invariante de la frontera, mantén el invariante y deja un comentario explicando por qué.
