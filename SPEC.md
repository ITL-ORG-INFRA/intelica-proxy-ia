# Proxy de sanitización PCI → Anthropic Batch API

**Especificación de implementación. Documento autocontenido: no requiere contexto previo.**

Intelica · cuenta única · región `eu-south-2` · v1 (MVP)

---

## 1. Objetivo y encuadre honesto

Un pipeline que envía lotes de peticiones a la **Message Batches API de Anthropic**
interponiendo un sanitizador que **bloquea** cualquier dato de titular de tarjeta
(CHD/SAD) antes de que salga de la cuenta.

Tres cosas que hay que entender antes de escribir código:

1. **El sanitizador es un tripwire, no un redactor.** El corpus real (ver
   `tests/fixtures/real_batch.jsonl`) son guías de tarifas de Mastercard: códigos de
   fee, tablas de tiers, tasas. **No contiene datos de tarjeta.** El número esperado
   de hallazgos es **cero**. Un hallazgo no es un caso rutinario que redactar: es la
   señal de que algo se rompió aguas arriba. De ahí que la política sea bloquear.

2. **El riesgo de ingeniería es el falso positivo, no el falso negativo.** El corpus
   está lleno de cifras largas (`120,000,000,000`, `USD 0.0000026`, `72 of 877`). Un
   detector mal calibrado para el pipeline en producción. El fixture
   `tier_tables.jsonl` existe exactamente para prevenir esa regresión.

3. **El alcance PCI se confina, no se elimina.** El sanitizador toca (o puede tocar)
   CHD, así que es un componente dentro del alcance PCI DSS y hereda sus requisitos.
   Lo que se consigue es que Anthropic y todo lo que está detrás queden fuera, y que
   la superficie auditable sea pequeña y explícita.

---

## 2. Hechos verificados de la Batch API (NO adivinar, NO cambiar)

Comprobados contra la documentación oficial. Si algo aquí parece raro, verifícalo,
no lo reescribas de memoria.

| Hecho | Valor |
|---|---|
| Envío | `POST /v1/messages/batches` con **JSON**: `{"requests": [{custom_id, params}, …]}` |
| Resultados | `GET /v1/messages/batches/{id}/results` devuelve **JSONL**, un objeto por línea |
| Estado | `GET /v1/messages/batches/{id}` → `processing_status`: `in_progress` → `ended` (y `canceling`) |
| Listado | `GET /v1/messages/batches?limit=100` → estado de hasta **100 batches en una petición** |
| Tope por batch | 100.000 peticiones **o** 256 MB, lo que llegue primero |
| Duración | la mayoría **< 1 h**; expiración dura a las **24 h** (lo expirado no se factura) |
| Retención de resultados | **29 días** |
| Cabeceras | `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json` |
| **NO hay webhooks** | El polling es el único mecanismo para saber si terminó |

**Rate limits de la Batch API** (compartidos entre todos los modelos; el RPM aplica a
**todos** los endpoints: create, retrieve, list, results, cancel):

| Tier | RPM | Peticiones encoladas | Por batch |
|---|---|---|---|
| Start | 1.000 | 200.000 | 100.000 |
| Build | 2.000 | 300.000 | 100.000 |
| Scale | 4.000 | 500.000 | 100.000 |

En 429 se devuelve la cabecera `retry-after` (segundos). También existen **límites de
aceleración**: subir el volumen de golpe dispara 429 aun estando dentro del RPM.

**El límite que muerde no es el polling, es la cola encolada.** A ~5.615 B/petición
(medido en el corpus real), 256 MB se alcanzan a **~48.000 peticiones**, así que en
tier Start caben **~4 batches simultáneos**. Pasarse no da error: da **expiraciones
silenciosas a las 24 h**.

---

## 3. Alcance: qué se construye y qué NO

### En esta entrega

