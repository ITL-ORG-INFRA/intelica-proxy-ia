# intelica-proxy-ia

Código de las Lambdas del proxy de sanitización PCI hacia la **Message Batches API
de Anthropic**.

El productor deja un lote en S3. El proxy lo sanitiza, comprueba que quedó limpio,
lo envía a Anthropic, espera, recoge los resultados, los vuelve a sanitizar y los
deja en otro bucket.

> **Este repo contiene código, no infraestructura.** Los buckets, claves, roles,
> tablas, reglas y alarmas viven en el repo de Terraform. Aquí sólo está el Python
> y lo que hace falta para publicarlo.
> Ver [docs/FRONTERA-CON-TERRAFORM.md](docs/FRONTERA-CON-TERRAFORM.md).

---

## Empezar

```bash
make venv       # entorno virtual con las dependencias
make pruebas    # las tres suites (99 comprobaciones)
make build      # artefactos reproducibles en dist/
```

El despliegue normal es automático: al mergear a `main`, el CI construye, despliega
a `dev`, luego a `qa`, y espera aprobación para `prod`. Para hacerlo a mano:

```bash
make build && make publicar-dev && make verificar-dev
```

Hay tres entornos: `dev`, `qa` y `prod`. `make verificar-todo` los compara los tres
de una vez — útil después de cada `terraform apply`.

`verificar-dev` compara el `CodeSha256` de cada función desplegada con el sha256 del
zip local. Responde exactamente a la pregunta que llega en toda auditoría y en todo
incidente: *¿el código que corre es el que está en el repo?*

---

## La idea que sostiene todo el diseño

**Ningún rol tiene a la vez acceso a datos de tarjeta y salida a internet.**

```
  FRONTERA CDE                          ZONA LIMPIA
  ┌────────────────────────┐            ┌──────────────────────────┐
  │  s3 raw ─┐             │            │  s3 clean                │
  │          ▼             │            │     │                    │
  │      λ SANITIZER       │            │     ▼                    │
  │      0 normalización   │  limpias   │  λ VERIFICADOR           │
  │      1 envelope        │ ─────────► │  (implementación         │
  │      2 nombre campo    │            │   distinta)              │
  │      3 PAN texto libre │            │     │                    │
  │      4 SAD  ▸ duro     │            │     ▼                    │
  │      5 binario         │            │  ADMISIÓN (cola vuelo)   │
  │          │             │            │     │                    │
  │      ◆ GATE  ──rechazo─┼─► s3       │     ▼                    │
  │          │             │  quarantine│  λ SUBMITTER ────────────┼──► Anthropic
  │  λ CANARIO 1×/h        │            │                          │    Batch API
  └────────────────────────┘            │  λ RECONCILIADOR ◄───────┼──  polling
       CMK-raw                          │  λ FETCHER+SANITIZER ◄───┼──  results
                                        │     │                    │
                                        │     ▼  s3 results        │
                                        └──────────────────────────┘
                                             CMK-clean
```

La frontera no es la red — en el MVP hay una sola cuenta y las Lambdas están fuera
de VPC. La frontera son **las claves y los roles**:

| Rol | Ve CHD | Habla con Anthropic |
|---|:---:|:---:|
| `rol-sanitizer` | sí | **no** (Deny sobre el secreto) |
| `rol-verificador` | sí | **no** (Deny sobre el secreto) |
| `rol-submitter` | **no** (Deny sobre raw, quarantine y CMK-raw) | sí |
| `rol-canario` | escribe en raw | **no** |

Robar cualquiera de esas credenciales no saca una tarjeta.

---

## Las seis capas del sanitizer

| # | Capa | Qué hace | Si acierta |
|---|---|---|---|
| 0 | Normalización | NFKC, zero-width, fullwidth, guiones Unicode, base64 | — |
| 1 | Envelope | Schema estricto **deny-by-default** | rechaza la petición |
| 2 | Nombre de campo | `pan`/`cc`/`cvv`/`track2`/`expiry`… | destruye el valor |
| 3 | PAN texto libre | 13–19 contiguo o 4-4-4-4, **+ Luhn + IIN** | rechaza la petición |
| 4 | SAD | track1/2, CVV en contexto, PIN | **bloqueo duro: aborta el lote** |
| 5 | Binario | `image`/`document`, base64 binario, `data:` URI | rechaza la petición |

