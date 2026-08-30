#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Casos de fallo OPERATIVOS contra el pipeline desplegado.
#
# Las suites de ejemplos/ cubren los fallos del filtro: un PAN, una evasion, un
# esquema mal. Esto cubre la otra familia, la que solo aparece con varios
# ficheros y un manifiesto de por medio:
#
#   · un fichero que no es JSON
#   · un envelope sin 'requests'
#   · un manifiesto ilegible
#   · un manifiesto que llega ANTES de que el sanitizer acabe
#   · un lote con una parte sucia -> no se envia NINGUNA parte
#   · el mismo custom_id en dos partes del lote
#
# Cada caso declara que deberia pasar y el script lo comprueba. No es una
# demostracion: si el sistema deja de comportarse asi, esto lo dice.
#
#   ./scripts/probar-fallos.sh dev            todos los cases
#   ./scripts/probar-fallos.sh dev 3          solo el caso 3
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/nombres.sh
source "${RAIZ}/scripts/lib/nombres.sh"

ENTORNO="${1:-}"
SOLO="${2:-}"

REGION="${AWS_REGION:-eu-south-2}"
ESPERA="${ESPERA:-70}"
ESPERA_LARGA="${ESPERA_LARGA:-360}"

if [[ -t 1 ]]; then
  R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
  VERDE=$'\033[32m'; AMBAR=$'\033[33m'; ROJO=$'\033[31m'; AZUL=$'\033[34m'
else
  R=""; B=""; D=""; VERDE=""; AMBAR=""; ROJO=""; AZUL=""
fi
caso()  { printf '\n%s==>%s %s%s%s\n' "$AZUL" "$R" "$B" "$*" "$R"; }
dato()  { printf '    %s·%s %s\n' "$D" "$R" "$*"; }
bien()  { printf '    %s✓%s %s\n' "$VERDE" "$R" "$*"; OK=$((OK + 1)); }
mal()   { printf '    %s✗%s %s\n' "$ROJO" "$R" "$*"; FALLOS=$((FALLOS + 1)); }
die()   { printf '\n%serror:%s %s\n' "$ROJO" "$R" "$*" >&2; exit 1; }

[[ "$ENTORNO" =~ ^(dev|qa)$ ]] || die "uso: $0 <dev|qa> [numero-de-caso]"

for bin in aws jq python3; do
  command -v "$bin" >/dev/null 2>&1 || die "falta '$bin'"
done

AWS=(aws --region "$REGION" --output json)
CUENTA="$("${AWS[@]}" sts get-caller-identity | jq -r .Account)" \
  || die "credenciales AWS invalidas o expiradas"

RAW="$(itl_bucket "$ENTORNO" raw)"
CLEAN="$(itl_bucket "$ENTORNO" clean)"
TABLA="$(itl_table "$ENTORNO")"
LOG_SUBMITTER="$(itl_log_group "$ENTORNO" submitter)"
SELLO="$(date +%Y%m%d-%H%M%S)"

#: las partes viven en raw, bajo input/. El manifiesto NO: va a clean.
lote_de() { printf '%sfallos/%s/%s' "$ITL_PREFIX_INPUT" "$SELLO" "$1"; }

OK=0; FALLOS=0; OMITIDOS=0

printf '%s==>%s %sDestino%s\n' "$AZUL" "$R" "$B" "$R"
dato "cuenta ${CUENTA} · region ${REGION} · entorno ${ENTORNO}"
dato "prefijo de esta tanda: $(lote_de '')"
printf '\n    %sOJO:%s varios cases disparan la alarma BatchesQuarantined.\n' "$AMBAR" "$R"
printf '    Es correcto, pero avisa a quien reciba las alarmas.\n'

# --- utilidades -------------------------------------------------------------

subir() {  # subir <clave-relativa> <fichero-o-'-'>   una parte, a raw
  local clave
  clave="$(lote_de "$1")"
  if [[ "$2" == "-" ]]; then
    "${AWS[@]}" s3api put-object --bucket "$RAW" --key "$clave" \
      --body /dev/stdin >/dev/null
  else
    "${AWS[@]}" s3api put-object --bucket "$RAW" --key "$clave" \
      --body "$2" >/dev/null
  fi
  echo "$clave"
}