| # | Recurso | Notas |
|---|---|---|
| 1 | S3 `raw` | ingesta, KMS CMK propia, sin acceso público |
| 2 | Lambda `sanitizer` | por fichero, streaming, **sin permiso de red saliente** |
| 3 | S3 `clean` | KMS CMK **distinta** de la de `raw` |
| 4 | S3 `quarantine` | líneas rechazadas, nunca cruza |
| 5 | Lambda `submitter` | ensambla + POST, API key en Secrets Manager |
| 6 | DynamoDB `batches` | tracking + idempotencia |
| 7 | SQS + DLQ | delante del submitter |
| 8 | Lambda `canary` | prueba de control, 1×/h |
| 9 | Alarmas CloudWatch | dos, separadas (ver §7) |

### Explícitamente FUERA (no implementar, no "dejar preparado")

- **Redacción / surrogates / HMAC / KMS para tokens.** Solo bloqueo. Esto elimina en
  cascada: sin surrogate → sin HMAC → sin gestión de clave → **el recorrido del
  documento es de solo lectura**, no hay que reconstruir el JSON.
- **VPC, NAT Gateway, Network Firewall.** Las Lambda van **fuera de VPC**. Decisión
  deliberada: evita heredar route tables, NACLs, SGs y restricciones de los otros
  productos de la cuenta. Ver §8 para los controles compensatorios.
- **Step Functions / Distributed Map.** Una Lambda con streaming JSONL cubre el batch
  más grande que la API acepta.
- **Amazon Macie.** Descartado deliberadamente: dispara *después* del POST (el dato ya
  salió), usa el mismo algoritmo (Luhn+IIN) así que sus fallos están correlacionados
  con los del sanitizador, y "no encontró nada" no es evidencia falsable. Lo sustituye
  el canary (§7), que produce evidencia **positiva**.
- **Gate de admisión** (contador de cola en vuelo). Solo muerde a volumen. La tabla
  DynamoDB debe soportarlo (`n_requests` por batch) pero no se implementa la lógica.
- **Reconciler / poller / fetcher de resultados.** Siguiente entrega. La tabla y el
  esquema deben dejarlo encajar sin migración.
- **Decodificación base64/urlencode anidada** en el detector.

---

## 4. Formato de datos

**JSONL dentro, JSON en el borde de la API.** El submitter es el único que conoce el
formato que exige Anthropic; no se filtra aguas arriba.

Cada línea de un `.jsonl` es **un objeto request completo**:

```json
{"custom_id":"ARG-2AB1006T-16b4a3b9","params":{"model":"claude-sonnet-5","max_tokens":2000,"system":[{"type":"text","text":"...","cache_control":{"type":"ephemeral"}}],"output_config":{"format":{"type":"json_schema","schema":{...}}},"messages":[{"role":"user","content":"--- PAGE 27 ---\n2AB1006T ..."}]}}
```

Layout en S3:

```
raw/lote-2026-08-24/parte-01.jsonl
raw/lote-2026-08-24/parte-02.jsonl
raw/lote-2026-08-24/_MANIFEST.json     ← se sube AL FINAL
clean/lote-2026-08-24/parte-01.jsonl
quarantine/lote-2026-08-24/parte-01.jsonl
```

`_MANIFEST.json`:

```json
{"lote":"lote-2026-08-24","files":["parte-01.jsonl","parte-02.jsonl"],"total_requests":9412}
```

### Propiedades medidas del corpus real

Úsalas para dimensionar; están verificadas sobre `real_batch.jsonl`:

- **5.615 B por petición** de media.
- El bloque `system` pesa **4.017 B y es byte a byte idéntico en todas las peticiones**
  → **72% del payload es el mismo prompt repetido**. El dedup por hash es obligatorio,
  no una optimización: sin él escaneas ~9× de más.
- El contenido de usuario real es solo el **11%** del payload.
- `messages[].content` es **string plano**, no lista de bloques.
- `output_config.format.schema` restringe la salida a 8 campos string con
  `additionalProperties: false`.
