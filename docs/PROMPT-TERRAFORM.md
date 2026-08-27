# Prompt para el agente de Terraform

> Copia todo lo que hay debajo de la línea y pásaselo a tu agente en el repo de
> Terraform. Está escrito para que pueda trabajar sin ver el repo de la aplicación.

---

Necesito que traduzcas a Terraform la infraestructura de **`intelica-proxy-ia`**, un
proxy de sanitización PCI hacia la Message Batches API de Anthropic. Ya existe un
script `deploy/deploy.sh` que crea todo con AWS CLI; tu trabajo es reproducir esos
mismos recursos como código en este repo.

**Antes de escribir nada, mira cómo está organizado este repositorio** (estructura de
módulos, backend de estado, convención de nombres, `locals` de etiquetas, versión del
provider) y ajústate a ello. Lo que sigue describe *qué* recursos hacen falta y con
*qué* configuración, no cómo organizarlos aquí.

## El invariante que no se puede romper

Todo el diseño se sostiene sobre una sola propiedad:

> **Ningún rol IAM tiene a la vez acceso a datos de tarjeta (CHD) y salida a internet.**

Si al refactorizar algo se pierde eso, la infraestructura deja de servir para lo que
se hizo. Hay dos zonas:

| Zona | Buckets | Clave KMS | Roles |
|---|---|---|---|
| **CDE** (datos de tarjeta) | `raw`, `quarantine` | CMK-raw | sanitizer, verificador, canario |
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
| `alias/${prefijo}-cmk-raw` | cifra buckets raw y quarantine | `chd` |
| `alias/${prefijo}-cmk-clean` | cifra buckets clean y results | `sin-chd` |

Política de clave: la de por defecto (root de la cuenta), para que las políticas IAM
funcionen y no haya riesgo de dejar la clave inutilizable.

### 2. S3 — 4 buckets

| Bucket | Clave | Retención | Clasificación |
|---|---|---|---|
| `${prefijo}-raw-${account_id}` | CMK-raw | 7 días | `chd` |
| `${prefijo}-quarantine-${account_id}` | CMK-raw | 90 días (prefijo `quarantine/`) | `chd` |
| `${prefijo}-clean-${account_id}` | CMK-clean | 30 días (prefijo `clean/`) | `sin-chd` |
| `${prefijo}-results-${account_id}` | CMK-clean | 30 días (prefijo `results/`) | `sin-chd` |

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
verificador no se disparan nunca — y el fallo es silencioso.

### 3. DynamoDB — 1 tabla

- Nombre: `${prefijo}-batches`, `PAY_PER_REQUEST`.
- Clave de partición: `batch_id` (String).
- GSI **`status-index`**: hash `status` (String), range `created_at` (String),
  proyección `ALL`.
- TTL activado sobre el atributo **`ttl`**.
- Point-in-time recovery activado.
- Cifrado con la clave gestionada por AWS (aquí **no** hace falta CMK: la tabla sólo
  guarda estados, contadores y punteros, nunca CHD).

### 4. Secrets Manager — 1 secreto

- Nombre: `${prefijo}/anthropic-api-key`.
- Contenido: JSON `{"api_key": "..."}`.
- **El valor no debe estar en Terraform.** Crea el recurso del secreto pero deja la
  versión fuera del estado (`ignore_changes` sobre el valor, o créalo vacío y que se
  rellene por fuera). Una API key en el state file es una fuga.

### 5. SNS — 1 topic + suscripción de correo

`${prefijo}-alarmas`, con suscripción `email` a una variable `alarm_email`.

### 6. IAM — 4 roles

Todos con `AWSLambdaBasicExecutionRole` más una política inline. `trust policy` para
`lambda.amazonaws.com`.

**`${prefijo}-rol-sanitizer`** (clasificación `chd`):
- Allow `s3:GetObject`, `s3:ListBucket` sobre bucket raw y su contenido.
- Allow `s3:PutObject` sobre `quarantine/*` y sobre `clean/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` sobre **ambas** claves.
- Allow DynamoDB `PutItem`, `GetItem`, `UpdateItem`, `Query` sobre la tabla y sus índices.
- Allow `cloudwatch:PutMetricData` (`Resource: "*"`).
- **Deny `secretsmanager:GetSecretValue` sobre `*`.**

**`${prefijo}-rol-verificador`** (clasificación `chd`):
- Allow `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket` sobre clean.
  (El `DeleteObject` es intencionado: si encuentra un PAN en la zona limpia, lo retira.)
- Allow `s3:PutObject` sobre `quarantine/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` sobre ambas claves.
- Allow DynamoDB (igual que arriba) y `cloudwatch:PutMetricData`.
- **Deny `secretsmanager:GetSecretValue` sobre `*`.**

**`${prefijo}-rol-submitter`** (clasificación `sin-chd`) — lo usan submitter,
reconciliador y fetcher:
- Allow `s3:GetObject`, `s3:ListBucket` sobre clean.
- Allow `s3:PutObject`, `s3:AbortMultipartUpload` sobre `results/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` **sólo sobre CMK-clean**.
- Allow `secretsmanager:GetSecretValue` sobre el secreto de Anthropic.
- Allow DynamoDB y `cloudwatch:PutMetricData`.
- **Deny `s3:*` sobre los buckets raw y quarantine (bucket y contenido).**
- **Deny `kms:*` sobre CMK-raw.**