subir_manifiesto() {  # subir_manifiesto <carpeta-relativa>   desde stdin, a clean
  local clave
  clave="$(lote_de "$1/${ITL_MANIFEST_SUFFIX}")"
  "${AWS[@]}" s3api put-object --bucket "$CLEAN" --key "$clave" \
    --body /dev/stdin --content-type application/json >/dev/null
  echo "$clave"
}

id_lote() {  # id_lote <clave>  -> el batch_id que calculara el sanitizer
  local etag
  etag="$("${AWS[@]}" s3api head-object --bucket "$RAW" --key "$1" \
          | jq -r .ETag | tr -d '"')"
  python3 -c "
import hashlib, sys
print('b_' + hashlib.sha256(f'{sys.argv[1]}/{sys.argv[2]}/{sys.argv[3]}'.encode()).hexdigest()[:24])
" "$RAW" "$1" "$etag"
}

# Espera a que aparezca el parte de estado de un fichero suelto.
esperar_parte() {  # esperar_parte <batch_id> [segundos]
  local lote="$1" limite="${2:-$ESPERA}" parte=""
  printf '    %sesperando%s' "$D" "$R"
  for _ in $(seq 1 "$limite"); do
    if parte="$("${AWS[@]}" s3 cp "s3://${CLEAN}/${ITL_PREFIX_STATUS}${lote}.json" - 2>/dev/null)"; then
      [[ -n "$parte" ]] && { printf '\n'; echo "$parte"; return 0; }
    fi
    printf '.'; sleep 1
  done
  printf '\n'; return 1
}

# Espera a que el item del lote alcance uno de los estados dados.
esperar_lote() {  # esperar_lote <carpeta> <estado1|estado2> [segundos]
  local carpeta="$1" quiero="$2" limite="${3:-$ESPERA}" item estado
  printf '    %sesperando%s' "$D" "$R"
  for _ in $(seq 1 "$limite"); do
    item="$("${AWS[@]}" dynamodb get-item --table-name "$TABLA" \
            --key "$(jq -nc --arg k "batch#${carpeta}" '{batch_id:{S:$k}}')" 2>/dev/null || echo '{}')"
    estado="$(jq -r '.Item.status.S // ""' <<<"$item")"
    if [[ -n "$estado" ]] && [[ "|$quiero|" == *"|$estado|"* ]]; then
      printf '\n'; echo "$item"; return 0
    fi
    printf '.'; sleep 3
  done
  printf '\n'
  # Se devuelve el ultimo item aunque no cuadre, para poder explicar el fallo.
  echo "$item"; return 1
}

peticion_json() {  # peticion_json <custom_id> <texto>
  jq -nc --arg id "$1" --arg t "$2" '{
    requests: [{custom_id: $id, params: {
      model: "claude-sonnet-4-5", max_tokens: 32,
      messages: [{role: "user", content: $t}]}}]}'
}

quiere_caso() { [[ -z "$SOLO" || "$SOLO" == "$1" ]]; }

# ===========================================================================
# 1 — un fichero que no es JSON
# ===========================================================================
if quiere_caso 1; then
  caso "1 · fichero que no es JSON"
  dato "esperado: cuarentena, motivo 'JSON invalido'"
  clave="$(printf 'esto no es json {{{' | subir "caso1/roto.json" -)"
  lote="$(id_lote "$clave")"
  if parte="$(esperar_parte "$lote")"; then
    estado="$(jq -r .status <<<"$parte")"
    motivo="$(jq -r .reason <<<"$parte")"
    [[ "$estado" == "quarantined" ]] && bien "quarantined" || mal "estado=${estado}"
    [[ "$motivo" == *"JSON invalido"* ]] \
      && bien "motivo lo explica: ${motivo:0:60}" \
      || mal "motivo inesperado: ${motivo:0:70}"
  else
    mal "sin parte de estado tras ${ESPERA}s"
  fi
fi