- Los `custom_id` son opacos: `ARG-2AB1006T-16b4a3b9` (país + código de fee + hash).

---

## 5. Disparadores y flujo

```
raw/**/*.jsonl          ──[S3 notif, sufijo .jsonl]──►  λ sanitizer  (por fichero, concurrente)
                                                             ├──► clean/**/*.jsonl
                                                             └──► quarantine/**/*.jsonl
raw/**/_MANIFEST.json   ──[S3 notif, sufijo _MANIFEST.json]──► SQS ──► λ submitter
                                                                          ├──► POST a Anthropic
                                                                          └──► DynamoDB
```

**Dos configuraciones de notificación sobre el mismo bucket**, con filtros de sufijo
disjuntos. El sanitizador NO se dispara con el manifiesto y el submitter NO se dispara
con los ficheros de datos.

**Por qué SQS solo delante del submitter:** el submitter espera una precondición (que
existan todas las salidas `clean/` del manifiesto). El manifiesto puede llegar antes de
que el sanitizador acabe. Si faltan, el submitter devuelve el mensaje a la cola y
reintenta con backoff. SQS aporta reintentos y DLQ gratis. El sanitizador no espera
nada, así que va con invocación directa.

**La DLQ solo transporta punteros** (bucket + key + lote), nunca contenido.

---

## 6. Módulos y contratos

```
src/
  policy.py     # YA ESCRITO. Autoritativo. Datos, no lógica.
  detect.py     # PURO: normaliza + detecta. Sin AWS, sin I/O, sin logging.
  sanitize.py   # PURO: valida una línea / un stream. Devuelve Verdict.
  store.py      # S3 + DynamoDB
  api.py        # cliente de la Batch API
  handler_sanitize.py
  handler_submit.py
  handler_canary.py
tests/
  fixtures/     # YA GENERADOS. Ver §9.
```

`src/policy.py` ya existe en la carpeta y es la fuente de verdad de la allowlist, los
subárboles exentos, los nombres de campo sensibles y los interruptores de política.
**Léelo antes de escribir `detect.py`.**

### Contratos

```python
@dataclass(frozen=True)
class Finding:
    rule: str       # "pan" | "sad.track1" | "sad.track2" | "sad.cvv" |
                    # "field_name" | "schema.unknown_key" | "schema.content_blocks" |
                    # "schema.malformed"
    path: str       # "$.params.messages[0].content"
    severity: str   # "CHD" | "SCHEMA"
    line_no: int
    # NO lleva el valor detectado. NUNCA.

@dataclass
class LineVerdict:
    ok: bool
    custom_id: str | None
    findings: list[Finding]

@dataclass
class Verdict:
    n_lines: int
    n_ok: int
    findings: list[Finding]
    stats: dict[str, int]        # contadores por regla, para EMF
```

```python
# detect.py
def normalize(s: str) -> str: ...
def luhn_ok(digits: str) -> bool: ...
def find_pan(text: str) -> list[str]: ...        # devuelve nombres de regla, no valores
def find_sad(text: str) -> list[str]: ...
def scan_text(text: str) -> list[str]: ...       # pan + sad

# sanitize.py
def validate_line(raw_line: str, line_no: int, pol: Policy) -> LineVerdict: ...
def validate_stream(lines: Iterable[str], pol: Policy) -> tuple[Verdict, list[str], list[str]]:
    """Devuelve (veredicto, líneas_limpias, líneas_rechazadas)."""
```

`validate_line` es el átomo: es lo que testean los fixtures, lo que invoca el canary y
lo que un futuro worker de Distributed Map llamaría sin cambios. **Debe ser puro.**

---

## 7. El detector: 6 capas

### Capa 0 — Normalización

`unicodedata.normalize("NFKC", s)` y luego eliminar zero-width:
`​ ‌ ‍ ⁠ ﻿`.

NFKC convierte dígitos fullwidth (`４１１１…`) a ASCII. El fixture
`poisoned_pan_fullwidth.jsonl` verifica esto.

