# Un lote con la forma del trabajo real

Los demás ejemplos de `ejemplos/` son ficheros sueltos que prueban **una cosa
cada uno**: un PAN en texto libre, una evasión de codificación, el gate. Sirven
para verificar el filtro, no para parecerse a nada.

Esto es lo contrario: **un lote de tres partes con la forma de un procesamiento
real** — extracción estructurada sobre fragmentos de una guía de tarifas.

## Qué tiene que no tienen los otros

| | Por qué está |
|---|---|
| **`system` idéntico byte a byte** en las 11 peticiones, con `cache_control: ephemeral` | Es lo que hace que un prompt de 930 bytes repetido once veces se cobre una vez. Si el sanitizer se comiera el `cache_control`, nada fallaría de forma visible: sólo se multiplicaría la factura |
| **`output_config` con `json_schema`** | La salida va forzada a un esquema, que es como se procesa de verdad. No se pide texto libre y luego se parsea con una expresión regular |
| **Contenido con cifras de las que dan miedo** | `120,000,000,000`, `USD 0.0000026`, `15,000,000,001`, rangos de tramos, números de página. Es exactamente lo que hace saltar a un detector mal calibrado |
| **Tres partes** | Ejercita el flujo del manifiesto: el lote no se envía hasta que las tres han pasado el filtro |
| **`custom_id` opacos** (`ARG-2AB1006T-16b4a3b9`) | Cruzan la frontera tal cual, así que no llevan nada identificable |

Dos de las once peticiones son fragmentos que **no contienen ningún evento de
facturación** (una remisión al manual de liquidación, una tabla de tramos). El
esquema obliga a devolver una lista vacía. Un corpus donde todos los fragmentos
tienen respuesta no se parece a un PDF de verdad.

## Ejecutarlo de punta a punta

```bash
./scripts/subir-lote.sh dev ejemplos/lote-multiparte lote-tarifas-01
```

Sube las tres partes a `raw/input/lote-tarifas-01/` y, al final, genera y sube
el `_MANIFEST.json` a `clean/input/lote-tarifas-01/` — que es lo que dispara el
envío. El `_MANIFEST.json` no está en la carpeta a propósito: lo escribe el
script a partir de lo que acaba de subir, para que su lista `files` no pueda
discrepar de la realidad.

Las partes van a `raw` y el manifiesto a `clean`, bajo la **misma** carpeta
`input/lote-tarifas-01/`. Es esa coincidencia de carpeta la que permite al
submitter emparejarlos sin leer `raw`, que es un bucket sobre el que no tiene —
ni debe tener— permiso.

Seguir el veredicto:

```bash
aws dynamodb get-item --region eu-south-2 \
  --table-name itl-0003-proxy-ia-dev-ddb-batches-03 \
  --key '{"batch_id":{"S":"batch#input/lote-tarifas-01"}}'
```

| `status` | Qué pasó |
|---|---|
| `awaiting_parts` | Aún faltan partes por sanitizar |
| `ready` | Las tres pasaron el filtro; el submitter lo va a enviar |
| `submitted` | Está en Anthropic. `anthropic_batch_id` dice cuál |
| `quarantined` | Alguna parte fue rechazada. El lote entero se para |

## Antes de subir nada

El filtro se puede pasar en local, sin tocar AWS ni gastar nada:

```bash
python3 scripts/probar_filtro.py ejemplos/lote-multiparte/parte-*.json
```

Las once peticiones deben salir limpias. Si alguna no lo hace, hay una
regresión en el sanitizer — este lote no tiene nada que ocultar.
