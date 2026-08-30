#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Mueve el ciclo a mano cuando las reglas de horario estan desactivadas.
#
# El sanitizer y el verifier se disparan por eventos de S3, asi que siguen
# funcionando solos. Los otros cuatro dependen de EventBridge por horario: si
# esas reglas estan apagadas, un lote limpio se queda en 'verificado' para
# siempre y da la impresion de que algo se rompio.
#
# Esto invoca las cuatro en el orden correcto y enseña lo que devuelve cada
# una. Util en dev mientras los horarios estan apagados, y para depurar sin
# esperar al proximo tick.
#
#   ./scripts/ciclo-manual.sh dev              submitter, reconciler, fetcher
#   ./scripts/ciclo-manual.sh dev --con-canary  incluye el canary
#   ./scripts/ciclo-manual.sh dev submitter      solo una
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/nombres.sh
source "${RAIZ}/scripts/lib/nombres.sh"

ENTORNO="${1:-}"
shift || true

REGION="${AWS_REGION:-eu-south-2}"

if [[ -t 1 ]]; then
  R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
  VERDE=$'\033[32m'; AMBAR=$'\033[33m'; ROJO=$'\033[31m'; AZUL=$'\033[34m'
else
  R=""; B=""; D=""; VERDE=""; AMBAR=""; ROJO=""; AZUL=""
fi
paso() { printf '\n%s==>%s %s%s%s\n' "$AZUL" "$R" "$B" "$*" "$R"; }
dato() { printf '    %s·%s %s\n' "$D" "$R" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$ROJO" "$R" "$*" >&2; exit 1; }

[[ "$ENTORNO" =~ ^(dev|qa)$ ]] || die "uso: $0 <dev|qa> [--con-canary|<lambda>]"

for bin in aws jq; do
  command -v "$bin" >/dev/null 2>&1 || die "falta '$bin'"
done

AWS=(aws --region "$REGION" --output json)
"${AWS[@]}" sts get-caller-identity >/dev/null || die "credenciales AWS invalidas"

PREFIJO="$(itl_prefix "$ENTORNO")"
TABLA="$(itl_table "$ENTORNO")"

# El orden importa: el submitter manda lo que el verifier aprobo, el
# reconciler marca lo que Anthropic termino, y el fetcher baja eso ultimo.
# Invocarlas al reves solo desperdicia un ciclo.
SECUENCIA=(submitter reconciler fetcher)
case "${1:-}" in
  --con-canary) SECUENCIA=(canary submitter reconciler fetcher) ;;
  "") ;;
  *) SECUENCIA=("$1") ;;
esac

paso "Estado de las reglas de EventBridge"
"${AWS[@]}" events list-rules --name-prefix "$PREFIJO" \
  --query 'Rules[].[Name,State]' --output text 2>/dev/null \
  | while IFS=$'\t' read -r nombre estado; do
      if [[ "$estado" == "ENABLED" ]]; then
        printf '    %s✓%s %-42s %s\n' "$VERDE" "$R" "$nombre" "$estado"
      else
        printf '    %s·%s %-42s %s\n' "$AMBAR" "$R" "$nombre" "$estado"
      fi
    done || dato "no se pudieron listar las reglas"

for lambda in "${SECUENCIA[@]}"; do
  nombre="$(itl_lambda "$ENTORNO" "$lambda")"
  paso "$nombre"

  salida="$(mktemp)"
  if respuesta="$("${AWS[@]}" lambda invoke --function-name "$nombre" \
        --payload '{"source":"ciclo-manual"}' --cli-binary-format raw-in-base64-out \
        "$salida" 2>&1)"; then
    error="$(jq -r '.FunctionError // empty' <<<"$respuesta")"
    if [[ -n "$error" ]]; then
      printf '    %s✗%s la funcion fallo (%s)\n' "$ROJO" "$R" "$error"
      jq -r '.errorMessage // .' "$salida" 2>/dev/null | head -5 | sed 's/^/      /'
    else
      printf '    %s✓%s ' "$VERDE" "$R"
      jq -c '.' "$salida" 2>/dev/null || cat "$salida"
    fi
  else
    printf '    %s✗%s no se pudo invocar\n' "$ROJO" "$R"
    echo "$respuesta" | head -3 | sed 's/^/      /'
  fi
  rm -f "$salida"
done

paso "Estado de los lotes"
"${AWS[@]}" dynamodb scan --table-name "$TABLA" \
  --projection-expression "batch_id,#s" \
  --expression-attribute-names '{"#s":"status"}' 2>/dev/null \
  | jq -r '.Items[]? | .status.S // "sin-estado"' | sort | uniq -c \
  | while read -r cuantos estado; do
      printf '    %-14s %s\n' "$estado" "$cuantos"
    done || dato "no se pudo leer la tabla"

cat <<AYUDA

    ${D}Un lote recorre: received -> clean -> verified -> submitted ->
    completed -> delivered. Si se queda en 'verified', falta el submitter;
    si se queda en 'completed', falta el fetcher.${R}

AYUDA