> **ORDEN DE EVALUACIÓN (crítico).** La capa 2 (nombre de campo) se evalúa **antes**
> que la capa 1 (allowlist). Un campo llamado `card_number` no está en la allowlist, así
> que si se evalúa primero la capa 1 el hallazgo sale como `schema.unknown_key` con
> severidad `SCHEMA` — y **suena la alarma equivocada** para lo que en realidad es un
> posible dato de tarjeta. Verificado: invertir el orden produce ese fallo.
> Orden correcto: **0 → 5 → 2 → 1 → 3/4**.

### Capa 1 — Estructural (deny-by-default)

Recorrer el objeto y normalizar cada ruta a la forma `$.params.messages[].content`
(índices de array → `[]`). Cualquier ruta que no esté en `policy.ALLOWED_PATHS` es un
`Finding(rule="schema.unknown_key", severity="SCHEMA")`.

**No entrar** en los subárboles de `policy.EXEMPT_SUBTREES` para aplicar la allowlist —
un JSON Schema tiene estructura libre y no se puede enumerar. Sus **cadenas sí se
escanean** con las capas 3 y 4.

Una línea que no parsea → `Finding(rule="schema.malformed")`.

### Capa 2 — Nombre de campo

Si el último segmento de una ruta (en minúsculas, sin `_`) está en
`policy.SENSITIVE_FIELDS` → `Finding(rule="field_name", severity="CHD")`, sin mirar el
formato del valor. Caza valores cifrados, ofuscados o con formato raro.

### Capa 3 — PAN en texto libre

**Doble puerta obligatoria: Luhn Y prefijo IIN.** Solo Luhn deja pasar 1 de cada 10
cifras al azar; el prefijo es lo que evita los falsos positivos en tablas financieras.

Candidato (nótese que **la coma NO es separador** — en este corpus es separador de
miles):

```python
_PAN = re.compile(r"""
    (?<![0-9])(?:
        [0-9]{13,19}                                    # contiguo
      | [0-9]{4}[ -][0-9]{4}[ -][0-9]{4}[ -][0-9]{1,4}  # 4-4-4-4
      | 3[47][0-9]{2}[ -][0-9]{6}[ -][0-9]{5}           # amex 4-6-5
    )(?![0-9])
""", re.X)
```

Prefijos IIN aceptados: Visa `4`; Mastercard `51-55` y `2221-2720`; Amex `34`,`37`;
Discover `6011`,`65xx`,`644-649`,`622x`; Diners `300-305`,`36`,`38`; JCB `3528-3589`.

Validación medida sobre el corpus real: **0 candidatos, 0 falsos positivos.** La
variante permisiva (comas y saltos como separadores) produce 2 candidatos
(`120,000,000,000`) que fallan ambas puertas. **Usar la restrictiva.**

### Capa 4 — SAD (datos sensibles de autenticación)

Track 1: `%B\d{12,19}\^[^\^]{2,30}\^\d{4}`
Track 2: `;?\d{12,19}[=D]\d{4}\d{3}`
CVV **solo contextual**: `(?:cvv2?|cvc2?|cid|csc|c[oó]digo de seguridad|security code)\W{0,10}(\d{3,4})(?!\d)`

Nunca detectar CVV sin contexto: 3-4 dígitos sueltos aparecen por todas partes.

### Capa 5 — Binario

Si `messages[].content` no es `str` y `policy.allow_content_blocks` es `False` →
`Finding(rule="schema.content_blocks", severity="CHD")` **y NO descender en ese
subárbol**. Sin el cortocircuito, las claves del bloque (`type`, `source`, `media_type`,
`data`) disparan además `schema.unknown_key` y una sola causa raíz produce dos hallazgos
con severidades distintas. Un regex no ve una foto de una tarjeta. Hoy el corpus es todo strings, así que esto no cuesta nada y protege si
alguien mete PDFs más adelante.

