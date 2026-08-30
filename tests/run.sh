#!/usr/bin/env bash
# Corre todas las suites. Necesita un interprete con las dependencias del layer.
#   python3 -m venv .venv && .venv/bin/pip install -r layer/requirements.txt boto3
#
# PYTHON admite las dos formas, y las dos se usan: en local una ruta al venv,
# y en CI el interprete que ya esta en el PATH. Se comprueba con 'command -v'
# y no con '-x' porque '-x python' pregunta por un fichero ./python en el
# directorio actual — que no existe, asi que CI moria diciendo "crea el venv"
# con el interprete perfectamente instalado.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
command -v "$PY" >/dev/null 2>&1 \
  || { echo "no se encuentra el interprete '$PY' — crea el venv (ver cabecera)"; exit 1; }
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
