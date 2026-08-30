# Prompt para el agente de Terraform — renombrado a inglés

> Copia todo lo que hay debajo de la línea y pásaselo a tu agente en el repo de
> Terraform.
>
> **Léete el apartado «Orden» antes de aplicar nada.** El código nuevo de
> `intelica-proxy-ia` **no se puede desplegar** hasta que Terraform haya
> renombrado las funciones Lambda, y las alarmas dejan de vigilar nada en cuanto
> se despliegue el código. Los dos repos tienen que moverse juntos.

---

Se ha unificado la nomenclatura de **`intelica-proxy-ia`**: los identificadores
de código y los valores persistidos pasan a inglés, y la prosa —comentarios,
documentación, mensajes de log y los textos que lee una persona— se queda en
castellano. El repo mezclaba las dos cosas (92 identificadores en inglés contra
40 en castellano, y 8 ficheros con ambas), y `SPEC.md` ya especificaba una API en
inglés que el código no seguía.

Esto arrastra seis cosas del lado de Terraform. Ninguna es opcional: si se aplica
sólo una parte, el sistema queda roto en silencio.

## 1 · Nombres de función Lambda  ·  **bloqueante**

Tres funciones cambian de nombre:

| Ahora | Pasa a ser |
|---|---|
| `${prefijo}-canario` | `${prefijo}-canary` |
| `${prefijo}-reconciliador` | `${prefijo}-reconciler` |
| `${prefijo}-verificador` | `${prefijo}-verifier` |

`sanitizer`, `submitter` y `fetcher` no cambian.

**Esto es lo que bloquea todo lo demás.** `scripts/publish.sh` despliega por
nombre exacto (`aws lambda update-function-code --function-name ${prefijo}-canary`)
y aborta si la función no existe. Hasta que Terraform no aplique este renombrado,
el pipeline de CI del repo de código falla en el paso de publicación.

Ojo con lo que arrastra un `name` nuevo en Terraform: **destruye y recrea** la
función, y con ella se van el grupo de logs, los permisos de invocación y los
targets de EventBridge que la apunten. Revisa que en el plan aparezcan también:

- `aws_cloudwatch_log_group` de cada una (los logs viejos se quedan huérfanos;
  bórralos o déjalos expirar, pero no los des por migrados)
- `aws_lambda_permission` de EventBridge hacia cada función
- los `target` de las reglas de EventBridge
- las dimensiones `FunctionName` de cualquier alarma sobre `AWS/Lambda`

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

## 4 · DynamoDB  ·  hay que vaciar la tabla

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

**No se ha escrito ninguna migración: el corte es limpio.** El código nuevo
escribe y lee sólo los nombres nuevos, así que las filas viejas quedan invisibles
—no rompen nada, simplemente no las ve nadie— y los lotes que estuvieran en vuelo
se quedarían huérfanos.

**En dev, vacía la tabla antes de desplegar.** Es lo más simple y no se pierde
nada que importe:

```bash
aws dynamodb scan --table-name intelica-proxy-ia-dev-batches \
  --region eu-south-2 --projection-expression batch_id --output json \
| jq -r '.Items[].batch_id.S' \
| while read -r id; do
    aws dynamodb delete-item --table-name intelica-proxy-ia-dev-batches \
      --region eu-south-2 --key "{\"batch_id\":{\"S\":\"$id\"}}"
  done
```

**Antes de vaciar, comprueba que no hay nada en vuelo**, porque un lote en
`enviado` está en Anthropic, se va a facturar, y borrar su fila significa que
nadie recogerá el resultado:

```bash
aws dynamodb scan --table-name intelica-proxy-ia-dev-batches --region eu-south-2 \
  --filter-expression "#s IN (:e, :t)" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":e":{"S":"enviado"},":t":{"S":"terminado"}}' \
  --query 'Count'
```

Si devuelve algo distinto de `0`, espera a que esos lotes lleguen a `entregado`
antes de cortar.

**El GSI `status-index` no cambia**: cambian los valores del atributo, no su
nombre ni el esquema de la tabla.

