# Sobre los números de tarjeta que hay en este repo

Hay números que parecen tarjetas en `tests/`, en `src/canary/cases.py` y en este
documento. **No son datos de tarjeta.** Esta página existe para que quien los
encuentre —una persona nueva, un escáner automático o un auditor— tenga la
respuesta sin tener que preguntar.

## Qué son

Números de prueba publicados por las propias marcas para entornos de integración.
Son públicos, aparecen en la documentación de Visa, Mastercard, Amex y del resto de
pasarelas de pago, y llevan décadas circulando. `4111111111111111` es probablemente
el número de prueba más conocido del sector.

Están construidos para pasar el checksum de Luhn y tener un IIN válido —por eso
sirven para probar— pero **no están asignados a ninguna cuenta, ningún emisor y
ninguna persona**. No hay titular. No hay saldo. No se pueden usar para nada.

## Por qué están en el repo y no fuera

Podrían vivir en un secreto o en un bucket. Sería peor:

- **El canario tiene que plantarlos.** Es la única prueba falsable de que el
  sanitizer bloquea. Si los números vivieran fuera del código, un fallo al leerlos
  haría que el canario plantara un lote vacío y **la prueba pasaría sin probar
  nada** — el peor fallo posible en un control de seguridad.
- **Las pruebas tienen que ser legibles.** Un caso de prueba que dice
  `pan_de_prueba_3` no le dice a nadie qué rama del detector está ejercitando.
- **Sacarlos del repo sugiere que son sensibles**, y no lo son. Tratarlos como si
  lo fueran confunde a quien venga después sobre qué sí lo es.

Lo que **nunca** debe entrar aquí es un PAN real, aunque sea de una tarjeta propia,
aunque esté caducada, aunque sea "sólo para reproducir un bug". Para eso están estos.

## Si un escáner los marca

GitHub secret scanning y varias herramientas de DLP detectan patrones de tarjeta y
pueden avisar sobre estos ficheros. Es esperable y correcto: el escáner hace su
trabajo. La respuesta es marcarlos como falso positivo apuntando a este documento,
no borrarlos.

Ficheros afectados:

- `src/canary/cases.py` — los siete señuelos que planta el canario
- `tests/detectores_test.py` — catorce tarjetas, una por marca y longitud
- `tests/detection2_test.py` — las mismas, para comparar las dos implementaciones
- `tests/e2e_test.py` — un lote con un PAN, para comprobar que el gate aborta

## Cómo comprobar que sigue sin haber nada real

Un PAN real y uno de prueba se distinguen mal a ojo. Lo que sí se puede vigilar es
que **no aparezcan números nuevos** que nadie haya justificado:

```bash
grep -rEo '[0-9]{13,19}' src/ tests/ | sort -u
```

Cualquier número en esa lista que no esté en este documento es algo que alguien
añadió y hay que revisar antes de aceptar el cambio. Los ficheros de arriba están en
`CODEOWNERS` precisamente por esto: tocarlos exige revisión explícita.
