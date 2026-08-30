# Prompt para el agente de Terraform — renombrado

> Copia todo lo que hay debajo de la línea y pásaselo a tu agente en el repo de
> Terraform.
>
> **Léete el apartado «Orden» antes de aplicar nada.** El código nuevo de
> `intelica-proxy-ia` **no se puede desplegar** hasta que Terraform haya
> renombrado las funciones Lambda, y las alarmas dejan de vigilar nada en cuanto
> se despliegue el código. Los dos repos tienen que moverse juntos.
>
> **Y hay dos datos que este documento no puede darte** —el código numérico del
> proyecto y las abreviaturas de tipo de recurso—: salen de la cuenta, no de
> aquí. Están en el apartado 1, marcados. No los inventes.

---

Dos cambios de nomenclatura que van en la misma ventana porque necesitan la
misma parada:

1. **Los recursos de AWS pasan a la convención de la cuenta**
   (`itl-<NNNN>-<app>-<entorno>-<tipo>-<descriptor>-<NN>`). Hoy este proyecto
   usa un criterio propio, `intelica-proxy-ia-dev-sanitizer`, que no se parece a
   nada de lo que hay alrededor.
2. **El código pasa a nomenclatura inglesa**: identificadores y valores
   persistidos en inglés; la prosa —comentarios, documentación, mensajes de log
   y los textos que lee una persona— se queda en castellano. El repo mezclaba
   las dos cosas (92 identificadores en inglés contra 40 en castellano, y 8
   ficheros con ambas), y `SPEC.md` ya especificaba una API en inglés que el
   código no seguía.

Se hacen juntos a propósito: los dos exigen parar la ingesta y redesplegar, y
partirlos en dos ventanas duplica el riesgo sin ganar nada.

Esto arrastra seis cosas del lado de Terraform. Ninguna es opcional: si se aplica
sólo una parte, el sistema queda roto en silencio.

## 1 · Nomenclatura de recursos AWS  ·  **bloqueante**

Los recursos de este proyecto no siguen la convención de la cuenta. Hoy se
llaman `intelica-proxy-ia-dev-sanitizer`, y el criterio de la casa es el de
recursos como:

```
itl-0003-portal-dev-lambda-sync-buk-absence-03
 │    │     │     │    │           │          │
 │    │     │     │    │           │          └─ secuencia
 │    │     │     │    │           └──────────── descriptor
 │    │     │     │    └──────────────────────── tipo de recurso
 │    │     │     └───────────────────────────── entorno
 │    │     └─────────────────────────────────── aplicación
 │    └───────────────────────────────────────── código numérico
 └────────────────────────────────────────────── organización
```

Es decir: `itl-<NNNN>-<app>-<entorno>-<tipo>-<descriptor>-<NN>`.

### Los dos datos que faltaban, ya resueltos

Este documento se escribió con dos incógnitas —el código numérico del proyecto y
los tokens de tipo de cada recurso— porque salían de la cuenta y no de aquí. Ya
están resueltas y **la infraestructura está desplegada** con estos valores:

```
Cuenta:      891376942769        Asset ID:  0003
Región:      eu-south-2          Secuencia: 03
Aplicación:  proxy-ia            Entorno:   dev
```

El asset id resultó ser `0003` igualmente, y los tokens de tipo son `lambda`,
`s3`, `ddb`, `kms`, `sns`, `evb` y `role`. **`role`, no `iam`**, que es lo que
proponía la versión anterior de esta tabla.

### Nombres definitivos