Después, el **GATE**: si los rechazos superan `GATE_REJECT_PCT` (1 % por defecto) o
`GATE_REJECT_ABS`, aborta el lote **entero**. Muchos rechazos no son errores sueltos:
son un productor mandando CHD de forma sistemática, y dejar pasar "sólo las buenas"
sería normalizarlo.

Dos reglas que el código sostiene de forma activa, no por convención:

- **Se envía lo que se escaneó.** El texto normalizado es el que viaja, no el
  original. Si escaneas una representación y envías otra, el hueco entre ambas es
  por donde se cuela un PAN.
- **Un hallazgo nunca lleva el valor.** Lleva capa, tipo, ruta y marca. Si lo
  llevara, acabaría en un log o en un mensaje de error, y el sanitizador sería el
  sitio por donde se escapa lo que venía a contener. `logs.py` además trunca y
  sustituye cualquier tirada larga de dígitos: no depende de que nadie se equivoque.

---

## Por qué el verificador usa otro algoritmo

Si el verificador buscara igual que el sanitizer, fallaría en los mismos casos y su
"no encontré nada" no sería evidencia de nada.

- **Sanitizer**: expresiones regulares → Luhn → IIN.
- **Verificador**: sin regex. Extrae *todos* los dígitos, tira los separadores sean
  cuales sean, y desliza una ventana de 13 a 19 comprobando Luhn e IIN en cada posición.

La prueba `tests/deteccion2_test.py` verifica que esto sirve de algo: hay seis
formas de escribir un PAN (`4111.1111.1111.1111`, separado por `/`, por `_`, por
saltos de línea, disperso entre palabras) **ante las que el regex es ciego y la
ventana no**. Encontrar algo aquí no es un productor malo: es un fallo del
sanitizer, así que el objeto se borra de la zona limpia y salta la alarma crítica.

## Por qué hay un canario

"Macie no encontró nada" no es evidencia, porque no se distingue de "Macie no miró".
El canario sí es falsable: cada hora planta PANes de prueba conocidos en raw y
comprueba que el anterior acabó en cuarentena. Si dejan de bloquearse, se sabe.

Macie además dispara *después* del POST — el dato ya salió — y usa el mismo
Luhn+IIN, así que sus fallos están correlacionados con los nuestros.

## Por qué el polling no te banea

No hay webhooks en la Batches API. Pero el polling no es el cuello de botella:

- **Una llamada por tick, no una por lote.** `GET /v1/messages/batches?limit=100`
  devuelve el estado de hasta 100 lotes de una vez. Con tick de 5 min son 288
  peticiones al día: **0,2 RPM sobre 1.000 disponibles, el 0,02 %**. Da igual que
  haya 3 lotes o 90.
- **Cadencia adaptativa**: 0–5 min no se consulta · 5–60 min cada 5 · 1–24 h cada 15
  · >24 h expiró, se cierra y se alerta.

**El límite que muerde es la cola en vuelo**, no el polling. El start tier son
200.000 peticiones encoladas. Pasarse no devuelve un error: devuelve expiraciones
silenciosas a las 24 h. De ahí el gate de admisión, con un contador atómico en
DynamoDB, y la rampa gradual (`SUBMIT_MAX_POR_TICK`) porque subir de golpe dispara
429 aun estando dentro del RPM.

---

## Despliegue

```
  PR ──► CI: pruebas + shellcheck + construccion reproducible
   │
   └─ merge a main ──► construir (una vez) ──► dev ──► qa ──► [aprobación] ──► prod
                            │                   │       │                       │
                            └────── el MISMO artefacto ─────────────────────────┘
```

Se construye **una sola vez**. Los mismos zips recorren dev, qa y prod: es la
única forma de que "esto ya se probó en qa" signifique algo. Si cada entorno
reconstruyera, lo aprobado y lo desplegado serían artefactos distintos.

