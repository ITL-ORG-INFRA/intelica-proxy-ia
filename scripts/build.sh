#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Construye los artefactos: un zip por Lambda mas el layer de dependencias.
#
# Las construcciones son REPRODUCIBLES: mismas fuentes -> mismo sha256. Sin eso
# no se puede afirmar que el artefacto que se aprobo en dev es el que entra en
# prod, y esa afirmacion es justo la que hace falta poder sostener.
#
#   ./scripts/build.sh              construye todo en dist/
#   ./scripts/build.sh --no-layer   solo el codigo (el layer tarda)
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${RAIZ}/dist"
TRABAJO="${RAIZ}/.build"

PY_RUNTIME="${PY_RUNTIME:-python3.13}"
ARQUITECTURA="${ARQUITECTURA:-arm64}"

CON_LAYER=true
[[ "${1:-}" == "--no-layer" ]] && CON_LAYER=false

ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '    \033[2m·\033[0m %s\n' "$*"; }
paso() { printf '\n\033[34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

for bin in zip python3 sha256sum; do
  command -v "$bin" >/dev/null 2>&1 || {
    [[ "$bin" == "sha256sum" ]] && command -v shasum >/dev/null 2>&1 && continue
    die "falta '$bin'"; }
done
sha() { command -v sha256sum >/dev/null 2>&1 && sha256sum "$1" | cut -d' ' -f1 \
        || shasum -a 256 "$1" | cut -d' ' -f1; }

#: fecha fija dentro de los zip. Sin esto el sha cambia en cada construccion
#: aunque el codigo sea identico, y la comparacion entre entornos no vale nada.
FECHA_FIJA="202601010000.00"

rm -rf "$DIST" "$TRABAJO"
mkdir -p "$DIST"

# --- funciones y lo que lleva cada una -------------------------------------
# Array indexado y un case, en vez de array asociativo: macOS trae bash 3.2 y
# ahi 'declare -A' no existe. El script tiene que correr igual en el portatil de
# quien lo usa que en el runner de CI.
FUNCIONES=(canary fetcher reconciler sanitizer submitter verifier)

# verifier y fetcher reutilizan los detectores del sanitizer.
carpetas_de() {
  case "$1" in
    canary)       echo "common canary" ;;
    fetcher)       echo "common sanitizer fetcher" ;;
    reconciler) echo "common reconciler" ;;
    sanitizer)     echo "common sanitizer" ;;
    submitter)     echo "common submitter" ;;
    verifier)   echo "common sanitizer verifier" ;;
    *) die "funcion desconocida: $1" ;;
  esac
}

paso "Empaquetando el codigo"
for funcion in "${FUNCIONES[@]}"; do
  read -ra carpetas <<< "$(carpetas_de "$funcion")"
  etapa="${TRABAJO}/${funcion}"
  mkdir -p "$etapa"
  for carpeta in "${carpetas[@]}"; do
    [[ -d "${RAIZ}/src/${carpeta}" ]] || die "no existe src/${carpeta}"
    cp -R "${RAIZ}/src/${carpeta}/." "$etapa/"
  done
  find "$etapa" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$etapa" -name '*.pyc' -delete 2>/dev/null || true

  # Fecha uniforme + orden estable + -X (sin metadatos de plataforma) = zip reproducible
  find "$etapa" -exec touch -t "$FECHA_FIJA" {} +
  ( cd "$etapa" && find . -type f | LC_ALL=C sort | zip -qX "${DIST}/${funcion}.zip" -@ )

  ok "$(printf '%-14s %8s  %s' "$funcion" \
        "$(du -h "${DIST}/${funcion}.zip" | cut -f1)" "$(sha "${DIST}/${funcion}.zip" | cut -c1-12)")"
done

# --- layer -----------------------------------------------------------------
if $CON_LAYER; then
  paso "Construyendo el layer"
  case "$ARQUITECTURA" in
    arm64)  PLATAFORMA="manylinux2014_aarch64" ;;
    x86_64) PLATAFORMA="manylinux2014_x86_64" ;;
    *) die "ARQUITECTURA debe ser arm64 o x86_64" ;;
  esac
  info "${PLATAFORMA} · ${PY_RUNTIME}"

  mkdir -p "${TRABAJO}/layer/python"
  python3 -m pip install \
    --requirement "${RAIZ}/layer/requirements.txt" \
    --target "${TRABAJO}/layer/python" \
    --platform "$PLATAFORMA" --python-version "${PY_RUNTIME#python}" \
    --implementation cp --only-binary=:all: --upgrade --quiet \
    || die "fallo el pip install del layer"

  find "${TRABAJO}/layer/python" -type d \
    \( -name '__pycache__' -o -name 'tests' -o -name '*.dist-info' \) \
    -prune -exec rm -rf {} + 2>/dev/null || true
  find "${TRABAJO}/layer" -exec touch -t "$FECHA_FIJA" {} +
  ( cd "${TRABAJO}/layer" && find . -type f | LC_ALL=C sort | zip -qX "${DIST}/layer.zip" -@ )

  ok "$(printf '%-14s %8s  %s' "layer" \
        "$(du -h "${DIST}/layer.zip" | cut -f1)" "$(sha "${DIST}/layer.zip" | cut -c1-12)")"
fi

# --- manifiesto ------------------------------------------------------------
paso "Manifiesto"
{
  echo "{"
  echo "  \"revision\": \"$(git -C "$RAIZ" rev-parse --verify HEAD 2>/dev/null || echo sin-git)\","
  echo "  \"requirements_sha\": \"$(sha "${RAIZ}/layer/requirements.txt")\","
  echo "  \"artefactos\": {"
  primero=true
  for zipfile in "${DIST}"/*.zip; do
    $primero || echo ","
    primero=false
    printf '    "%s": "%s"' "$(basename "$zipfile" .zip)" "$(sha "$zipfile")"
  done
  echo
  echo "  }"
  echo "}"
} > "${DIST}/manifiesto.json"
cat "${DIST}/manifiesto.json" | sed 's/^/    /'

rm -rf "$TRABAJO"
printf '\n    artefactos en dist/\n\n'
