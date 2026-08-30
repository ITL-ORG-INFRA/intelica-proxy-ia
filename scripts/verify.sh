#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Compara lo que hay desplegado con lo que hay en dist/.
#
# Lambda expone CodeSha256, que es el sha256 del zip en base64. Es el mismo
# numero que podemos calcular aqui, asi que la comparacion es exacta: no
# "parece la misma version", es la misma o no lo es.
#
# Sirve para responder a la pregunta que siempre llega en la auditoria y en el
# incidente: "¿el codigo que hay en produccion es el que esta en el repo?".
#
#   ./scripts/verify.sh dev
#   ./scripts/verify.sh qa
#   ./scripts/verify.sh prod
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${RAIZ}/dist"

# shellcheck source=scripts/lib/nombres.sh
source "${RAIZ}/scripts/lib/nombres.sh"

ENTORNO="${1:-}"
REGION="${AWS_REGION:-eu-south-2}"

paso() { printf '\n\033[34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$ENTORNO" =~ ^(dev|qa|prod)$ ]] || die "uso: $0 <dev|qa|prod>"
[[ -d "$DIST" ]] || die "no existe dist/ — corre antes ./scripts/build.sh"

AWS=(aws --region "$REGION" --output json)
FUNCIONES=("${ITL_FUNCTIONS[@]}")

paso "Comparando ${ENTORNO} con dist/"
printf '    %-16s %-10s %s\n' "FUNCION" "ESTADO" "DETALLE"
printf '    %-16s %-10s %s\n' "----------------" "----------" "-------"

desviados=0
for f in "${FUNCIONES[@]}"; do
  nombre="$(itl_lambda "$ENTORNO" "$f")"
  zipfile="${DIST}/${f}.zip"

  if [[ ! -f "$zipfile" ]]; then
    printf '    %-16s \033[33m%-10s\033[0m %s\n' "$f" "SIN ZIP" "no esta en dist/"
    desviados=$((desviados + 1)); continue
  fi

  config="$("${AWS[@]}" lambda get-function-configuration --function-name "$nombre" 2>/dev/null)" || {
    printf '    %-16s \033[31m%-10s\033[0m %s\n' "$f" "NO EXISTE" "$nombre"
    desviados=$((desviados + 1)); continue; }

  esperado="$(openssl dgst -sha256 -binary "$zipfile" | openssl base64)"
  desplegado="$(jq -r .CodeSha256 <<<"$config")"

  if [[ "$esperado" == "$desplegado" ]]; then
    printf '    %-16s \033[32m%-10s\033[0m v%s · %s\n' "$f" "IGUAL" \
      "$(jq -r .Version <<<"$config")" "$(jq -r .LastModified <<<"$config" | cut -c1-19)"
  else
    printf '    %-16s \033[31m%-10s\033[0m desplegado=%s repo=%s\n' "$f" "DISTINTO" \
      "${desplegado:0:12}" "${esperado:0:12}"
    desviados=$((desviados + 1))
  fi
done

paso "Resultado"
if [[ $desviados -eq 0 ]]; then
  printf '    \033[32m✓\033[0m lo desplegado en %s coincide con este repo\n\n' "$ENTORNO"
else
  printf '    \033[31m✗\033[0m %d funcion(es) no coinciden\n' "$desviados"
  printf '      ./scripts/publish.sh %s   para alinearlas\n\n' "$ENTORNO"
  exit 1
fi
