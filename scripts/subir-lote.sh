#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Sube un lote de varias partes y genera su _MANIFEST.json.
#
# El manifiesto NO se escribe a mano. Su lista de 'files' tiene que coincidir
# exactamente con lo que se subio: si sobra un nombre el submitter espera una
# parte que no va a llegar, y si falta uno el lote se envia incompleto sin que
# nadie lo note. Ninguno de los dos errores da un mensaje claro, asi que se
# genera desde lo que realmente hay en la carpeta.
#
# El orden tambien importa: las partes primero, el manifiesto al final. Es lo
# que hace que el caso normal sea envio inmediato en vez de esperar al barrido.
#
#   ./scripts/subir-lote.sh dev ./mi-carpeta
#   ./scripts/subir-lote.sh dev ./mi-carpeta lote-agosto     nombre del lote
#   ./scripts/subir-lote.sh dev ./mi-carpeta --solo-manifiesto   ver sin subir
# ---------------------------------------------------------------------------
set -Eeuo pipefail

ENTORNO="${1:-}"
CARPETA="${2:-}"
NOMBRE="${3:-}"

PROYECTO="${PROYECTO:-intelica-proxy-ia}"
REGION="${AWS_REGION:-eu-south-2}"
PREFIJO="${PREFIJO:-entrada}"

SOLO_MANIFIESTO=false
[[ "$NOMBRE" == "--solo-manifiesto" ]] && { SOLO_MANIFIESTO=true; NOMBRE=""; }

if [[ -t 1 ]]; then
  R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
  VERDE=$'\033[32m'; AMBAR=$'\033[33m'; ROJO=$'\033[31m'; AZUL=$'\033[34m'
else
  R=""; B=""; D=""; VERDE=""; AMBAR=""; ROJO=""; AZUL=""
