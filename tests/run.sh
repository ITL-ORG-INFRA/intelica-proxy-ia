#!/usr/bin/env bash
# Corre las tres pruebas. Necesita un venv con las dependencias del layer.
#   python3 -m venv .venv && .venv/bin/pip install -r layer/requirements.txt boto3
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
[[ -x "$PY" ]] || { echo "no existe $PY — crea el venv (ver cabecera)"; exit 1; }
fallos=0
for prueba in tests/detectores_test.py tests/detection2_test.py \
              tests/envelope_test.py tests/fixtures_test.py \
              tests/e2e_test.py tests/manifiesto_test.py \
              tests/nombres_test.py tests/contrato_test.py \
              tests/limpieza_test.py; do
  echo; echo "=============== $prueba ==============="
  "$PY" "$prueba" || fallos=$((fallos+1))
done
echo; [[ $fallos -eq 0 ]] && echo "TODAS LAS PRUEBAS OK" || { echo "$fallos ficheros con fallos"; exit 1; }
