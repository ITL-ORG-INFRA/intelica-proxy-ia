#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Publica los artefactos de dist/ en un entorno.
#
# Este script toca EXCLUSIVAMENTE el codigo y el layer. No crea ni modifica
# buckets, roles, claves, tablas, reglas ni alarmas: de eso es dueno el repo de
# Terraform. Si algun dia hace falta cambiar memoria, timeout o una variable de
# entorno, se cambia alli, no aqui.
#
#   ./scripts/publish.sh dev
#   ./scripts/publish.sh qa
#   ./scripts/publish.sh prod
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${RAIZ}/dist"

ENTORNO="${1:-}"
PROYECTO="${PROYECTO:-intelica-proxy-ia}"
REGION="${AWS_REGION:-eu-south-2}"

ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '    \033[2m·\033[0m %s\n' "$*"; }
paso() { printf '\n\033[34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$ENTORNO" =~ ^(dev|qa|prod)$ ]] || die "uso: $0 <dev|qa|prod>"
[[ -d "$DIST" ]] || die "no existe dist/ — corre antes ./scripts/build.sh"
[[ -f "${DIST}/manifiesto.json" ]] || die "falta dist/manifiesto.json"

AWS=(aws --region "$REGION" --output json)
LLAMANTE="$("${AWS[@]}" sts get-caller-identity)" || die "credenciales AWS invalidas"
CUENTA="$(jq -r .Account <<<"$LLAMANTE")"

P="${PROYECTO}-${ENTORNO}"
FUNCIONES=(sanitizer verifier submitter reconciler fetcher canary)

paso "Destino"
info "cuenta ${CUENTA} · region ${REGION} · entorno ${ENTORNO}"
info "identidad: $(jq -r .Arn <<<"$LLAMANTE")"
info "revision:  $(jq -r .revision "${DIST}/manifiesto.json")"

# Antes de tocar nada: que las seis funciones existan. Si falta alguna, es que
# Terraform no ha corrido o el entorno esta a medias, y publicar a medias deja
# el sistema con unas Lambdas nuevas y otras viejas.
paso "Comprobando que el entorno existe"
faltan=""
for f in "${FUNCIONES[@]}"; do
  "${AWS[@]}" lambda get-function --function-name "${P}-${f}" >/dev/null 2>&1 || faltan="${faltan} ${P}-${f}"
done
[[ -z "$faltan" ]] || die "estas funciones no existen en AWS:${faltan}
Despliega primero la infraestructura desde el repo de Terraform."
ok "las 6 funciones existen"

# --- layer -----------------------------------------------------------------
LAYER="${P}-deps"
REQ_SHA="$(jq -r .requirements_sha "${DIST}/manifiesto.json")"
LAYER_ARN=""

paso "Layer · ${LAYER}"
if LISTA="$("${AWS[@]}" lambda list-layer-versions --layer-name "$LAYER" 2>/dev/null)"; then
  ACTUAL_ARN="$(jq -r '.LayerVersions[0].LayerVersionArn // empty' <<<"$LISTA")"
  ACTUAL_DESC="$(jq -r '.LayerVersions[0].Description // empty' <<<"$LISTA")"
else
  ACTUAL_ARN=""; ACTUAL_DESC=""
fi

if [[ "$ACTUAL_DESC" == "reqs:${REQ_SHA}" ]]; then
  LAYER_ARN="$ACTUAL_ARN"
  ok "dependencias sin cambios, se reutiliza la version $(jq -r '.LayerVersions[0].Version' <<<"$LISTA")"
elif [[ -f "${DIST}/layer.zip" ]]; then
  LAYER_ARN="$("${AWS[@]}" lambda publish-layer-version --layer-name "$LAYER" \
    --description "reqs:${REQ_SHA}" --zip-file "fileb://${DIST}/layer.zip" \
    --compatible-runtimes python3.13 --compatible-architectures arm64 \
    | jq -r .LayerVersionArn)"
  ok "publicada version ${LAYER_ARN##*:}"
else
  [[ -n "$ACTUAL_ARN" ]] || die "las dependencias cambiaron y no hay dist/layer.zip
Corre ./scripts/build.sh (sin --no-layer)."
  LAYER_ARN="$ACTUAL_ARN"
  info "sin layer.zip en dist/: se mantiene la version desplegada"
fi

# --- codigo ----------------------------------------------------------------
paso "Publicando codigo"
sha_lambda() {  # el CodeSha256 de Lambda es el sha256 del zip en base64
  openssl dgst -sha256 -binary "$1" | openssl base64
}

for f in "${FUNCIONES[@]}"; do
  nombre="${P}-${f}"
  zipfile="${DIST}/${f}.zip"
  [[ -f "$zipfile" ]] || die "falta ${zipfile}"

  esperado="$(sha_lambda "$zipfile")"
  desplegado="$("${AWS[@]}" lambda get-function-configuration --function-name "$nombre" \
                | jq -r .CodeSha256)"

  if [[ "$esperado" == "$desplegado" ]]; then
    capas="$("${AWS[@]}" lambda get-function-configuration --function-name "$nombre" \
             | jq -r '[.Layers[]?.Arn] | join(",")')"
    if [[ "$capas" == "$LAYER_ARN" ]]; then
      info "$(printf '%-14s sin cambios' "$f")"
      continue
    fi
  fi

  "${AWS[@]}" lambda update-function-code --function-name "$nombre" \
    --zip-file "fileb://${zipfile}" --publish >/dev/null
  "${AWS[@]}" lambda wait function-updated-v2 --function-name "$nombre"

  # Solo se toca el layer. Memoria, timeout y variables son de Terraform.
  "${AWS[@]}" lambda update-function-configuration --function-name "$nombre" \
    --layers "$LAYER_ARN" >/dev/null
  "${AWS[@]}" lambda wait function-updated-v2 --function-name "$nombre"

  version="$("${AWS[@]}" lambda get-function-configuration --function-name "$nombre" \
             | jq -r .Version)"
  ok "$(printf '%-14s actualizada (v%s)' "$f" "$version")"
done

paso "Publicado en ${ENTORNO}"
printf '    Comprueba que lo desplegado coincide con dist/:\n'
printf '      ./scripts/verify.sh %s\n\n' "$ENTORNO"
