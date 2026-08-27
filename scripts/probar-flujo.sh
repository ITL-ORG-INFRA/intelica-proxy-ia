#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Prueba el pipeline REAL: sube un lote a raw y espera el veredicto.
#
# Cierra el ciclo completo — S3 -> EventBridge -> sanitizer -> gate -> clean o
# cuarentena — y te devuelve el parte de estado sin que tengas que ir a buscarlo
# por la consola.
#
# El id del lote se calcula igual que lo hace el sanitizer (sha256 de
# bucket/key/etag), asi que se sabe donde mirar antes de que exista.
#
#   ./scripts/probar-flujo.sh dev                             todos los ejemplos
#   ./scripts/probar-flujo.sh dev ejemplos/02-pan-texto-libre.json   uno
#   ./scripts/probar-flujo.sh dev mi-lote.json                       el tuyo
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTORNO="${1:-}"
shift || true
FICHEROS=("$@")

PROYECTO="${PROYECTO:-intelica-proxy-ia}"
REGION="${AWS_REGION:-eu-south-2}"
ESPERA_MAX="${ESPERA_MAX:-90}"

if [[ -t 1 ]]; then
  R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
  VERDE=$'\033[32m'; AMBAR=$'\033[33m'; ROJO=$'\033[31m'; AZUL=$'\033[34m'
else
  R=""; B=""; D=""; VERDE=""; AMBAR=""; ROJO=""; AZUL=""