Con `workflow_dispatch` se puede desplegar a un solo entorno, sin cadena. La construcción es
reproducible (mismas fuentes → mismo sha256) y el CI lo comprueba en cada PR
construyendo dos veces y comparando.

| Comando | Qué hace |
|---|---|
| `./scripts/build.sh` | Construye `dist/` más un manifiesto con los sha256 |
| `./scripts/build.sh --no-layer` | Sólo el código, sin dependencias (más rápido) |
| `./scripts/publish.sh <dev\|qa\|prod>` | Sube código y layer. **No toca infraestructura** |
| `./scripts/verify.sh <dev\|qa\|prod>` | Compara lo desplegado con `dist/` |
| `./scripts/probar-fallos.sh <dev\|qa>` | Casos de fallo operativos y de lote contra el pipeline real |
| `./scripts/subir-lote.sh <entorno> <carpeta>` | Sube un lote de varias partes y **genera su `_MANIFEST.json`** |

`publish.sh` comprueba primero que las 6 funciones existan. Si falta alguna, aborta
sin tocar nada: publicar a medias deja el sistema con unas Lambdas nuevas y otras
viejas, que es peor que no publicar.

Lo único que `publish.sh` cambia de la configuración es el layer adjunto. Memoria,
timeout, concurrencia y variables de entorno son de Terraform.

## Estados

`recibido` → `limpio` → `verificado` → `enviado` → `terminado` → `entregado`

Salidas laterales: `cuarentena` (no cruza), `retenido` (no cabe en la cola),
`expirado` (24 h), `fallido`.

## Probar el filtro

En [ejemplos/](ejemplos) hay 33 lotes repartidos en ocho suites. Cada carpeta
lleva un `manifiesto.json` que **declara qué debería pasar** con cada fichero, así
que la batería vale como prueba de aceptación y no sólo como demostración.

| Suite | Qué cubre |
|---|---|
| `01-limpios` | Debe pasar entero: texto normal, `system`, bloques de texto |
| `02-pan-texto-libre` | Capa 3: Visa, Mastercard, Amex, PAN enterrado en un párrafo, PAN en el `system` |
| `03-evasiones` | Capa 0: ancho completo, ancho cero, guion suave, guion Unicode, base64 |
| `04-sad` | Capa 4: track1, track2, CVV, PIN → **abortan el lote entero** |
| `05-binario` | Capa 5: PNG, PDF, `data:` URI, bloque `image` |
| `06-envelope` | Capa 1: claves extra, modelo, `custom_id` duplicado / con PAN, rol inválido |
| `07-gate` | Por debajo, por encima y justo en el umbral del 1 % |
| `08-falsos-positivos` | UUID, hashes, IBAN, teléfonos, importes: no disparan nada |

**En local, sin AWS** — segundos por iteración. Corre el handler real del
sanitizer contra un S3 y un DynamoDB simulados, así que no puede divergir de lo
que hace producción:

```bash
.venv/bin/python scripts/probar_filtro.py                  # las ocho suites
.venv/bin/python scripts/probar_filtro.py ejemplos/04-sad  # una
.venv/bin/python scripts/probar_filtro.py mi-lote.json --detalle
```

**Contra el pipeline desplegado** — sube a `raw`, calcula el `batch_id` igual que
el sanitizer, espera el parte de estado y lo compara con el manifiesto:

```bash
./scripts/probar-flujo.sh dev
./scripts/probar-flujo.sh dev ejemplos/04-sad
```

Las suites `04-sad` disparan la alarma `BloqueoDuro`. Es correcto, pero avisa a
quien reciba las alarmas antes de lanzarlas.

**Volumen**, para ver el gate a escala:

```bash
.venv/bin/python scripts/generar-lotes.py --ficheros 20 --peticiones 500 --sucio 0.5
./scripts/probar-flujo.sh dev carga/*.json
```

`--desde-jsonl datos.jsonl` convierte datos reales al envelope. (La **entrada**
del proxy es JSON con un array `requests`; JSONL es el formato de los
**resultados**.)

## Pruebas

```bash
python3 -m venv .venv && .venv/bin/pip install -r layer/requirements.txt boto3 && ./tests/run.sh
```