### Dedup por hash (obligatorio)

Antes de escanear una cadena, calcular `blake2b(text, digest_size=16)` y consultar una
caché en memoria de veredictos. El bloque `system` es idéntico en todas las líneas: sin
esto escaneas 4 KB × N veces en vez de una.

### Dos severidades, dos alarmas

| Severidad | La disparan | Significado | Alarma |
|---|---|---|---|
| `CHD` | pan, sad.*, field_name, content_blocks | entró un documento que no debía | **paginar a una persona** |
| `SCHEMA` | schema.unknown_key, schema.malformed | el productor o Anthropic añadió un campo | actualizar la allowlist |

**Deben ser alarmas separadas.** Si se mezclan, el día que Anthropic añada un parámetro
suena "posible dato de tarjeta" y se quema la credibilidad del control.

### Umbral

`policy.max_findings = 0`: **cualquier** hallazgo aborta el lote completo. Las líneas
rechazadas van a `quarantine/`, el lote no se envía, y salta la alarma correspondiente.

### El canary (prueba de control)

Lambda con schedule 1×/h que invoca `validate_line` **directamente** (no por el
pipeline, para que un PAN de prueba no pueda acabar en un batch real) con los fixtures
envenenados, y verifica que cada uno produce el hallazgo esperado. Si alguno **no** se
detecta → alarma `CHD`: el control está caído.

Produce **evidencia positiva** (una detección que tiene que ocurrir), a diferencia de un
escáner que dice "no encontré nada" y no distingue entre "funciona" y "está roto".

---

## 8. Infraestructura

IaC a elección del implementador (Terraform o SAM/CDK). Requisitos no negociables:

### Buckets

- `raw`, `clean`, `quarantine`: versionado activado, acceso público bloqueado,
  `aws:SecureTransport` obligatorio, **CMK distinta para `raw` y para `clean`**.
- Lifecycle en `quarantine`: retención corta y explícita.

### IAM — la frontera del diseño

Con una sola cuenta, **el rol y la política son la frontera**. Dos roles distintos, y
ninguno con ambas capacidades:

| Rol | Puede | NO puede |
|---|---|---|
| `sanitizer` | leer `raw`, escribir `clean` y `quarantine`, sus CMK | **ninguna acción de red saliente**; nada de Secrets Manager |
| `submitter` | leer `clean`, Secrets Manager, DynamoDB, SQS | **`Deny` explícito sobre `arn:aws:s3:::raw/*`** |

Ese `Deny` explícito es el corazón del argumento: comprometer un credencial no saca una
tarjeta, porque quien lee CHD no tiene salida y quien tiene salida no lee CHD.

### Secrets Manager

La API key se lee **en cold start** y se cachea en ámbito de módulo. **Nunca en variable
de entorno**: las env vars de Lambda las lee cualquiera con
`lambda:GetFunctionConfiguration`.

### DynamoDB `batches`

Facturación on-demand. TTL activado sobre `expires_at`.

```
PK = "manifest#<manifest_key>"
     status      = CLAIMED | SUBMITTED | FAILED
     batch_ids   = [ ... ]           # un manifiesto puede producir VARIOS batches
     claimed_at

PK = "batch#<batch_id>"
     manifest_key, status = SUBMITTED | ENDED | FETCHED | EXPIRED
     n_requests                      # para el futuro gate de admisión
     created_at
     expires_at                      # epoch = created_at + 24 h  → TTL
     results_key
```

### Compensación por estar fuera de VPC

Sin VPC no hay allowlist de egress y se está ciego a la salida de red. Obligatorio:

1. URL destino **constante en código**, no configurable. Nada acepta una URL externa.
2. **Dependencias pineadas por hash**, capa construida en CI. Sin firewall, una
   dependencia transitiva maliciosa es el único vector real de exfiltración. Idealmente
   solo `urllib3`, que ya viene en el runtime de Python.