| Recurso | Nombre | Tipo |
|---|---|---|
| Lambda sanitizer | `itl-0003-proxy-ia-dev-lambda-sanitizer-03` | `lambda` |
| Lambda submitter | `itl-0003-proxy-ia-dev-lambda-submitter-03` | `lambda` |
| Lambda reconciler | `itl-0003-proxy-ia-dev-lambda-reconciler-03` | `lambda` |
| Lambda fetcher | `itl-0003-proxy-ia-dev-lambda-fetcher-03` | `lambda` |
| Lambda verifier | `itl-0003-proxy-ia-dev-lambda-verifier-03` | `lambda` |
| Lambda canary | `itl-0003-proxy-ia-dev-lambda-canary-03` | `lambda` |
| Bucket raw | `itl-0003-proxy-ia-dev-s3-raw-03` | `s3` |
| Bucket clean | `itl-0003-proxy-ia-dev-s3-clean-03` | `s3` |
| Bucket quarantine | `itl-0003-proxy-ia-dev-s3-quarantine-03` | `s3` |
| Bucket results | `itl-0003-proxy-ia-dev-s3-results-03` | `s3` |
| Tabla DynamoDB | `itl-0003-proxy-ia-dev-ddb-batches-03` | `ddb` |
| Alias KMS raw | `alias/itl-0003-proxy-ia-dev-kms-raw-03` | `kms` |
| Alias KMS clean | `alias/itl-0003-proxy-ia-dev-kms-clean-03` | `kms` |
| Topic SNS | `itl-0003-proxy-ia-dev-sns-alarms-03` | `sns` |
| Roles IAM | `itl-0003-proxy-ia-dev-role-<lambda>-03` | `role` |
| Reglas EventBridge | `itl-0003-proxy-ia-dev-evb-<disparador>-03` | `evb` |
| Rol de CI | `itl-0003-proxy-ia-dev-role-ci-03` | `role` |
| Layer | `itl-0003-proxy-ia-dev-lambda-deps-03` | `lambda` |

Y los tres nombres de función que además cambian de idioma: `canario`→`canary`,
`reconciliador`→`reconciler`, `verificador`→`verifier`. `sanitizer`, `submitter`
y `fetcher` ya estaban bien.

El repo de código construye estos nombres **en un solo sitio**
(`scripts/lib/nombres.sh`) y `tests/nombres_test.py` los compara uno a uno con
esta tabla. Si Terraform y esta tabla discrepan, manda AWS — pero entonces hay
que corregir la tabla y la prueba, que es justo lo que hace visible la
discrepancia en vez de dejarla latente.

### Lo que NO se puede renombrar y hay que recrear

Esto es lo que distingue este cambio de un renombrado normal. Tres recursos de
AWS **no admiten cambio de nombre**:

| Recurso | Qué pasa realmente |
|---|---|
| **Buckets S3** | No se renombran. Hay que crear el nuevo, copiar el contenido y repuntar. En dev basta con crear los nuevos y borrar los viejos cuando el flujo funcione |
| **Tabla DynamoDB** | No se renombra. Tabla nueva — y da la casualidad de que vamos a vaciarla igualmente (apartado 4), así que aquí no se pierde nada |
| **Claves KMS** | La clave no se renombra; **el alias sí**. Renombra sólo el alias y deja la clave donde está: recrearla haría ilegible todo lo cifrado con ella |

Y dos que sí se renombran pero con consecuencias que no se ven en el plan:

- **El topic SNS.** Renombrarlo lo destruye y lo recrea, y **las suscripciones
  por correo se pierden**: cada destinatario tiene que volver a confirmar la
  suya pinchando el enlace que le llega. Hasta que lo hagan, **las alarmas
  suenan en el vacío**. Avisa a quien reciba las alarmas *antes* de aplicar, no
  después.
- **El rol de IAM de CI.** Su ARN está en la variable `AWS_ROLE_DEV` del entorno
  `dev` en GitHub, en el repo `intelica-proxy-ia`. Si se renombra el rol y no se
  actualiza esa variable, **los despliegues dejan de funcionar** con un error de
  OIDC que no menciona el nombre del rol. Actualiza la variable en el mismo
  momento.

### El caso de las alarmas: un conflicto real

Hay una tensión que conviene decidir a conciencia. El prefijo de severidad
(`CRITICO-`, `AVISO-`) existe porque **el nombre de la alarma es el asunto del
correo**, y en el móvil es lo único que se lee. La convención de la casa pone
`itl-0003-...` delante, con lo que todos los asuntos empiezan igual y la
severidad desaparece de la vista.

Tres salidas, de mejor a peor en mi opinión:

1. **Convención + severidad al final**: `itl-0003-proxy-ia-dev-cw-hard-block-critico`.
   Respeta el criterio y la severidad sigue en el asunto, aunque al final.
