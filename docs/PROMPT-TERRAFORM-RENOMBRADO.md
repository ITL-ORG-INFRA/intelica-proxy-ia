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

### Dos cosas que hay que confirmar antes de escribir nada

De ese ejemplo sólo se deduce con certeza el token de tipo `lambda`. Lo demás
hay que sacarlo de la cuenta, no inventarlo:

1. **El código numérico `<NNNN>` de este proyecto.** El `0003` del ejemplo es de
   `portal`. Averigua cuál corresponde a proxy-ia, o si hay que pedir uno nuevo,
   antes de aplicar. **No reutilices el 0003.**
2. **Los tokens de tipo de los demás recursos.** Abajo van los que parecen
   naturales, pero contrástalos con lo que ya existe en la cuenta:

   ```bash
   aws resourcegroupstaggingapi get-resources --region eu-south-2 \
     --query 'ResourceTagMappingList[].ResourceARN' --output text \
     | grep -o 'itl-[0-9]*-[a-z-]*' | sort -u | head -40
   ```

   Si en la cuenta los buckets se llaman `-s3-` úsalo; si se llaman `-buk-`,
   ése. **El criterio es lo que ya hay, no lo que propone este documento.**

### Nombres propuestos

Con `<N>` = el código numérico y `proxy-ia` como aplicación. Los tokens de tipo
marcados con `?` son los que hay que confirmar.

| Recurso | Nombre propuesto | Tipo |
|---|---|---|
| Lambda sanitizer | `itl-<N>-proxy-ia-dev-lambda-sanitizer-01` | confirmado |
| Lambda submitter | `itl-<N>-proxy-ia-dev-lambda-submitter-01` | confirmado |
| Lambda reconciler | `itl-<N>-proxy-ia-dev-lambda-reconciler-01` | confirmado |
| Lambda fetcher | `itl-<N>-proxy-ia-dev-lambda-fetcher-01` | confirmado |
| Lambda verifier | `itl-<N>-proxy-ia-dev-lambda-verifier-01` | confirmado |
| Lambda canary | `itl-<N>-proxy-ia-dev-lambda-canary-01` | confirmado |
| Bucket raw | `itl-<N>-proxy-ia-dev-s3-raw-01` | `s3` ? |
| Bucket clean | `itl-<N>-proxy-ia-dev-s3-clean-01` | `s3` ? |
| Bucket quarantine | `itl-<N>-proxy-ia-dev-s3-quarantine-01` | `s3` ? |
| Bucket results | `itl-<N>-proxy-ia-dev-s3-results-01` | `s3` ? |
| Tabla DynamoDB | `itl-<N>-proxy-ia-dev-ddb-batches-01` | `ddb` ? |
| Alias KMS raw | `alias/itl-<N>-proxy-ia-dev-kms-raw-01` | `kms` ? |
| Alias KMS clean | `alias/itl-<N>-proxy-ia-dev-kms-clean-01` | `kms` ? |
| Topic SNS | `itl-<N>-proxy-ia-dev-sns-alarms-01` | `sns` ? |
| Roles IAM | `itl-<N>-proxy-ia-dev-iam-<lambda>-01` | `iam` ? |
| Reglas EventBridge | `itl-<N>-proxy-ia-dev-evb-<disparador>-01` | `evb` ? |

Y los tres nombres de función que además cambian de idioma: `canario`→`canary`,
`reconciliador`→`reconciler`, `verificador`→`verifier`. `sanitizer`, `submitter`
y `fetcher` ya estaban bien.

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
`itl-<N>-...` delante, con lo que todos los asuntos empiezan igual y la
severidad desaparece de la vista.

Tres salidas, de mejor a peor en mi opinión:

1. **Convención + severidad al final**: `itl-<N>-proxy-ia-dev-cw-hard-block-critico`.
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

- **La regla del manifiesto** filtra por prefijo `entrada/` y sufijo
  `_MANIFEST.json`. Si sólo cambias una de las dos reglas del bucket raw, los
  lotes se sanitizan pero nunca se envían, o al revés — y no hay error, sólo
  silencio.
- **El prefijo `status/` no debe disparar al verificador.** El verificador
  escucha en `clean/`; el parte de estado se escribe en el mismo bucket bajo
  `status/` justo para no despertarlo. Si el filtro del verificador se amplía a
  todo el bucket, se pondrá a verificar partes de estado.

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
"arn:aws:s3:::itl-<N>-proxy-ia-<entorno>-s3-clean-01/status/*"
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

Si una de las dos renombradas no se cambia, la Lambda **no falla**: cae al valor
por defecto del código (`2`) y nadie se entera. Revísalas explícitamente.

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
3. **Terraform**: recursos con la nomenclatura nueva, prefijos, métricas,
   namespaces, IAM y variables de entorno. Todo en un solo apply.
4. **Actualizar `AWS_ROLE_DEV`** en las variables del entorno `dev` de GitHub con
   el ARN nuevo del rol de CI. Si no, el paso 5 falla con un error de OIDC que ni
   siquiera menciona el rol.
5. **Desplegar el código** desde `intelica-proxy-ia` (merge a `main`), ya con los
   nombres nuevos en los scripts.
6. **Reconfirmar las suscripciones del SNS.** Cada destinatario tiene que pinchar
   su enlace. Hasta entonces las alarmas suenan en el vacío.
7. **Reactivar la ingesta** y comprobar (abajo).

**Después, cuando el flujo lleve unos días bien:**

8. Borrar los buckets y la tabla viejos. **No el mismo día**: son el único sitio
   donde queda constancia de lo que pasó antes del corte.

Los pasos 3 y 5 no se pueden invertir: `publish.sh` falla si las funciones aún
tienen el nombre viejo. Y no dejes el 5 para otro día — entre el 3 y el 5 las
Lambdas viejas siguen vivas escribiendo en recursos que ya nadie lee.

## Cómo comprobar que quedó bien

Con la ingesta reactivada, sube un lote de prueba desde el repo de código:

```bash
./scripts/subir-lote.sh dev ejemplos/lote-multiparte lote-renombrado-01
```

Sustituye `<N>` por el código numérico y verifica las cinco cosas que este
cambio podía romper:

```bash
# 1. La clave nueva existe y el status esta en ingles
aws dynamodb get-item --table-name itl-<N>-proxy-ia-dev-ddb-batches-01 \
  --region eu-south-2 \
  --key '{"batch_id":{"S":"batch#input/lote-renombrado-01"}}'

# 2. El parte de estado se escribe bajo status/ en el bucket nuevo
aws s3 ls s3://itl-<N>-proxy-ia-dev-s3-clean-01/status/

# 3. Las metricas nuevas se estan emitiendo
aws cloudwatch list-metrics --namespace IntelicaProxyIA/Sanitizer \
  --region eu-south-2 --query 'Metrics[].MetricName' --output text

# 4. Ninguna alarma se quedo ciega
aws cloudwatch describe-alarms --alarm-name-prefix itl-<N>-proxy-ia \
  --region eu-south-2 --state-value INSUFFICIENT_DATA \
  --query 'MetricAlarms[].[AlarmName,MetricName]' --output table

# 5. El SNS tiene suscripciones CONFIRMADAS, no pendientes
aws sns list-subscriptions-by-topic --region eu-south-2 \
  --topic-arn arn:aws:sns:eu-south-2:<CUENTA>:itl-<N>-proxy-ia-dev-sns-alarms-01 \
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