3. API key desde Secrets Manager (ver arriba).
4. **Activar GuardDuty Lambda Protection** (`LAMBDA_NETWORK_LOGS`). GuardDuty ya está
   activo en la cuenta; es un toggle aparte. Comprobar con:
   `aws guardduty list-detectors --region eu-south-2` y luego
   `aws guardduty get-detector --detector-id ID --region eu-south-2`
5. **Upgrade futuro: VPC DEDICADA**, nunca la VPC compartida de los otros productos.

### Reglas transversales (invariantes)

- **Nunca payload en logs.** Solo EMF con contadores por regla. `Finding` no puede
  transportar el valor detectado — está en el tipo para que no dependa de la disciplina.
  Cuidado con `except` que interpolan contenido en el mensaje: es la fuga más común de
  este patrón.
- **Retención de logs corta y explícita.**
- **`custom_id` viaja a Anthropic.** Si alguna vez llevara un PAN o un id de cliente,
  sería una exfiltración por el campo que nadie revisa. La capa 3 lo escanea.
- **No rehidratar nunca el PAN.** Guardar un mapa PAN↔token sería seguir almacenando CHD.
- **TLS no protege DE Anthropic**, solo de terceros en el cable. Cifrar el payload no
  sirve: el modelo tampoco podría leerlo. Si un dato no le hace falta, se omite.
- **ZDR con Anthropic: negociar, no asumir.** Los resultados viven ~29 días de su lado.

---

## 9. Comportamiento del submitter

### Ensamblado

1. Leer el manifiesto y verificar que existe la salida `clean/` de **cada** fichero
   listado. Si falta alguna → devolver el mensaje a SQS (backoff), no fallar.
2. Recorrer las líneas de todas las salidas `clean/` en streaming, escribiendo el cuerpo
   `{"requests":[…]}` a **`/tmp`** (configurable hasta 10 GB) en vez de a memoria. POST
   desde el fichero con `Content-Length` conocido: evita la incertidumbre del chunked
   encoding.
3. **Partir por topes**: acumular hasta el menor de 100.000 líneas o **200 MB** (margen
   bajo los 256 MB). Al llegar, cerrar el batch y abrir otro. **Un manifiesto puede
   producir varios `batch_id`.**
4. **Verificar unicidad de `custom_id` en la misma pasada** con un `set` de hashes
   (100k ids ≈ 4 MB). Los `custom_id` deben ser únicos *dentro del batch*; al fusionar
   ficheros pueden colisionar (dos ficheros con el mismo fee del mismo país) y **el POST
   entero se rechaza**. Fallar con un mensaje claro que nombre el id duplicado y sus dos
   ficheros de origen.
5. **No tocar el bloque `system` si está limpio.** Es lo que sostiene el prompt caching
   (`cache_control: ephemeral`) y su ahorro de coste.

### Idempotencia — la clave NO es el batch_id

El `batch_id` no existe hasta *después* de llamar a la API, así que no puede ser la
clave de idempotencia. **La clave es el manifiesto.** Secuencia obligatoria:

```
1. PutItem "manifest#<key>" con ConditionExpression: attribute_not_exists(PK)
   → si el evento de S3 llega duplicado, la 2ª invocación falla aquí y sale limpia
2. POST a la Batch API
3. UpdateItem: guardar batch_id(s), status = SUBMITTED
```

Los eventos de S3 son **at-least-once**: llegan duplicados. Sin el paso 1 condicional se
envía el mismo lote dos veces y se paga dos veces.

### La ventana entre el POST y la escritura

Si la Lambda muere entre 2 y 3, el batch **está enviado** y no se tiene su id: se está
procesando, se va a pagar, y no se sabe.

No se cierra con transacciones. Se cierra con **reconciliación**: el futuro poller
compara `GET /v1/messages/batches` con la tabla y **adopta los huérfanos** (batches que
existen en Anthropic y no en la tabla). Por eso el diseño usa `list` y no solo
`retrieve`. Dejar el esquema preparado para eso.