| Fichero | Qué cubre |
|---|---|
| `tests/detectores_test.py` | 14 PANes de prueba de todas las marcas, 5 evasiones de codificación, SAD, base64, falsos positivos |
| `tests/deteccion2_test.py` | Que las dos implementaciones **no** fallen en los mismos sitios |
| `tests/e2e_test.py` | Pipeline completo sobre S3 y DynamoDB en memoria, incluido el contador atómico de la admisión |

## Estructura

```
src/common/               config, logs (sin payload), store, cliente Anthropic
src/sanitizer/            normalize · envelope · detectors · handler (gate)
src/verificador/          deteccion2 (ventana deslizante) · handler
src/submitter/            admisión + POST
src/reconciliador/        polling adaptativo, 1 llamada por tick
src/fetcher/              stream JSONL + 2º pase de sanitización
src/canario/              prueba de control horaria
layer/requirements.txt    dependencias del layer
scripts/                  build · publish · verify
tests/                    las tres suites y los dobles de AWS
.github/workflows/        ci.yml · deploy.yml
docs/                     frontera con Terraform · prompt del módulo
```

Cada Lambda se empaqueta con `src/common/` más su carpeta, todo en plano. Dos
llevan carpetas extra porque reutilizan los detectores: `verificador` y `fetcher`.

## Alarmas

| Alarma | Significa |
|---|---|
| `canario-no-bloqueado` | **Crítico.** El sanitizer dejó pasar PANes de prueba. |
| `canario-no-procesado` | **Crítico.** El canario no llegó al sanitizer: la cadena está rota. |
| `fallo-del-sanitizer` | **Crítico.** El verificador encontró PAN en la zona limpia. |
| `pan-en-resultados` | **Crítico.** Volvió un PAN desde Anthropic. |
| `bloqueo-duro` | Se detectó SAD. Revisar al productor. |
| `cola-casi-llena` | Cola en vuelo >80 %. Pasarse da expiraciones silenciosas. |

## Documentación

| Documento | Para qué |
|---|---|
| [EJEMPLO.md](docs/EJEMPLO.md) | Paso a paso de un lote real de varias partes: qué contiene cada fichero y en qué orden se sube |
| [PARA-PRODUCTORES.md](docs/PARA-PRODUCTORES.md) | Guía para quien manda lotes. Es la que se pasa a los equipos |
| [FRONTERA-CON-TERRAFORM.md](docs/FRONTERA-CON-TERRAFORM.md) | Quién manda sobre qué recurso, y qué exige el repo de Terraform |
| [PROMPT-TERRAFORM-MANIFIESTO.md](docs/PROMPT-TERRAFORM-MANIFIESTO.md) | Cambio pendiente en Terraform: disparo del submitter por `_MANIFEST.json` |
| [SOBRE-LOS-PANES-DE-PRUEBA.md](docs/SOBRE-LOS-PANES-DE-PRUEBA.md) | Por qué hay números de tarjeta en el repo y por qué no son datos reales |
| [PROMPT-TERRAFORM.md](docs/PROMPT-TERRAFORM.md) | Especificación completa de la infraestructura |

## Lo que el MVP deja fuera

| Upgrade | Por qué se pospone |
|---|---|
| Step Functions Distributed Map | Con una Lambda, el lote debe caber en memoria: `MAX_RAW_BYTES` es real. |
| VPC dedicada + NAT + Network Firewall (allowlist SNI) | Sin VPC no hay allowlist de egress. Se compensa con rol sin acceso a raw, URL destino constante en código, dependencias fijadas y GuardDuty Lambda Protection. Cuando llegue, **VPC dedicada, nunca la de los otros productos**. |
| Segunda cuenta AWS | Con una cuenta se pierde el blast radius de un credencial y la separación de CloudTrail. Compensar con SCP y bucket policy por rol. |
| Redacción / tokenización | El mapa PAN↔token sería seguir almacenando CHD. Por eso el MVP bloquea y no rehidrata. |

Pendiente y ajeno al código: **negociar ZDR con Anthropic**. Los resultados viven
~29 días de su lado, y TLS protege de terceros, no de Anthropic.
El sanitizador **es** un componente PCI y entra en el alcance de la auditoría.