fi
paso() { printf '\n%s==>%s %s%s%s\n' "$AZUL" "$R" "$B" "$*" "$R"; }
ok()   { printf '    %s✓%s %s\n' "$VERDE" "$R" "$*"; }
dato() { printf '    %s·%s %s\n' "$D" "$R" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$ROJO" "$R" "$*" >&2; exit 1; }

[[ "$ENTORNO" =~ ^(dev|qa|prod)$ ]] || die "uso: $0 <dev|qa|prod> <carpeta> [nombre-del-lote]"
[[ -d "$CARPETA" ]] || die "no existe la carpeta: ${CARPETA:-<vacia>}"

for bin in aws jq python3; do
  command -v "$bin" >/dev/null 2>&1 || command -v "$bin" >/dev/null || die "falta '$bin'"
done

# --- que partes hay --------------------------------------------------------
PARTES=()
while IFS= read -r f; do PARTES+=("$f"); done < <(
  find "$CARPETA" -maxdepth 1 -type f -name '*.json' \
    ! -name '_MANIFEST.json' ! -name 'manifiesto.json' | sort)

[[ ${#PARTES[@]} -gt 0 ]] || die "no hay ficheros .json de datos en ${CARPETA}"

[[ -z "$NOMBRE" ]] && NOMBRE="lote-$(date +%Y-%m-%d-%H%M%S)"

paso "Revisando las partes"

# Se valida ANTES de subir nada. Un lote a medias con una parte mal formada
# obliga a limpiar el prefijo a mano y volver a empezar.
TOTAL=0
NOMBRES=()
PROBLEMAS=0
for ruta in "${PARTES[@]}"; do
  base="$(basename "$ruta")"
  if ! jq empty "$ruta" 2>/dev/null; then
    printf '    %s✗%s %-28s no es JSON valido\n' "$ROJO" "$R" "$base"
    PROBLEMAS=$((PROBLEMAS + 1)); continue
  fi
  n="$(jq '(.requests // []) | length' "$ruta")"
  if [[ "$n" -eq 0 ]]; then
    printf '    %s✗%s %-28s sin array "requests"\n' "$ROJO" "$R" "$base"
    PROBLEMAS=$((PROBLEMAS + 1)); continue
  fi
  bytes="$(wc -c < "$ruta" | tr -d ' ')"
  printf '    %s✓%s %-28s %5s peticiones  %8s\n' "$VERDE" "$R" "$base" "$n" \
    "$(python3 -c "print(f'{$bytes/1024:.1f} KB')")"
  TOTAL=$((TOTAL + n))
  NOMBRES+=("$base")
done

[[ $PROBLEMAS -eq 0 ]] || die "${PROBLEMAS} fichero(s) con problemas. No se sube nada."

# --- custom_id duplicados entre partes -------------------------------------
# Anthropic rechaza el POST entero sin decir cual esta repetido. El submitter
# lo detecta al ensamblar, pero para entonces ya se subio todo; mejor aqui.
paso "Comprobando custom_id"
DUPES="$(jq -r '.requests[].custom_id' "${PARTES[@]}" 2>/dev/null \
         | sort | uniq -d | head -5)"
if [[ -n "$DUPES" ]]; then
  printf '    %s✗%s custom_id repetidos entre partes:\n' "$ROJO" "$R"
  echo "$DUPES" | sed 's/^/        /'
  die "Anthropic rechazaria el lote entero. Corrigelos antes de subir."
fi
ok "$TOTAL peticiones, todos los custom_id unicos"

# --- el manifiesto ---------------------------------------------------------
MANIFIESTO="$(jq -nc \
  --arg lote "$NOMBRE" \
  --argjson files "$(printf '%s\n' "${NOMBRES[@]}" | jq -R . | jq -sc .)" \
  --argjson total "$TOTAL" \
  '{lote: $lote, files: $files, total_requests: $total}')"

paso "Manifiesto"
jq . <<<"$MANIFIESTO" | sed 's/^/    /'

if $SOLO_MANIFIESTO; then
  printf '\n    %s--solo-manifiesto: no se ha subido nada%s\n\n' "$AMBAR" "$R"
  exit 0
fi

# --- subir -----------------------------------------------------------------
AWS=(aws --region "$REGION" --output json)
CUENTA="$("${AWS[@]}" sts get-caller-identity | jq -r .Account)" \
  || die "credenciales AWS invalidas o expiradas"
RAW="${PROYECTO}-${ENTORNO}-raw-${CUENTA}"
DESTINO="${PREFIJO}/${NOMBRE}"

paso "Subiendo a s3://${RAW}/${DESTINO}/"

for ruta in "${PARTES[@]}"; do
  base="$(basename "$ruta")"
  "${AWS[@]}" s3api put-object --bucket "$RAW" --key "${DESTINO}/${base}" \
    --body "$ruta" --content-type application/json >/dev/null
  ok "$base"
done

# El manifiesto AL FINAL. Es la señal de "ya esta todo": subirlo antes no
# rompe nada —el lote queda esperando y el barrido lo recoge— pero retrasa el
# envio hasta el siguiente tick.
"${AWS[@]}" s3api put-object --bucket "$RAW" --key "${DESTINO}/_MANIFEST.json" \
  --body <(echo "$MANIFIESTO") --content-type application/json >/dev/null 2>&1 \
  || echo "$MANIFIESTO" | "${AWS[@]}" s3api put-object --bucket "$RAW" \
       --key "${DESTINO}/_MANIFEST.json" --body /dev/stdin >/dev/null
printf '    %s✓%s %s  %s(dispara el envio)%s\n' "$VERDE" "$R" "_MANIFEST.json" "$D" "$R"

# --- que mirar -------------------------------------------------------------
TABLA="${PROYECTO}-${ENTORNO}-batches"
CLEAN="${PROYECTO}-${ENTORNO}-clean-${CUENTA}"

paso "Seguimiento"
cat <<SEGUIR
    Estado del lote:

      aws dynamodb get-item --table-name ${TABLA} \\
        --key '{"batch_id":{"S":"lote#${DESTINO}"}}' --region ${REGION} \\
        | jq '.Item | {status:.status.S, limpias:.partes_limpias.N,
                       esperadas:.partes_esperadas.N,
                       batch_ids:[.batch_ids.L[]?.S], motivo:.motivo.S}'

    Parte de estado de cada fichero:

      aws s3 ls s3://${CLEAN}/estado/ --region ${REGION}

    ${D}enviado = ya esta en Anthropic · esperando_partes = el sanitizer sigue
    cuarentena = alguna parte fue rechazada, no se envio ninguna${R}

SEGUIR