### Cliente HTTP

- `max_retries = 0` explícito en el poller (siguiente entrega): el reintento silencioso
  del SDK convierte una tormenta de 429 en latencia invisible y la métrica de errores
  queda plana.
- Leer y persistir `retry-after` y `anthropic-ratelimit-requests-remaining` / `-reset`
  para que el futuro poller se auto-frene con datos reales.

---

## 10. Tests

Los fixtures **ya están generados** en `tests/fixtures/` y derivan del archivo real de
producción. El canary los reutiliza: la suite y la prueba en producción comparten
fixtures a propósito.

| Fixture | Debe producir |
|---|---|
| `real_batch.jsonl` (5 líneas) | **0 hallazgos.** Es el corpus real. |
| `tier_tables.jsonl` | **0 hallazgos.** Contiene `120,000,000,000`, `15,000,000,000`, `USD 0.0000026`. **Es el test anti-falso-positivo y el más importante de la suite**: protege contra el día que alguien "mejore" el regex y el pipeline se pare a las 3 de la mañana por una tabla de tarifas. |
| `poisoned_pan_content.jsonl` | 1 × `pan` en `$.params.messages[0].content` |
| `poisoned_pan_spaced.jsonl` | 1 × `pan` (formato `4111 1111 1111 1111`) |
| `poisoned_pan_fullwidth.jsonl` | 1 × `pan` (dígitos fullwidth; prueba la capa 0) |
| `poisoned_pan_custom_id.jsonl` | 1 × `pan` en `$.custom_id` |
| `poisoned_pan_system.jsonl` | 1 × `pan` en `$.params.system[0].text` |
| `poisoned_track2.jsonl` | `sad.track2` **y** `pan` — los datos de banda contienen el PAN, así que ambas reglas disparan legítimamente. No es un bug. |
| `poisoned_cvv_ctx.jsonl` | 1 × `sad.cvv` |
| `poisoned_unknown_key.jsonl` | 1 × `schema.unknown_key` (`$.params.metadata`), severidad **SCHEMA** |
| `poisoned_field_name.jsonl` | 1 × `field_name` (`$.params.card_number`), severidad **CHD** — y **no** `schema.unknown_key`. Este test fija el orden de capas. |
| `poisoned_content_blocks.jsonl` | 1 × `schema.content_blocks` **y nada más** — verifica el cortocircuito del subárbol. |

**Esta tabla está verificada, no es aspiracional.** Se implementó el detector tal como
lo especifica §7 y se ejecutó contra los 12 fixtures: los 12 producen exactamente lo
indicado. Ese ejercicio detectó tres errores en versiones previas de esta spec (el
orden de capas 2/1, el cortocircuito de la capa 5, y que los datos de banda contienen
un PAN válido). Si tu implementación difiere de la tabla, el bug es tuyo, no de la tabla.

Los PAN de prueba son números públicos documentados (`4111111111111111`,
`5500000000000004`, `378282246310005`): no introducen CHD real.

Test adicional obligatorio: **ningún `Finding` serializado debe contener ninguna
subcadena de 6+ dígitos consecutivos.** Es la verificación automática de la invariante
"no se filtra el valor por logs".

---

## 11. A confirmar con Diego antes de cerrar

1. **Tier de la organización en Anthropic** (Console → Settings → Limits). Cambia el
   tope de cola encolada: 200k / 300k / 500k, y con él cuántos batches caben a la vez.
2. **Estado de GuardDuty Lambda Protection** (comando en §8).
3. **Origen de los PDFs** que alimentan `{document_text}`. Si son solo publicaciones de
   Mastercard, el sanitizador es un tripwire puro y el canary será lo único que
   dispare nunca — que es el resultado correcto. Si el mismo pipeline procesa también
   ficheros de clearing, extractos o expedientes de disputa, hay que reconsiderar el
   modo `redact` y reactivar el gate de admisión.
