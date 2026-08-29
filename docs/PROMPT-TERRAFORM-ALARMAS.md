# Prompt para el agente de Terraform — alarmas accionables

> Copia todo lo que hay debajo de la línea y pásaselo a tu agente en el repo de
> Terraform.

---

Necesito reescribir las alarmas de **`intelica-proxy-ia`**. Dos problemas:

1. **La alarma `hard-block` suena por el canario.** El canario planta datos de
   banda magnética a propósito para comprobar que el filtro los bloquea. Que los
   bloquee es la buena noticia — pero compartía métrica con los productores
   reales, así que la alarma salta cada vez que el control funciona. El código ya
   está corregido: ahora emite métricas distintas.

2. **El correo no dice nada accionable.** El formato de CloudWatch sólo muestra
   *"la métrica X pasó de 1"*. A las 4 de la mañana eso no dice qué lote, qué
   productor, ni qué hacer. Lo único que se puede controlar es la
   `alarm_description`, así que ahí va el runbook.

## 1 · Métricas nuevas que NO deben alarmar

El sanitizer emite ahora dos métricas nuevas en `IntelicaProxyIA/Sanitizer`:

| Métrica | Qué significa |
|---|---|
| `CanarioBloqueoDuro` | El canario plantó SAD y el filtro lo bloqueó. **Es un éxito** |
| `CanarioEnCuarentena` | El lote del canario no cruzó. **También es un éxito** |

**No crees alarmas sobre estas dos.** Son para el panel, no para despertar a
nadie. La alarma que sí importa sobre el canario ya existe:
`canario-no-bloqueado`, que salta cuando el canario **no** fue bloqueado.

Y comprueba que las alarmas existentes `hard-block` (o como se llame la de
`BloqueoDuro`) y `lotes-en-cuarentena` siguen apuntando a `BloqueoDuro` y
`LotesEnCuarentena`. Esas métricas ahora sólo las emiten productores reales.

## 2 · Descripciones nuevas

**Localiza cada alarma por la MÉTRICA que vigila, no por su nombre.** Los nombres
que uses en este repo pueden no coincidir con los de abajo — la que vigila
`BloqueoDuro` se llama hoy `hard-block`, por ejemplo. Si creas alarmas nuevas en
vez de actualizar las existentes, acabaremos con las siete duplicadas y sonando
por partida doble.

Para ver qué hay desplegado y con qué nombre:

```bash
aws cloudwatch describe-alarms --alarm-name-prefix intelica-proxy-ia \
  --region eu-south-2 \
  --query 'MetricAlarms[].[AlarmName,MetricName,Threshold]' --output table
```

Sustituye la `alarm_description` de cada una por el texto que corresponde a su
métrica. Van en español porque quien las recibe trabaja en español, e incluyen el
comando a ejecutar porque a las 4 de la mañana nadie recuerda la sintaxis de
`aws dynamodb`.

Sustituye `<CUENTA>` por el id de cuenta y `<ENTORNO>` por dev/qa/prod.

### La que vigila `BloqueoDuro`  ·  hoy se llama `hard-block`

```
CRITICO. Un productor mando datos de banda magnetica, CVV o PIN en un lote.
El lote NO se envio a Anthropic y esta en cuarentena.

Esto no es un falso positivo: los datos de autenticacion no son almacenables
ni cifrados, asi que su presencia significa que el productor tiene datos que
no deberia tener. Hay que avisar al equipo que lo genero, no solo reintentar.

Que lote fue:
  aws dynamodb scan --table-name intelica-proxy-ia-<ENTORNO>-batches
    --region eu-south-2 --filter-expression "#s = :c"
    --expression-attribute-names '{"#s":"status"}'
    --expression-attribute-values '{":c":{"S":"cuarentena"}}'
    --projection-expression "batch_id,raw_key,motivo,updated_at"

El informe, con la capa y la ruta exacta (nunca el valor):
  aws s3 ls s3://intelica-proxy-ia-<ENTORNO>-quarantine-<CUENTA>/quarantine/
```

### La que vigila `CanarioNoBloqueado`

```
CRITICO. El filtro dejo pasar tarjetas de prueba que deberia haber bloqueado.
El control de sanitizacion NO esta funcionando.

Mientras esto siga asi, cualquier lote puede llevarse datos de tarjeta a
Anthropic sin que nada lo pare. Considera parar la ingesta hasta resolverlo.

Que planto y que paso:
  aws dynamodb get-item --table-name intelica-proxy-ia-<ENTORNO>-batches
    --key '{"batch_id":{"S":"__canario__"}}' --region eu-south-2

Ese item trae 'ultimo_lote'. Consulta ese batch_id: si su status no es
'cuarentena', el filtro fallo con ese caso.

  aws logs tail /aws/lambda/intelica-proxy-ia-<ENTORNO>-sanitizer --since 2h
```

### La que vigila `CanarioNoProcesado`