fi
paso() { printf '\n%s==>%s %s%s%s\n' "$AZUL" "$R" "$B" "$*" "$R"; }
dato() { printf '    %s·%s %s\n' "$D" "$R" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$ROJO" "$R" "$*" >&2; exit 1; }

[[ "$ENTORNO" =~ ^(dev|qa|prod)$ ]] || die "uso: $0 <dev|qa|prod> [fichero.json ...]"
[[ "$ENTORNO" == "prod" ]] && die "no se lanzan pruebas contra prod desde aqui"

for bin in aws jq python3; do
  command -v "$bin" >/dev/null 2>&1 || die "falta '$bin'"
done

AWS=(aws --region "$REGION" --output json)
CUENTA="$("${AWS[@]}" sts get-caller-identity | jq -r .Account)" \
  || die "credenciales AWS invalidas o expiradas"

P="${PROYECTO}-${ENTORNO}"
RAW="${P}-raw-${CUENTA}"
CLEAN="${P}-clean-${CUENTA}"
QUAR="${P}-quarantine-${CUENTA}"

if [[ ${#FICHEROS[@]} -eq 0 ]]; then
  # Sin argumentos: todos los ejemplos, en orden.
  while IFS= read -r f; do FICHEROS+=("$f"); done < <(find "${RAIZ}/ejemplos" -name '*.json' | sort)
fi
[[ ${#FICHEROS[@]} -gt 0 ]] || die "no hay ficheros que probar"

paso "Destino"
dato "cuenta ${CUENTA} · region ${REGION} · entorno ${ENTORNO}"
dato "raw:   s3://${RAW}"
dato "clean: s3://${CLEAN}"
printf '\n    %sOJO:%s los ejemplos con SAD disparan la alarma BloqueoDuro.\n' "$AMBAR" "$R"
printf '    Es correcto —el control funciona— pero avisa a quien reciba las alarmas.\n'

# El sanitizer deriva el id de bucket/key/etag. Se replica aqui para saber
# donde mirar antes incluso de que el parte exista.
id_lote() {
  python3 -c "
import hashlib, sys
print('b_' + hashlib.sha256(f'{sys.argv[1]}/{sys.argv[2]}/{sys.argv[3]}'.encode()).hexdigest()[:24])
" "$1" "$2" "$3"
}

limpios=0; cuarentena=0; sin_respuesta=0

for fichero in "${FICHEROS[@]}"; do
  [[ -f "$fichero" ]] || { printf '\n  %s✗%s no existe: %s\n' "$ROJO" "$R" "$fichero"; continue; }
  nombre="$(basename "$fichero")"

  jq empty "$fichero" 2>/dev/null || { printf '\n  %s✗%s %s no es JSON valido\n' "$ROJO" "$R" "$nombre"; continue; }

  paso "$nombre"
  caso="$(jq -r '.metadata.caso // ""' "$fichero")"
  [[ -n "$caso" ]] && dato "$caso"
  dato "peticiones: $(jq '.requests | length' "$fichero")"

  clave="entrada/prueba-$(date +%Y%m%d-%H%M%S)-${nombre}"
  "${AWS[@]}" s3api put-object --bucket "$RAW" --key "$clave" \
    --body "$fichero" --content-type application/json >/dev/null
  etag="$("${AWS[@]}" s3api head-object --bucket "$RAW" --key "$clave" | jq -r .ETag | tr -d '"')"
  lote="$(id_lote "$RAW" "$clave" "$etag")"
  dato "subido · batch_id ${lote}"

  # --- esperar al parte ---
  printf '    %sesperando%s' "$D" "$R"
  parte=""
  for _ in $(seq 1 "$ESPERA_MAX"); do
    # 's3 cp -' escribe solo el cuerpo; 's3api get-object' mezclaria el
    # cuerpo con los metadatos de la respuesta en la misma salida.
    if parte="$("${AWS[@]}" s3 cp "s3://${CLEAN}/estado/${lote}.json" - 2>/dev/null)"; then
      [[ -n "$parte" ]] && break
    fi
    printf '.'
    sleep 1
    parte=""
  done
  printf '\n'

  if [[ -z "$parte" ]]; then
    printf '    %s?%s sin parte tras %ss\n' "$AMBAR" "$R" "$ESPERA_MAX"
    dato "mira el log:  aws logs tail /aws/lambda/${P}-sanitizer --since 5m --region ${REGION}"
    sin_respuesta=$((sin_respuesta + 1))
    continue
  fi

  estado="$(jq -r '.estado' <<<"$parte")"

  if [[ "$estado" == "limpio" ]]; then
    printf '    %s✓ LIMPIO%s  %s\n' "$VERDE" "$R" "$(jq -c '.peticiones' <<<"$parte")"
    limpios=$((limpios + 1))
  else
    printf '    %s✗ CUARENTENA%s  %s\n' "$ROJO" "$R" "$(jq -c '.peticiones' <<<"$parte")"
    printf '      motivo: %s\n' "$(jq -r '.motivo' <<<"$parte")"
    jq -r '.resumen_por_capa | to_entries[] | "      \(.key): \(.value)"' <<<"$parte" 2>/dev/null || true
    jq -r '.rechazos[]? | "      requests[\(.indice)] " +
             (if .hallazgos then (.hallazgos[] | "capa \(.capa) \(.tipo) — \(.detalle) en \(.donde)")
              else .detalle end)' <<<"$parte" 2>/dev/null | head -6 || true
    jq -r '.que_hacer[]? | "      → \(.)"' <<<"$parte" 2>/dev/null || true
    cuarentena=$((cuarentena + 1))
  fi
  dato "parte completo: aws s3 cp s3://${CLEAN}/estado/${lote}.json - --region ${REGION}"
done

paso "Resumen"
printf '    %s%d limpios%s · %s%d en cuarentena%s · %d sin respuesta\n\n' \
  "$VERDE" "$limpios" "$R" "$ROJO" "$cuarentena" "$R" "$sin_respuesta"

cat <<AYUDA
    ${B}Para mirar mas a fondo${R}

      Log del sanitizer, en vivo:
        aws logs tail /aws/lambda/${P}-sanitizer --follow --region ${REGION}

      Informe de cuarentena (solo infra, esta dentro del CDE):
        aws s3 ls s3://${QUAR}/quarantine/ --region ${REGION}

      Estado de todos los lotes:
        aws dynamodb scan --table-name ${P}-batches \\
          --projection-expression "batch_id,#s,motivo" \\
          --expression-attribute-names '{"#s":"status"}' \\
          --region ${REGION} | jq -r '.Items[] | [.batch_id.S, .status.S, (.motivo.S // "")] | @tsv'

AYUDA
