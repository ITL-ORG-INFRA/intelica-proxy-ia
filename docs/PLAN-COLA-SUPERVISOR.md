# Plan: sustituir el polling por horario por una cola

Cambio decidido pero **no implementado**. Este documento es la instrucción para
quien lo retome.

## Qué problema resuelve

Hoy el reconciliador y el fetcher se despiertan cada 5 minutos aunque no haya
nada que hacer: 576 invocaciones al día que casi siempre salen en 130 ms sin
llamar a Anthropic. El coste es despreciable (0,14 % de la capa gratuita), así
que **no se hace por ahorro**. Se hace por dos razones concretas:

1. **Latencia.** Entre que Anthropic acaba y el JSONL aparece en S3 hay hasta
   10 minutos: cinco en que el reconciliador se entere, y otros cinco hasta el
   siguiente tick del fetcher.
2. **El estado del trabajo pendiente vive en un `if`** que lee DynamoDB, en vez
   de en algo que se pueda observar y que avise cuando algo se pierde.

## Por qué SQS y no encender/apagar reglas

Se valoró que las Lambdas activaran y desactivaran su propia regla de
EventBridge. Se descartó: exige `events:EnableRule` y `DisableRule` —permisos
que en un componente PCI hay que justificar— y abre una carrera que produce un
huérfano silencioso:

```
supervisor lee la tabla → no hay nada → va a desactivar
submitter escribe el lote y activa la regla
supervisor desactiva
   → el lote está en Anthropic, se factura, y nadie va a preguntar por él
```

Con SQS esa carrera no existe: el mensaje ES el estado, y si se pierde acaba en
la DLQ y salta una alarma. Un `EnableRule` perdido no deja rastro.

## Diseño

```
submitter
   ├─ POST a Anthropic
   ├─ escribe "enviado" en DynamoDB
   └─ encola {batch_id} con DelaySeconds = 300     ← cierre del submitter
            │
            ▼
      ┌──────────┐
      │   SQS    │
      └────┬─────┘
           ▼
     supervisor ── pregunta a Anthropic por ESE batch_id
           ├─ in_progress → se reencola (300 s si <60 min, 900 s si más)
           └─ ended       → invoca al fetcher ASÍNCRONO y no reencola

     cola vacía = cero invocaciones
```

**El supervisor no espera al fetcher.** Invocación asíncrona
(`InvocationType="Event"`): su trabajo son ~2 segundos y el visibility timeout
se queda en 60 s. Si esperase, heredaría el tiempo de una descarga de 200 MB y
SQS devolvería el mensaje a la cola a mitad, provocando una segunda descarga del
mismo lote.

No saber si la descarga funcionó no importa: el estado vive en DynamoDB, el lote
se queda en `terminado` si el fetcher falla, y el barrido horario lo reintenta.

## Cambios por fichero

### `src/submitter/handler.py`

Al final del envío exitoso, después de `store.marcar_lote(... ENVIADO ...)`,
encolar el `batch_id` con `DelaySeconds=300`. Unas diez líneas.

**El orden importa**: primero la escritura en DynamoDB, después el encolado. Si
se invierte, el supervisor puede leer la tabla antes de que el lote esté.

Si el encolado falla, **no** hacer fallar el envío: el lote ya está en Anthropic.
Registrarlo y dejar que el barrido horario lo recoja.

### `src/reconciler/handler.py` → pasa a ser el supervisor

Cambia de *«barro la tabla buscando lotes en vuelo»* a *«me dan un `batch_id`,
pregunto por ése»*.

- Desaparece `toca_preguntar()` y la lista `CADENCIA`. La cadencia adaptativa
  pasa a ser el valor de `DelaySeconds` al reencolar: 300 s mientras el lote
  tenga menos de 60 minutos, 900 s después.
- Sigue leyendo y persistiendo `retry-after` y los `anthropic-ratelimit-*`.
- Sigue detectando la expiración a las 24 h y marcando `expirado`.
- **Conserva** el modo barrido para el tick horario: si el evento no trae
  `batch_id`, barre lotes en vuelo como hasta ahora.

Un efecto secundario bienvenido: desaparece el desfase de relojes que hacía que
la primera consulta real ocurriera siempre a los ~10 minutos en vez de a los 5.

### `src/fetcher/handler.py`

Aceptar un `batch_id` concreto en el evento, además del barrido actual. La
lógica de descarga no se toca.

Añadir un **contador de intentos**: hoy, si la descarga falla, el lote se queda
en `terminado` y se reintenta indefinidamente sin que nadie se entere. A partir
de 5 intentos, marcar `fallido` con el motivo y emitir métrica.

```
intentos_descarga: 3
ultimo_fallo: "no cabe en /tmp"
```

Eso separa dos cosas que ahora se confunden: un fallo transitorio debe
reintentarse en silencio; uno persistente necesita que alguien lo mire.

### `src/common/store.py`

- `lotes_sin_mensaje()` para el barrido horario: lotes en vuelo cuyo
  `consultado_en` lleva más de X minutos sin actualizarse. Es lo que detecta un
  mensaje perdido.
- Campos nuevos en el item: `intentos_descarga`, `ultimo_fallo`.

### `src/common/config.py`

```
COLA_SUPERVISOR_URL     url de la cola
DELAY_CORTO = 300       reencolado mientras el lote es joven
DELAY_LARGO = 900       reencolado a partir de 60 min
MAX_INTENTOS_DESCARGA = 5
```

### `tests/`

Suite nueva, `tests/cola_test.py`, sobre los dobles existentes. Lo que hay que
cubrir:

- El submitter encola después de escribir en DynamoDB, no antes.
- Si el encolado falla, el envío no falla.
- El supervisor con `in_progress` reencola; con `ended` no.
- El delay sube de 300 a 900 al pasar de 60 minutos.
- El supervisor invoca al fetcher **asíncrono**.
- A los 5 intentos fallidos, el lote pasa a `fallido` con motivo.
- El barrido horario encuentra un lote en vuelo sin mensaje reciente.

## Qué NO cambia

- La frontera CDE / zona limpia. Ningún rol gana acceso a datos de tarjeta.
- El sanitizer, el verificador y el canario.
- El flujo del manifiesto.
- El segundo pase de sanitización en el fetcher.

## Riesgo a vigilar al implementar

El barrido horario es la red que convierte «se perdió un mensaje» en una hora de
retraso en vez de un lote huérfano permanente. **No lo dejes para el final ni lo
recortes**: sin él, el diseño tiene un punto único de olvido y el fallo es
silencioso.