2. **Convención pura** y la severidad como primera línea de la descripción.
   Consistente, pero se pierde en el asunto — que es donde hacía falta.
3. **Exceptuar las alarmas** de la convención. No lo recomiendo: una excepción
   sin justificar es cómo empiezan las convenciones a morirse.

Yo iría por la 1. Decídelo tú, pero decídelo: no lo dejes al azar del orden en
que se escriban los recursos.

### Por qué esto bloquea el resto

`scripts/publish.sh` despliega por nombre exacto de función y aborta si no
existe. **Mientras el nombre en AWS no coincida con el que construye el repo de
código, el CI falla en el paso de publicación.** Y el repo de código no puede
adelantarse: hasta que no sepamos el `<NNNN>` y los tokens de tipo, no hay nada
que escribir ahí (ver «Lo que hay que cambiar en el repo de código»).

Ojo también con lo que arrastra un `name` nuevo en Terraform: **destruye y
recrea** el recurso. Revisa que en el plan aparezcan también:

- `aws_cloudwatch_log_group` de cada Lambda (los logs viejos quedan huérfanos;
  bórralos o déjalos expirar, pero no los des por migrados)
- `aws_lambda_permission` de EventBridge hacia cada función
- los `target` de las reglas de EventBridge
- las dimensiones `FunctionName` de las alarmas sobre `AWS/Lambda`
- las referencias cruzadas por ARN entre políticas IAM, buckets y claves KMS

## 2 · Prefijos de S3  ·  cambian los filtros de EventBridge

| Ahora | Pasa a ser | Quién lo usa |
|---|---|---|
| `entrada/` | `input/` | filtro de la regla que dispara el **sanitizer** sobre el bucket raw |
| `estado/` | `status/` | prefijo del parte de estado en el bucket clean |
| `canario/` | `canary/` | valor de la variable de entorno `CANARY_PREFIX` |

`clean/`, `results/` y `quarantine/` no cambian.

Dos avisos concretos:

- **La regla del manifiesto cambia de bucket, no sólo de prefijo.** El
  manifiesto pasa a publicarse en **clean** bajo `input/`, con sufijo
  `_MANIFEST.json`; las partes siguen yendo a **raw** bajo `input/`. El motivo
  es el invariante de siempre: el submitter no puede leer raw, así que un
  manifiesto en raw lo dejaría sin poder abrirlo. Si sólo cambias una de las dos
  reglas, los lotes se sanitizan pero nunca se envían, o al revés — y no hay
  error, sólo silencio.
- **Ni `status/` ni `input/` deben disparar al verifier.** El verifier escucha
  en `clean/`; el parte de estado se escribe en el mismo bucket bajo `status/` y
  ahora el manifiesto bajo `input/`, justo para no despertarlo. Si el filtro se
  amplía a todo el bucket, se pondrá a verificar partes de estado y manifiestos
  — documentos sin `requests`, que marcaría como verificados.

## 3 · Métricas de CloudWatch  ·  las alarmas dejan de ver su métrica

**Todas** las métricas cambian de nombre, y dos namespaces también. Una alarma
que apunta a una métrica que ya nadie emite **no da error: se queda en
`INSUFFICIENT_DATA` para siempre.** Es el fallo más silencioso de esta lista.

Namespaces:

| Ahora | Pasa a ser |
|---|---|
| `IntelicaProxyIA/Canario` | `IntelicaProxyIA/Canary` |
| `IntelicaProxyIA/Reconciliador` | `IntelicaProxyIA/Reconciler` |
| `IntelicaProxyIA/Verificador` | `IntelicaProxyIA/Verifier` |

`Sanitizer`, `Submitter` y `Fetcher` no cambian.

Las métricas que hoy tienen alarma:

| Ahora | Pasa a ser |
|---|---|
| `BloqueoDuro` | `HardBlock` |
| `CanarioNoBloqueado` | `CanaryNotBlocked` |
| `CanarioNoProcesado` | `CanaryNotProcessed` |
| `FalloDelSanitizer` | `SanitizerFailure` |
| `PanEnResultados` | `PanInResults` |
| `LotesEnCuarentena` | `BatchesQuarantined` |
| `LotesExpirados` | `BatchesExpired` |
| `OcupacionCola` | `QueueOccupancy` |

