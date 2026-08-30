"""Los senuelos que planta el canary.

Modulo aparte y sin dependencias a proposito: lo importan tanto la Lambda que
los planta como la prueba que comprueba que el sanitizer los bloquea. Si fueran
dos copias podrian divergir, y entonces la prueba pasaria verificando unos cases
mientras el canary real planta otros — la peor version posible de tener una
prueba.

Los numeros son de prueba, publicados por las marcas para entornos de
integracion. No son datos de tarjeta reales.
Ver docs/SOBRE-LOS-PANES-DE-PRUEBA.md.
"""

#: (nombre del caso, texto que se planta). Cada uno ejercita una rama distinta
#: del detector: anadir uno aqui es ampliar lo que el canary vigila.
CASES = [
    ("visa-contiguo",   "El numero es 4111111111111111 gracias"),
    ("mc-agrupado",     "Tarjeta: 5555 5555 5555 4444"),
    ("amex-guiones",    "AMEX 3782-822463-10005"),
    ("visa-fullwidth",  "Pago con ４１１１１１１１１１１１１１１１"),
    ("visa-zerowidth",  "Numero 4111​1111​1111​1111"),
    ("track2-sad",      "banda ;4111111111111111=25121011000000000?"),
    ("cvv-context",    "el CVV es 123 y caduca en 12/26"),
]
