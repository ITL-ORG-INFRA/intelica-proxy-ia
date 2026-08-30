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

Sube las tres partes a `raw/entrada/lote-tarifas-01/` y, al final, genera y sube
el `_MANIFEST.json` — que es lo que dispara la validación y el paso a zona
limpia. El `_MANIFEST.json` no está en la carpeta a propósito: lo escribe el
script con los sha256 reales de lo que acaba de subir.

Seguir el veredicto:

```bash
aws dynamodb get-item --region eu-south-2 \
  --table-name intelica-proxy-ia-dev-batches \
  --key '{"batch_id":{"S":"lote#entrada/lote-tarifas-01"}}'
```

| `status` | Qué pasó |
|---|---|
| `esperando_partes` | Aún faltan partes por sanitizar |
| `listo` | Las tres pasaron el filtro; el submitter lo va a enviar |
| `enviado` | Está en Anthropic. `batch_id_anthropic` dice cuál |
| `cuarentena` | Alguna parte fue rechazada. El lote entero se para |

## Antes de subir nada

El filtro se puede pasar en local, sin tocar AWS ni gastar nada:

```bash
python3 scripts/probar_filtro.py ejemplos/lote-multiparte/parte-*.json
```

Las once peticiones deben salir limpias. Si alguna no lo hace, hay una
regresión en el sanitizer — este lote no tiene nada que ocultar.