Y el resto, que van al panel y no despiertan a nadie:

| Ahora | Pasa a ser | | Ahora | Pasa a ser |
|---|---|---|---|---|
| `CanarioBloqueado` | `CanaryBlocked` | | `LotesEnviados` | `BatchesSubmitted` |
| `CanarioBloqueoDuro` | `CanaryHardBlock` | | `LotesEntregados` | `BatchesDelivered` |
| `CanarioEnCuarentena` | `CanaryQuarantined` | | `LotesRetenidos` | `BatchesHeld` |
| `CanarioDetenidoEnSubmitter` | `CanaryStoppedAtSubmitter` | | `LotesTerminados` | `BatchesCompleted` |
| `CanarioSinReferencia` | `CanaryWithoutBaseline` | | `LotesVerificados` | `BatchesVerified` |
| `ConsultasFallidas` | `PollsFailed` | | `ManifiestoIlegible` | `ManifestUnreadable` |
| `ConsultasRealizadas` | `PollsPerformed` | | `ManifiestoInvalido` | `ManifestInvalid` |
| `CustomIdDuplicado` | `DuplicateCustomId` | | `ManifiestosRecibidos` | `ManifestsReceived` |
| `EnviosFallidos` | `SubmitsFailed` | | `PanesEnZonaLimpia` | `PansInCleanZone` |
| `PeticionesEnVuelo` | `RequestsInFlight` | | `PorcentajeRechazo` | `RejectionRate` |
| `PeticionesEnviadas` | `RequestsSubmitted` | | `TextosConDigitosLargos` | `TextsWithLongDigits` |
| `PeticionesLimpias` | `RequestsClean` | | `TicksFrenados` | `TicksThrottled` |
| `PeticionesRechazadas` | `RequestsRejected` | | `TicksSinConsulta` | `TicksWithoutPoll` |

**Localiza cada alarma por la métrica que vigila, no por su nombre**, igual que
en el prompt de descripciones: los nombres de alarma de este repo no coinciden
con los de las métricas. Y **actualiza también los comandos `aws` que van dentro
de las `alarm_description`** — ahí hay `--filter-expression` con `"cuarentena"`,
claves `lote#...` y rutas `/estado/`, y todos cambian (apartados 4 y 5).

> El prompt `PROMPT-TERRAFORM-ALARMAS.md` de este mismo directorio nombra las
> métricas viejas. Este documento lo sustituye en ese punto; el resto de aquel
> —los textos de runbook y los prefijos `CRITICO-`/`AVISO-`— sigue vigente.

## 4 · DynamoDB  ·  el contenido de la tabla no se migra

Cambian los valores del atributo `status`, los prefijos de la clave de partición
y varios nombres de atributo:

| | Ahora | Pasa a ser |
|---|---|---|
| **clave** | `lote#<carpeta>` | `batch#<carpeta>` |
| | `parte#<key>` | `part#<key>` |
| | `__canario__` | `__canary__` |
| **status** | `recibido` `limpio` `verificado` `retenido` `enviado` | `received` `clean` `verified` `held` `submitted` |
| | `terminado` `entregado` `cuarentena` `expirado` `fallido` | `completed` `delivered` `quarantined` `expired` `failed` |
| | `esperando_partes` `listo` | `awaiting_parts` `ready` |
| **atributos** | `partes_esperadas` `partes_limpias` `partes_rechazadas` | `expected_parts` `clean_parts` `rejected_parts` |
| | `manifiesto_visto` `ficheros` `lote` | `manifest_seen` `files` `batch` |
| | `reclamado_en` `consultado_en` `enviado_en` | `claimed_at` `polled_at` `submitted_at` |
| | `ultimo_lote` `ultima_clave` `es_canario` | `last_batch` `last_key` `is_canary` |

