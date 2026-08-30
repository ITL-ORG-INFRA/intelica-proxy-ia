"""Segunda implementacion de la deteccion de PAN. Deliberadamente distinta.

El sanitizer busca con expresiones regulares: tiradas de 13-19 digitos o
grupos 4-4-4-4, y sobre lo que encuentra aplica Luhn e IIN. Si esta funcion
hiciera lo mismo, no verificaria nada: fallaria exactamente en los mismos
cases que el sanitizer y su "no encontre nada" no seria evidencia de nada.

Asi que aqui no hay regex. Se extraen TODOS los digitos del texto, se tiran
los separadores sean los que sean, y se desliza una ventana de 13 a 19 sobre
el flujo resultante comprobando Luhn e IIN en cada posicion.

Consecuencias buscadas:
  · Coge PANes partidos por caracteres que ningun regex previo contempla:
    "4111.x.1111/1111 abc 1111" es invisible para el sanitizer y evidente aqui.
  · Da mas falsos positivos. Da igual: esto corre sobre datos que ya deberian
    estar limpios. Cualquier acierto aqui es un fallo del sanitizer, y ante la
    duda se bloquea.
"""
from typing import Dict, List, Tuple

#: se comparte a proposito solo la tabla de marcas y el checksum: son hechos
#: del dominio (ISO/IEC 7812), no decisiones de implementacion. Lo que no se
#: comparte es COMO se localizan los candidatos, que es donde estan los fallos.
from detectors import iin_brand, luhn

MIN_PAN, MAX_PAN = 13, 19

#: mas alla de esto el texto no es una frase con un numero, es un volcado
MAX_DIGITS = 200_000


def _digit_stream(text: str) -> Tuple[str, List[int]]:
    """Devuelve todos los digitos seguidos y donde estaba cada uno."""
    digits: List[str] = []
    posiciones: List[int] = []
    for index, caracter in enumerate(text):
        if caracter.isdigit():
            # isdigit() acepta digitos no ASCII; se traducen a su valor.
            digits.append(str(int(caracter)) if caracter.isascii() else _value(caracter))
            posiciones.append(index)
            if len(digits) >= MAX_DIGITS:
                break
    return "".join(digits), posiciones


def _value(caracter: str) -> str:
    import unicodedata
    try:
        return str(unicodedata.digit(caracter))
    except (TypeError, ValueError):
        return "0"


def pans_in_stream(text: str) -> List[Dict[str, object]]:
    """Ventana deslizante sobre el flujo de digitos. Sin regex."""
    if not text:
        return []

    flujo, posiciones = _digit_stream(text)
    if len(flujo) < MIN_PAN:
        return []

    encontrados: List[Dict[str, object]] = []
    marked = set()

    inicio = 0
    while inicio <= len(flujo) - MIN_PAN:
        # Se prueban las longitudes de mayor a menor: un PAN de 16 no debe
        # reportarse ademas como uno de 13 contenido dentro.
        for longitud in range(min(MAX_PAN, len(flujo) - inicio), MIN_PAN - 1, -1):
            candidato = flujo[inicio:inicio + longitud]
            if not luhn(candidato):
                continue
            brand = iin_brand(candidato)
            if not brand:
                continue
            if any(inicio < fin and ini < inicio + longitud for ini, fin in marked):
                continue
            marked.add((inicio, inicio + longitud))
            encontrados.append({
                "brand": brand,
                "longitud": longitud,
                # posicion en el TEXTO, no el valor
                "text_position": posiciones[inicio],
                "separado": posiciones[inicio + longitud - 1] - posiciones[inicio] + 1 != longitud,
            })
            inicio += longitud - 1
            break
        inicio += 1

    return encontrados


def has_suspicious_digits(text: str) -> bool:
    """Senal barata y grosera: una tirada larga de digitos en datos ya limpios
    es rara de por si, aunque no valide Luhn."""
    seguidos = 0
    for caracter in text:
        if caracter.isdigit():
            seguidos += 1
            if seguidos >= MAX_PAN + 1:
                return True
        elif caracter not in " -":
            seguidos = 0
    return False
