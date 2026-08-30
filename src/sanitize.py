"""Validacion pura de una linea y de un stream. Sin AWS, sin I/O, sin logging.

Implementa las capas estructurales (1, 2 y 5) y orquesta las de texto (3 y 4),
que viven en detect.py.

    ORDEN DE EVALUACION:  0 -> 5 -> 2 -> 1 -> 3/4

No es arbitrario. La capa 2 (nombre de campo) va ANTES que la 1 (allowlist)
porque un campo llamado 'card_number' tampoco esta en la allowlist: si la 1 va
primero, el hallazgo sale como schema.unknown_key con severidad SCHEMA y suena
la alarma de "el productor anadio un campo" para lo que en realidad es un
posible dato de tarjeta. Invertir el orden reproduce ese fallo, y el fixture
poisoned_field_name.jsonl lo fija.

validate_line es el atomo: lo testean los fixtures, lo invoca el canary, y un
futuro worker de Distributed Map lo llamaria sin cambios. Por eso es puro.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from detect import normalize, scan_text
from policy import DEFAULT, Policy

#: Que severidad tiene cada regla. CHD pagina a una persona; SCHEMA solo pide
#: actualizar la allowlist. Tienen que ser alarmas separadas: el dia que
#: Anthropic anada un parametro no puede sonar "posible dato de tarjeta", o se
#: quema la credibilidad del control.
SEVERITY: Dict[str, str] = {
    "pan": "CHD",
    "sad.track1": "CHD",
    "sad.track2": "CHD",
    "sad.cvv": "CHD",
    "field_name": "CHD",
    "schema.content_blocks": "CHD",
    "schema.unknown_key": "SCHEMA",
    "schema.malformed": "SCHEMA",
}


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    severity: str
    line_no: int
    # NO lleva el valor detectado. NUNCA. Esta ausencia es del tipo, no de la
    # disciplina de quien lo construye.


@dataclass
class LineVerdict:
    ok: bool
    custom_id: Optional[str]
    findings: List[Finding] = field(default_factory=list)


@dataclass
class Verdict:
    n_lines: int = 0
    n_ok: int = 0
    findings: List[Finding] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)


def _finding(rule: str, path: str, line_no: int) -> Finding:
    return Finding(rule=rule, path=path, severity=SEVERITY.get(rule, "SCHEMA"),
                   line_no=line_no)


def _normalized_field(name: str) -> str:
    """'card_number' y 'cardNumber' son el mismo campo a estos efectos."""
    return name.replace("_", "").replace("-", "").lower()


def _is_exempt(path: str, exentos: frozenset) -> bool:
    """Dentro de un JSON Schema la estructura es libre y no se puede enumerar.

    Ahi no se aplica la allowlist — pero las cadenas SI se escanean.
    """
    return any(path == e or path.startswith(e + ".") or path.startswith(e + "[")
               for e in exentos)


def validate_line(raw_line: str, line_no: int, pol: Policy = DEFAULT) -> LineVerdict:
    """Valida UNA linea JSONL. Puro: mismos bytes, mismo veredicto."""
    try:
        objeto = json.loads(raw_line)
    except (json.JSONDecodeError, TypeError):
        return LineVerdict(ok=False, custom_id=None,
                           findings=[_finding("schema.malformed", "$", line_no)])

    if not isinstance(objeto, dict):
        return LineVerdict(ok=False, custom_id=None,
                           findings=[_finding("schema.malformed", "$", line_no)])

    findings: List[Finding] = []
    sensibles = {_normalized_field(f) for f in pol.sensitive_fields}

    def scan_string(text: str, path: str) -> None:
        for regla in scan_text(normalize(text)):
            findings.append(_finding(regla, path, line_no))

    def walk(nodo: Any, path: str, last: str) -> None:
        exento = _is_exempt(path, pol.exempt_subtrees)

        # --- Capa 5: bloques de contenido -----------------------------------
        # Cortocircuito obligatorio: sin el, las claves del bloque (type,
        # source, media_type, data) disparan ademas schema.unknown_key y una
        # sola causa raiz produce dos hallazgos con severidades distintas.
        if path == "$.params.messages[].content" and not isinstance(nodo, str):
            if not pol.allow_content_blocks:
                findings.append(_finding("schema.content_blocks", path, line_no))
                return

        # --- Capa 2: nombre de campo ----------------------------------------
        # Antes que la allowlist, y sin mirar el formato del valor: caza
        # valores cifrados, ofuscados o con formato raro.
        if last and _normalized_field(last) in sensibles:
            findings.append(_finding("field_name", path, line_no))
            return

        # --- Capa 1: allowlist deny-by-default ------------------------------
        if not exento and path != "$" and path not in pol.allowed_paths:
            findings.append(_finding("schema.unknown_key", path, line_no))
            return  # no se desciende: una causa raiz, un hallazgo

        # --- Capas 3 y 4: texto ---------------------------------------------
        if isinstance(nodo, str):
            scan_string(nodo, path)
            return

        if isinstance(nodo, dict):
            for key_, value in nodo.items():
                walk(value, f"{path}.{key_}", str(key_))
            return

        if isinstance(nodo, list):
            for element in nodo:
                # Los indices se normalizan a [] para que la allowlist no
                # tenga que enumerar posiciones.
                walk(element, f"{path}[]", "")
            return

    for key_, value in objeto.items():
        walk(value, f"$.{key_}", str(key_))

    custom_id = objeto.get("custom_id")
    return LineVerdict(ok=not findings,
                       custom_id=custom_id if isinstance(custom_id, str) else None,
                       findings=findings)


def validate_stream(lines: Iterable[str],
                    pol: Policy = DEFAULT) -> Tuple[Verdict, List[str], List[str]]:
    """Valida un stream de lineas. Devuelve (veredicto, limpias, rechazadas)."""
    veredicto = Verdict()
    clean_count: List[str] = []
    rejected_count: List[str] = []

    for numero, linea in enumerate(lines, 1):
        if not linea.strip():
            continue
        veredicto.n_lines += 1
        result = validate_line(linea, numero, pol)

        if result.ok:
            veredicto.n_ok += 1
            clean_count.append(linea if linea.endswith("\n") else linea + "\n")
        else:
            rejected_count.append(linea if linea.endswith("\n") else linea + "\n")
            veredicto.findings.extend(result.findings)
            for finding in result.findings:
                veredicto.stats[finding.rule] = veredicto.stats.get(finding.rule, 0) + 1

    return veredicto, clean_count, rejected_count


def aborts(veredicto: Verdict, pol: Policy = DEFAULT) -> bool:
    """Con max_findings = 0, cualquier hallazgo tumba el lote entero.

    Es semantica de tripwire: en este corpus el numero esperado de hallazgos es
    cero, asi que uno solo no es un caso rutinario que apartar — es la senal de
    que algo se rompio aguas arriba.
    """
    return len(veredicto.findings) > pol.max_findings


def severities(veredicto: Verdict) -> Dict[str, int]:
    """Cuantos hallazgos de cada severidad. Alimenta las DOS alarmas."""
    conteo: Dict[str, int] = {}
    for finding in veredicto.findings:
        conteo[finding.severity] = conteo.get(finding.severity, 0) + 1
    return conteo