# ===========================================================================
# 2 — envelope sin 'requests'
# ===========================================================================
if quiere_caso 2; then
  caso "2 · envelope sin 'requests'"
  dato "esperado: cuarentena, motivo 'envelope invalido'"
  clave="$(printf '{"metadata":{"caso":"sin requests"}}' | subir "caso2/vacio.json" -)"
  lote="$(id_lote "$clave")"
  if parte="$(esperar_parte "$lote")"; then
    estado="$(jq -r .status <<<"$parte")"
    motivo="$(jq -r .reason <<<"$parte")"
    [[ "$estado" == "quarantined" ]] && bien "quarantined" || mal "estado=${estado}"
    [[ "$motivo" == *"envelope invalido"* ]] \
      && bien "motivo lo explica: ${motivo:0:60}" \
      || mal "motivo inesperado: ${motivo:0:70}"
  else
    mal "sin parte de estado tras ${ESPERA}s"
  fi
fi

# ===========================================================================
# 3 — manifiesto ilegible
# ===========================================================================
if quiere_caso 3; then
  caso "3 · manifiesto ilegible"
  dato "esperado: el lote queda failed, no revienta la Lambda"
  carpeta="$(lote_de "caso3")"
  peticion_json "c3-1" "Resume el expediente" | subir "caso3/parte-01.json" - >/dev/null
  printf '{no es json' | subir_manifiesto "caso3" >/dev/null
  if item="$(esperar_lote "$carpeta" "failed" 40)"; then
    bien "lote marcado fallido"
    dato "motivo: $(jq -r '.Item.reason.S // "-"' <<<"$item")"
  else
    mal "estado=$(jq -r '.Item.status.S // "sin item"' <<<"$item")"
  fi
fi

# ===========================================================================
# 4 — el manifiesto llega ANTES que las partes
# ===========================================================================
if quiere_caso 4; then
  caso "4 · manifiesto antes de que el sanitizer acabe"
  dato "esperado: awaiting_parts, y el barrido lo envia despues"
  carpeta="$(lote_de "caso4")"
  jq -nc '{batch:"caso4", files:["parte-01.json","parte-02.json"], total_requests:2}' \
    | subir_manifiesto "caso4" >/dev/null

  if item="$(esperar_lote "$carpeta" "awaiting_parts" 30)"; then
    bien "queda awaiting_parts"
    dato "limpias=$(jq -r '.Item.clean_parts.N // 0' <<<"$item")/$(jq -r '.Item.expected_parts.N // 0' <<<"$item")"
  else
    mal "estado=$(jq -r '.Item.status.S // "sin item"' <<<"$item")"
  fi

  dato "ahora se suben las dos partes"
  peticion_json "c4-1" "Clasifica la urgencia" | subir "caso4/parte-01.json" - >/dev/null
  peticion_json "c4-2" "Extrae la fecha"       | subir "caso4/parte-02.json" - >/dev/null

  dato "el barrido corre cada 5 min; esto puede tardar"
  if item="$(esperar_lote "$carpeta" "submitted" "$ESPERA_LARGA")"; then
    bien "el barrido lo recogio y lo envio"
    dato "batch_ids: $(jq -r '[.Item.batch_ids.L[]?.S] | join(", ")' <<<"$item")"
  else
    estado="$(jq -r '.Item.status.S // "sin item"' <<<"$item")"
    if [[ "$estado" == "awaiting_parts" ]]; then
      printf '    %s?%s sigue esperando tras %ss\n' "$AMBAR" "$R" "$ESPERA_LARGA"
      dato "si la regla de horario del submitter esta desactivada, es lo esperado"
      dato "muevelo a mano:  ./scripts/ciclo-manual.sh ${ENTORNO} submitter"
      OMITIDOS=$((OMITIDOS + 1))
    else
      mal "estado=${estado}"
    fi
  fi
fi