**No hay migración, y con el cambio de nombre tampoco hace falta.** Como la tabla
se recrea con el nombre nuevo (apartado 1), nace vacía: no hay filas viejas que
convertir ni que borrar. El código nuevo escribe y lee sólo los nombres nuevos.

**El esquema no cambia**: misma clave de partición `batch_id`, mismo GSI
`status-index`. Lo que cambia son los valores que se guardan dentro.

### Lo único que hay que comprobar antes

Un lote en `enviado` o `terminado` está **en Anthropic, se va a facturar, y tras
el corte nadie recogerá su resultado**: la tabla nueva no sabrá que existe.
Contra la tabla **vieja**, antes de aplicar:

```bash
aws dynamodb scan --table-name intelica-proxy-ia-dev-batches --region eu-south-2 \
  --filter-expression "#s IN (:e, :t)" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":e":{"S":"enviado"},":t":{"S":"terminado"}}' \
  --query 'Count'
```

Si no devuelve `0`, espera a que esos lotes lleguen a `entregado` antes de
cortar. Es la única pérdida real que puede provocar esta ventana.

La tabla vieja puede quedarse unos días por si hay que consultar algo, y
borrarse después. **No la borres el mismo día**: es el único sitio donde queda
constancia de lo que pasó antes del corte.

**Para prod, esto no vale.** Con tráfico real hace falta una ventana con la
ingesta parada y todo drenado, o una migración de doble escritura que hoy no
existe. Dilo antes de aplicar en prod en vez de asumir el corte limpio.

## 5 · Política IAM de los productores

Cambian **el nombre del bucket y el prefijo a la vez**. En
`PARA-PRODUCTORES.md` el permiso de lectura del parte de estado es hoy:

```
"arn:aws:s3:::intelica-proxy-ia-<entorno>-clean-<cuenta>/estado/*"
```

y pasa a:

```
"arn:aws:s3:::itl-0003-proxy-ia-<entorno>-s3-clean-03/status/*"
```

Si no se cambia, el equipo de Cuotas deja de poder leer por qué se le rechazó un
lote — y ése es justo el momento en que lo necesita.

Lo mismo con el permiso de escritura en raw: cambia el bucket y, si está acotado
por prefijo `entrada/*`, pasa a `input/*`. Compruébalo, porque **si no se
actualiza, los productores dejan de poder subir** y el error que verán
(`AccessDenied`) no dice nada de un renombrado.

**Avisa a Cuotas antes de la ventana, no después.** Su política vive en su
propio rol; si la gestionan ellos, el cambio no está en tu plan de Terraform y
se descubre cuando falla.

## 6 · Variables de entorno de las Lambdas

| Ahora | Pasa a ser | Dónde |
|---|---|---|
| `SUBMIT_MAX_POR_TICK` | `SUBMIT_MAX_PER_TICK` | submitter |
| `FETCH_MAX_POR_TICK` | `FETCH_MAX_PER_TICK` | fetcher |
| `CANARY_PREFIX` (valor `canario/`) | valor `canary/` | canary, sanitizer |

El nombre `CANARY_PREFIX` ya estaba en inglés; lo que cambia es su **valor**.

Las demás (`RAW_BUCKET`, `CLEAN_BUCKET`, `BATCHES_TABLE`, `ANTHROPIC_SECRET_ARN`,
`GATE_REJECT_PCT`, `INFLIGHT_LIMIT`…) ya estaban en inglés y no se tocan.

Si una de las dos renombradas no se cambia, **la Lambda ya no arranca**: el
código comprueba al importar si el entorno trae un nombre retirado y revienta
nombrando el que corresponde. Antes no era así —caía al valor por defecto (`2`)
y nadie se enteraba, con el submitter enviando 2 por tick durante semanas
mientras alguien creía haber configurado 20—, y por eso ahora falla en frío: un
arranque roto se diagnostica en un minuto, un valor por defecto silencioso no se
diagnostica.

## Lo que hay que cambiar en el repo de código

Esto **no** lo haces tú, pero necesitas saberlo porque condiciona el orden y
porque el repo de código no puede adelantarse hasta que tú confirmes dos datos.