**Para prod, esto no vale.** Si hay tráfico real, hace falta una ventana con la
ingesta parada y la tabla drenada, o una migración de doble lectura que hoy no
existe. Dilo antes de aplicar en prod en vez de asumir el corte limpio.

## 5 · Política IAM de los productores

En `PARA-PRODUCTORES.md` se documenta el permiso de lectura del parte de estado:

```
"arn:aws:s3:::intelica-proxy-ia-<entorno>-clean-<cuenta>/estado/*"
```

Pasa a `/status/*`. Si no se cambia, el equipo de Cuotas deja de poder leer por
qué se le rechazó un lote — y ése es justo el momento en que lo necesita.

El permiso de escritura en raw, si está acotado por prefijo `entrada/*`, pasa a
`input/*`. Compruébalo: si lo está y no se cambia, **los productores dejan de
poder subir**.

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

## Orden

Hay una ventana inevitable en la que el sistema no procesa. El orden minimiza su
duración y evita el estado peor —código nuevo contra infraestructura vieja—, que
sí produce daños silenciosos.

1. **Parar la ingesta.** Desactiva la regla de EventBridge del bucket raw y avisa
   a Cuotas de que no suban durante la ventana.
2. **Esperar a que se vacíe lo que está en vuelo** (la consulta del apartado 4
   debe devolver `0`).
3. **Vaciar la tabla de DynamoDB** en dev.
4. **Terraform**: funciones Lambda, prefijos, métricas de las alarmas,
   namespaces, IAM y variables de entorno. Todo junto, en un solo apply.
5. **Desplegar el código** desde `intelica-proxy-ia` (merge a `main`).
6. **Reactivar la ingesta** y comprobar (abajo).

Los pasos 4 y 5 no se pueden invertir: `publish.sh` falla si las funciones aún se
llaman `-canario`. Y no dejes el paso 5 para más tarde: entre el 4 y el 5 las
Lambdas viejas escriben valores viejos en una tabla ya vaciada.

## Cómo comprobar que quedó bien

Con la ingesta ya reactivada, sube un lote de prueba desde el repo de código:

```bash
./scripts/subir-lote.sh dev ejemplos/lote-multiparte lote-renombrado-01
```

Y verifica las cuatro cosas que este cambio podía romper:

```bash
# 1. La clave nueva existe y el status esta en ingles
aws dynamodb get-item --table-name intelica-proxy-ia-dev-batches \
  --region eu-south-2 \
  --key '{"batch_id":{"S":"batch#input/lote-renombrado-01"}}'

# 2. El parte de estado se escribe bajo status/
aws s3 ls s3://intelica-proxy-ia-dev-clean-<CUENTA>/status/

# 3. Las metricas nuevas se estan emitiendo
aws cloudwatch list-metrics --namespace IntelicaProxyIA/Sanitizer \
  --region eu-south-2 --query 'Metrics[].MetricName' --output text

# 4. Ninguna alarma se quedo ciega
aws cloudwatch describe-alarms --alarm-name-prefix intelica-proxy-ia \
  --region eu-south-2 --state-value INSUFFICIENT_DATA \
  --query 'MetricAlarms[].[AlarmName,MetricName]' --output table
```

La cuarta es la importante. Una alarma en `INSUFFICIENT_DATA` un rato después del
despliegue es normal en las que dependen del canario —que sólo corre una vez al
día—, pero **cualquier otra que siga ahí al día siguiente está apuntando a una
métrica que ya no existe**. Ésa es la forma en que este cambio puede dejar el
sistema sin vigilancia sin que nadie lo note.

## Lo que NO cambia

- La frontera CDE / zona limpia. Ningún rol gana ni pierde acceso, y el `Deny`
  del rol del submitter sobre `raw`, `quarantine` y la CMK de raw **se queda
  exactamente como está**.
- Los buckets, la tabla, las CMK, el topic SNS y el secreto de Anthropic:
  mismos nombres, mismos ARNs.
- Los umbrales, períodos y acciones de las alarmas. Sólo cambia a qué métrica
  apuntan.
- El esquema de la tabla y el GSI `status-index`.
- El nombre del proyecto y las etiquetas (`c_cost: Cuotas` incluida).