```
CRITICO. El canario planto su lote y el sanitizer no llego a procesarlo.
La cadena esta rota antes del filtro.

Sospechosos, en este orden: la regla de EventBridge del bucket raw esta
desactivada o con el patron mal, la Lambda sanitizer falla al arrancar, o el
canario no tiene permiso para escribir en raw.

  aws events list-rules --name-prefix intelica-proxy-ia-<ENTORNO>
    --region eu-south-2 --query 'Rules[].[Name,State]' --output table

  aws logs tail /aws/lambda/intelica-proxy-ia-<ENTORNO>-sanitizer --since 2h
```

### La que vigila `FalloDelSanitizer`

```
CRITICO. El verificador encontro un numero de tarjeta en la ZONA LIMPIA.
El sanitizer lo dejo pasar y el verificador lo caza con otro algoritmo.

Hay CHD fuera del entorno protegido. El objeto ya se borro de clean/ de forma
automatica, pero hay que averiguar como llego ahi y revisar si algun lote
anterior con el mismo patron si se envio a Anthropic.

El informe:
  aws s3 ls s3://intelica-proxy-ia-<ENTORNO>-quarantine-<CUENTA>/quarantine/verificador/

Trae la ruta y la marca de la tarjeta, nunca el numero.
```

### La que vigila `PanEnResultados`

```
CRITICO. Volvio un numero de tarjeta en la respuesta de Anthropic.
El segundo pase lo descarto y no se escribio en results/.

Dos causas posibles: el filtro de ida fallo y el dato salio, o el modelo
genero por su cuenta una cifra que valida Luhn. La primera es un incidente;
la segunda, ruido. Distinguirlas exige mirar el lote de origen.

Lo descartado, sin los valores:
  aws s3 ls s3://intelica-proxy-ia-<ENTORNO>-results-<CUENTA>/results/
    --region eu-south-2 | grep descartados
```

### La que vigila `LotesEnCuarentena`  ·  umbral 3

```
Tres o mas lotes de productores reales rechazados en cinco minutos. No es un
error suelto: apunta a una fuente de datos mal configurada aguas arriba.

Cada lote rechazado tiene un parte de estado que el propio productor puede
leer, con la capa que disparo y que hacer:

  aws s3 ls s3://intelica-proxy-ia-<ENTORNO>-clean-<CUENTA>/estado/
    --region eu-south-2

Empieza por ahi antes de mirar el CDE: el parte suele bastar para saber a que
equipo avisar.
```

### La que vigila `LotesExpirados`

```
Un lote paso 24 h en Anthropic sin completarse y expiro. Lo expirado no se
factura, pero el trabajo se perdio y hay que reenviarlo.

La causa habitual es saturacion de la cola en vuelo: si se superan las 200.000
peticiones encoladas del tier, Anthropic no da error, deja que expiren en
silencio. Revisa la ocupacion antes de reenviar:

  aws dynamodb get-item --table-name intelica-proxy-ia-<ENTORNO>-batches
    --key '{"batch_id":{"S":"__inflight__"}}' --region eu-south-2
```

### La que vigila `OcupacionCola`  ·  umbral 80 %

```
La cola en vuelo supera el 80% del tier de Anthropic (200.000 peticiones).

Pasarse no devuelve un error: devuelve expiraciones silenciosas a las 24 h. Si
sigue subiendo, baja SUBMIT_MAX_POR_TICK o pausa la ingesta hasta que los
lotes en vuelo se vacien.

  aws dynamodb get-item --table-name intelica-proxy-ia-<ENTORNO>-batches
    --key '{"batch_id":{"S":"__inflight__"}}' --region eu-south-2
```

## 3 · Dos cosas que ayudan y cuestan poco

**Prefijo de severidad en el nombre.** Renombra las alarmas para que la
severidad se lea en el asunto del correo, que es lo único que se ve en el móvil:

```
CRITICO-intelica-proxy-ia-<entorno>-hard-block
AVISO-intelica-proxy-ia-<entorno>-lotes-en-cuarentena
```

Ojo: en Terraform, cambiar `alarm_name` **destruye y recrea** el recurso. Es
inofensivo aquí —una alarma no guarda estado que importe— pero el plan mostrará
destrucciones y conviene saber que es esperado. Si prefieres evitarlo, deja los
nombres y quédate sólo con las descripciones: son el 90 % del valor.

**`ok_actions` al mismo topic** en las alarmas de volumen (`lotes-en-cuarentena`,
`cola-casi-llena`). Sin eso nadie se entera de que la situación se resolvió y
alguien sigue investigando algo que ya pasó. No lo pongas en las críticas: ahí el
cierre lo decide una persona, no la métrica.

## Lo que NO cambia

Los umbrales, los períodos, las métricas vigiladas y el topic SNS. Esto es
reescribir textos y añadir dos métricas a la lista de las que no alarman.
