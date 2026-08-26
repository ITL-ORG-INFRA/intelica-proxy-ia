## Qué cambia

<!-- Una o dos frases. Qué hace distinto el sistema después de esto. -->

## Por qué

<!-- El problema, no la solución. La solución ya está en el diff. -->

---

## Impacto PCI

Marca lo que aplique. Si marcas alguna, explica debajo.

- [ ] Toca `src/sanitizer/`, `src/verificador/` o `src/canario/`
- [ ] Cambia qué se detecta o qué se deja pasar
- [ ] Cambia el umbral del gate, la lista blanca de modelos o un límite
- [ ] Añade o quita una dependencia
- [ ] Añade números que parecen tarjetas (ver [docs/SOBRE-LOS-PANES-DE-PRUEBA.md](../docs/SOBRE-LOS-PANES-DE-PRUEBA.md))

<!-- Explicación: -->

## Comprobaciones

- [ ] `make pruebas` pasa en local
- [ ] Si toqué un detector, **añadí un caso que falla sin el cambio**
- [ ] Si toqué el canario, actualicé `src/canario/casos.py` (fuente única)
- [ ] No hay payload ni valores de tarjeta en logs ni en mensajes de error

## Si esto toca la frontera CDE

- [ ] Ningún rol gana a la vez acceso a CHD y salida a internet
- [ ] Ningún hallazgo transporta el valor que lo provocó