**`${prefijo}-rol-canario`** (clasificación `chd`):
- Allow `s3:PutObject`, `s3:GetObject` sobre `raw/*`.
- Allow `kms:Decrypt`, `kms:GenerateDataKey` sobre CMK-raw.
- Allow DynamoDB y `cloudwatch:PutMetricData`.
- **Deny `secretsmanager:GetSecretValue` sobre `*`.**

### 7. Lambda — 1 layer + 6 funciones

Layer `${prefijo}-deps`: `anthropic==0.69.0` compilado para `manylinux2014_aarch64` y
Python 3.13. `boto3` **no** va en el layer (ya está en el runtime).

Todas las funciones usan handler **`handler.lambda_handler`**, X-Ray activo
(`tracing_config = Active`) y las mismas variables de entorno (ver abajo).

| Función | Rol | Memoria | Timeout | `/tmp` | Clasif. |
|---|---|---|---|---|---|
| `${prefijo}-sanitizer` | rol-sanitizer | 3008 | 600 s | 512 | `chd` |
| `${prefijo}-verificador` | rol-verificador | 2048 | 600 s | 512 | `chd` |
| `${prefijo}-canario` | rol-canario | 512 | 120 s | 512 | `chd` |
| `${prefijo}-submitter` | rol-submitter | 1024 | 300 s | 512 | `sin-chd` |
| `${prefijo}-reconciliador` | rol-submitter | 512 | 120 s | 512 | `sin-chd` |
| `${prefijo}-fetcher` | rol-submitter | 2048 | 900 s | **4096** | `sin-chd` |

**Concurrencia reservada = 1** en submitter, reconciliador, fetcher y canario. Dos
ejecuciones simultáneas se pisarían el contador de la cola en vuelo en DynamoDB y el
presupuesto de peticiones de Anthropic. No es una optimización, es corrección.

La memoria del sanitizer tampoco es arbitraria: en el MVP procesa el lote entero en
memoria, sin Step Functions Distributed Map.

**Empaquetado.** Cada función se arma con `src/common/` más su propia carpeta, todo
en plano en la raíz del zip (no en subdirectorios). Dos funciones necesitan carpetas
extra:
- `verificador` = `common` + `sanitizer` + `verificador`
- `fetcher` = `common` + `sanitizer` + `fetcher`

(porque reutilizan los detectores). El resto es `common` + su carpeta.

Grupos de log `/aws/lambda/<función>` con retención de **90 días**, creados
explícitamente para que Terraform los gestione y no aparezcan sin retención.

### 8. EventBridge — 6 reglas

Dos por evento de S3:

| Regla | Patrón |
|---|---|
| `${prefijo}-raw-creado` | `source: aws.s3`, `detail-type: Object Created`, bucket = raw. **Sin filtro de prefijo** (el canario escribe en `canario/` y también debe dispararlo). |
| `${prefijo}-clean-creado` | ídem con bucket = clean y `object.key` con prefijo `clean/` |

Cuatro por horario:

| Regla | Expresión |
|---|---|
| `${prefijo}-submitter` | `rate(5 minutes)` |
| `${prefijo}-reconciliador` | `rate(5 minutes)` |
| `${prefijo}-fetcher` | `rate(5 minutes)` |
| `${prefijo}-canario` | `rate(1 hour)` |

Cada una con su *target* a la Lambda y su `aws_lambda_permission` para
`events.amazonaws.com` acotado al ARN de la regla.

### 9. CloudWatch — 8 alarmas

Todas: `statistic = Sum`, `period = 300`, `evaluation_periods = 1`,
`comparison_operator = GreaterThanOrEqualToThreshold`,
`treat_missing_data = notBreaching`, acción → el topic SNS,
dimensión `Entorno = ${environment}`.

| Alarma | Namespace | Métrica | Umbral |
|---|---|---|---|
| `canario-no-bloqueado` | `IntelicaProxyIA/Canario` | `CanarioNoBloqueado` | 1 |
| `canario-no-procesado` | `IntelicaProxyIA/Canario` | `CanarioNoProcesado` | 1 |
| `fallo-del-sanitizer` | `IntelicaProxyIA/Verificador` | `FalloDelSanitizer` | 1 |
| `pan-en-resultados` | `IntelicaProxyIA/Fetcher` | `PanEnResultados` | 1 |
| `bloqueo-duro` | `IntelicaProxyIA/Sanitizer` | `BloqueoDuro` | 1 |
| `lotes-en-cuarentena` | `IntelicaProxyIA/Sanitizer` | `LotesEnCuarentena` | 3 |
| `lotes-expirados` | `IntelicaProxyIA/Reconciliador` | `LotesExpirados` | 1 |

Las cuatro primeras son **críticas**: significan que un control de seguridad falló.

Y una distinta: `cola-casi-llena`, namespace `IntelicaProxyIA/Submitter`, métrica
`OcupacionCola`, `statistic = Maximum`, `evaluation_periods = 2`, umbral **80**,
operador `GreaterThanThreshold`.

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
SUBMIT_MAX_POR_TICK      = "2"
FETCH_MAX_POR_TICK       = "2"
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

- **Escritura** en `raw/entrada/*` y `kms:GenerateDataKey` sobre CMK-raw.
- **Lectura** en `clean/estado/*` y en `results/*`, con `kms:Decrypt` sólo sobre
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