# ===========================================================================
# 5 — una parte sucia tumba el lote entero
# ===========================================================================
if quiere_caso 5; then
  caso "5 · una parte sucia: NO se envia ninguna parte"
  dato "esperado: lote en cuarentena aunque la otra parte este limpia"
  carpeta="$(lote_de "caso5")"
  peticion_json "c5-ok"  "Resume el expediente sin incidencias" \
    | subir "caso5/parte-01.json" - >/dev/null
  peticion_json "c5-mal" "El cliente pago con la 4111111111111111" \
    | subir "caso5/parte-02.json" - >/dev/null
  sleep 8
  jq -nc '{batch:"caso5", files:["parte-01.json","parte-02.json"], total_requests:2}' \
    | subir_manifiesto "caso5" >/dev/null

  if item="$(esperar_lote "$carpeta" "quarantined" 60)"; then
    bien "lote en cuarentena"
    dato "rechazadas=$(jq -r '.Item.rejected_parts.N // 0' <<<"$item") · limpias=$(jq -r '.Item.clean_parts.N // 0' <<<"$item")"
    ids="$(jq -r '[.Item.batch_ids.L[]?.S] | length' <<<"$item")"
    [[ "$ids" == "0" ]] \
      && bien "no se envio ningun batch a Anthropic" \
      || mal "se enviaron ${ids} batch(es): la parte limpia cruzo"
  else
    mal "estado=$(jq -r '.Item.status.S // "sin item"' <<<"$item")"
  fi
fi

# ===========================================================================
# 6 — el mismo custom_id en dos partes
# ===========================================================================
if quiere_caso 6; then
  caso "6 · custom_id duplicado entre partes"
  dato "esperado: lote failed nombrando el id, sin POST a Anthropic"
  carpeta="$(lote_de "caso6")"
  peticion_json "colision" "Primera aparicion del id" \
    | subir "caso6/parte-01.json" - >/dev/null
  peticion_json "colision" "Segunda aparicion del MISMO id" \
    | subir "caso6/parte-02.json" - >/dev/null
  sleep 8
  jq -nc '{batch:"caso6", files:["parte-01.json","parte-02.json"], total_requests:2}' \
    | subir_manifiesto "caso6" >/dev/null

  if item="$(esperar_lote "$carpeta" "failed" 60)"; then
    bien "lote failed"
    motivo="$(jq -r '.Item.reason.S // "-"' <<<"$item")"
    [[ "$motivo" == *"colision"* ]] \
      && bien "nombra el id duplicado: ${motivo}" \
      || mal "el motivo no dice cual: ${motivo}"
  else
    estado="$(jq -r '.Item.status.S // "sin item"' <<<"$item")"
    if [[ "$estado" == "awaiting_parts" || "$estado" == "ready" ]]; then
      printf '    %s?%s sigue en %s tras 60s\n' "$AMBAR" "$R" "$estado"
      dato "el fallo se detecta al ensamblar; muevelo:  ./scripts/ciclo-manual.sh ${ENTORNO} submitter"
      OMITIDOS=$((OMITIDOS + 1))
    else
      mal "estado=${estado}"
    fi
  fi
fi

# ===========================================================================
printf '\n%s==>%s %sResumen%s\n' "$AZUL" "$R" "$B" "$R"
printf '    %s%d comprobaciones bien%s' "$VERDE" "$OK" "$R"
[[ $FALLOS -gt 0 ]] && printf ' · %s%d mal%s' "$ROJO" "$FALLOS" "$R"
[[ $OMITIDOS -gt 0 ]] && printf ' · %s%d sin concluir%s' "$AMBAR" "$OMITIDOS" "$R"
printf '\n\n'

cat <<AYUDA
    ${B}Lo que esta bateria NO puede probar${R}

      · Fichero por encima de MAX_RAW_BYTES (100 MB). Subirlo cuesta mas de lo
        que aporta; la ruta esta cubierta en las pruebas unitarias.
      · Lote expirado a las 24 h. Habria que esperar 24 h.
      · Un hallazgo del verifier. Exigiria envenenar clean/ a mano, que es
        justo lo que su rol impide.
      · 429 de Anthropic. Necesita volumen real.

    ${B}Para limpiar lo que dejo esta tanda${R}

      aws s3 rm s3://${RAW}/$(lote_de '') --recursive --region ${REGION}
      aws s3 rm s3://${CLEAN}/$(lote_de '') --recursive --region ${REGION}

    ${B}Si algo quedo sin concluir${R}

      ./scripts/ciclo-manual.sh ${ENTORNO} submitter
      aws logs tail ${LOG_SUBMITTER} --since 10m --region ${REGION}

AYUDA

[[ $FALLOS -eq 0 ]] || exit 1