Los scripts construyen los nombres concatenando `${PROYECTO}-${ENTORNO}` y un
sufijo (`scripts/publish.sh`, `probar-flujo.sh`, `probar-fallos.sh`,
`ciclo-manual.sh`, `subir-lote.sh`). La convención nueva mete el entorno **en
medio** y añade un token de tipo, así que no es un cambio de prefijo: hay que
sustituir esa concatenación por una función de nombres.

**Lo que necesita el repo de código, de ti:**

1. El código numérico `<NNNN>` asignado a proxy-ia.
2. Los tokens de tipo definitivos (`s3` o `buk`, `ddb` o `dynamodb`, etc.).
3. Confirmación de si el sufijo de secuencia es siempre `01` o si hay recursos
   con varias instancias.

En cuanto tengas esos tres datos, dilo en el hilo del repo de código: el cambio
allí es de media hora y hasta entonces no hay nada que escribir, porque
adivinarlos produciría scripts que fallan en tiempo de ejecución contra nombres
que no existen.

## Orden

Hay una ventana inevitable en la que el sistema no procesa. El orden minimiza su
duración y evita el estado peor —código nuevo contra infraestructura vieja—, que
sí produce daños silenciosos.

**Antes del día, sin prisa:**

0. Consigue el `<NNNN>`, confirma los tokens de tipo, decide lo de las alarmas
   (apartado 1), pásale los tres datos al repo de código y **avisa a dos sitios**:
   a quien recibe las alarmas (tendrá que reconfirmar su suscripción al SNS) y al
   equipo de Cuotas (les cambia la política de IAM y el nombre de los buckets).

**El día:**

1. **Parar la ingesta.** Desactiva la regla de EventBridge del bucket raw y pide
   a Cuotas que no suban durante la ventana.
2. **Esperar a que se vacíe lo que está en vuelo** — la consulta del apartado 4
   debe devolver `0`. Es el único punto donde se puede perder trabajo de verdad.
3. **Limpiar lo que haría fallar el apply.** Desde el repo de código:

   ```bash
   ./scripts/limpiar-recursos-viejos.sh dev            # lista, no toca nada
   ./scripts/limpiar-recursos-viejos.sh dev --borrar   # ejecuta
   ```

   Vacía los buckets, desactiva la protección de borrado de la tabla y quita los
   log groups huérfanos. **Sin esto el `terraform apply` revienta a mitad** y
   deja el sistema medio renombrado, que es el peor estado posible. El script
   repite por su cuenta la comprobación del paso 2 y se niega a seguir si algo
   sigue en vuelo. Ver «Por qué hace falta un paso manual» más abajo.
4. **Terraform**: recursos con la nomenclatura nueva, prefijos, métricas,
   namespaces, IAM y variables de entorno. Todo en un solo apply.
5. **Actualizar `AWS_ROLE_DEV`** en las variables del entorno `dev` de GitHub con
   el ARN nuevo del rol de CI. Si no, el paso 6 falla con un error de OIDC que ni
   siquiera menciona el rol.
6. **Desplegar el código** desde `intelica-proxy-ia` (merge a `main`), ya con los
   nombres nuevos en los scripts.
7. **Reconfirmar las suscripciones del SNS.** Cada destinatario tiene que pinchar
   su enlace. Hasta entonces las alarmas suenan en el vacío.
8. **Reactivar la ingesta** y comprobar (abajo).

**Después, cuando el flujo lleve unos días bien:**

9. Borrar la tabla vieja, si Terraform no lo hizo ya. **No el mismo día**: es el
   único sitio donde queda constancia de lo que pasó antes del corte. Los
   buckets ya los vació el paso 3.

Los pasos 4 y 6 no se pueden invertir: `publish.sh` falla si las funciones aún
tienen el nombre viejo. Y no dejes el 6 para otro día — entre el 4 y el 6 las
Lambdas viejas siguen vivas escribiendo en recursos que ya nadie lee.

### Por qué hace falta un paso manual antes del apply

Terraform destruye y recrea sin problema las Lambdas, los roles, las reglas, las
alarmas, el topic y el alias de KMS. Pero hay tres cosas que **no puede** y que
hacen fallar el apply a mitad:

| Qué | Por qué falla |
|---|---|
| Bucket con objetos | `force_destroy` viene a `false`. Y con versionado, un `s3 rm --recursive` **no basta**: deja vivas las versiones y las marcas de borrado, y el bucket sigue sin poder borrarse |
| Tabla con `deletion_protection` | El destroy se rechaza sin más |
| Log group que creó AWS solo | No está en el estado. Al crear el nuevo, Terraform choca con `ResourceAlreadyExistsException` |

Un apply que falla en la mitad de eso deja la mitad de las Lambdas apuntando a
buckets que ya no existen. **Es peor que no haber empezado**, y hacia atrás no
hay botón. De ahí el paso 3.

El script tiene además dos cosas que conviene saber: por defecto **sólo lista**
—hay que pasar `--borrar` y teclear el nombre del entorno—, y **nunca toca las
claves KMS, el secreto de Anthropic ni el rol de CI**. Las claves porque
destruirlas dejaría ilegible todo lo cifrado con ellas y su borrado tiene una
espera irreversible de 7 a 30 días; el rol de CI porque puede ser el que estés
usando en ese momento.

## Cómo comprobar que quedó bien

Con la ingesta reactivada, sube un lote de prueba desde el repo de código:

```bash
./scripts/subir-lote.sh dev ejemplos/lote-multiparte lote-renombrado-01
```

Sustituye `<N>` por el código numérico y verifica las cinco cosas que este
cambio podía romper:

```bash
# 1. La clave nueva existe y el status esta en ingles
aws dynamodb get-item --table-name itl-0003-proxy-ia-dev-ddb-batches-03 \
  --region eu-south-2 \
  --key '{"batch_id":{"S":"batch#input/lote-renombrado-01"}}'

# 2. El parte de estado se escribe bajo status/ en el bucket nuevo
aws s3 ls s3://itl-0003-proxy-ia-dev-s3-clean-03/status/

# 3. Las metricas nuevas se estan emitiendo
aws cloudwatch list-metrics --namespace IntelicaProxyIA/Sanitizer \
  --region eu-south-2 --query 'Metrics[].MetricName' --output text

# 4. Ninguna alarma se quedo ciega
aws cloudwatch describe-alarms --alarm-name-prefix itl-0003-proxy-ia \
  --region eu-south-2 --state-value INSUFFICIENT_DATA \
  --query 'MetricAlarms[].[AlarmName,MetricName]' --output table

# 5. El SNS tiene suscripciones CONFIRMADAS, no pendientes
aws sns list-subscriptions-by-topic --region eu-south-2 \
  --topic-arn arn:aws:sns:eu-south-2:<CUENTA>:itl-0003-proxy-ia-dev-sns-alarms-03 \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
```

Las dos últimas son las que importan, porque las dos fallan **en silencio**:

- Una alarma en `INSUFFICIENT_DATA` justo tras el despliegue es normal en las que
  dependen del canario, que sólo corre una vez al día. **Cualquier otra que siga
  ahí al día siguiente apunta a una métrica que ya no existe**, y eso deja el
  sistema sin vigilancia sin que nadie lo note.
- Una suscripción cuyo `SubscriptionArn` diga `PendingConfirmation` **no recibe
  nada**. El topic parece bien configurado y los correos sencillamente no llegan.

## Lo que NO cambia

- La frontera CDE / zona limpia. Ningún rol gana ni pierde acceso, y el `Deny`
  del rol del submitter sobre `raw`, `quarantine` y la CMK de raw **se queda
  exactamente como está**.
- **Las claves KMS.** Cambia su alias, no la clave: recrearla dejaría ilegible
  todo lo que se cifró con ella. El secreto de Anthropic tampoco se toca.
- La arquitectura: mismas seis Lambdas, mismos cuatro buckets, misma tabla y
  mismo GSI `status-index`. Esto es un renombrado, no un rediseño.
- Los umbrales, períodos y acciones de las alarmas. Sólo cambia a qué métrica
  apuntan.
- El esquema de la tabla y el GSI `status-index`.
- El nombre del proyecto y las etiquetas (`c_cost: Cuotas` incluida).
