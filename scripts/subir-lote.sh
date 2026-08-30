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
# Las partes y el manifiesto NO van al mismo bucket:
#
#   partes      -> s3://<raw>/input/<lote>/parte-NN.json
#   manifiesto  -> s3://<clean>/input/<lote>/_MANIFEST.json
#
# El manifiesto no lleva datos, solo la lista de lo que compone el lote, asi
# que no tiene por que entrar en el CDE. Dejarlo fuera permite ademas que el
# productor lo escriba sin permiso de escritura sobre raw mas alla de las
# partes, y que el submitter —que no puede leer raw— lo lea por si mismo.
#
# Antes de subir se pasa el filtro REAL —el mismo codigo que corre en Lambda—
# sobre cada parte. Si alguna no cruzaria, no se sube ninguna: un lote es una
# unidad, asi que subir el resto solo deja partes sueltas en clean que no se
# enviaran nunca.
#
#   ./scripts/subir-lote.sh dev ./mi-carpeta
#   ./scripts/subir-lote.sh dev ./mi-carpeta lote-agosto     nombre del lote
#   ./scripts/subir-lote.sh dev ./mi-carpeta --solo-manifiesto   ver sin subir
#   ./scripts/subir-lote.sh dev ./mi-carpeta --sin-filtro    subir sin comprobar
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/nombres.sh
source "${RAIZ}/scripts/lib/nombres.sh"

ENTORNO="${1:-}"
CARPETA="${2:-}"
NOMBRE="${3:-}"

REGION="${AWS_REGION:-eu-south-2}"
PREFIJO="${PREFIJO:-${ITL_PREFIX_INPUT%/}}"

SOLO_MANIFIESTO=false
SIN_FILTRO=false
for arg in "$@"; do
  case "$arg" in
    --solo-manifiesto) SOLO_MANIFIESTO=true; [[ "$NOMBRE" == "$arg" ]] && NOMBRE="" ;;
    --sin-filtro)      SIN_FILTRO=true;      [[ "$NOMBRE" == "$arg" ]] && NOMBRE="" ;;
  esac
done

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

[[ "$ENTORNO" =~ ^(dev|qa|prod)$ ]] \
  || die "uso: $0 <dev|qa|prod> <carpeta> [nombre-del-lote] [--solo-manifiesto] [--sin-filtro]"
[[ -d "$CARPETA" ]] || die "no existe la carpeta: ${CARPETA:-<vacia>}"

for bin in aws jq python3; do
  command -v "$bin" >/dev/null 2>&1 || command -v "$bin" >/dev/null || die "falta '$bin'"
done

# --- que partes hay --------------------------------------------------------
PARTES=()
while IFS= read -r f; do PARTES+=("$f"); done < <(
  find "$CARPETA" -maxdepth 1 -type f -name '*.json' \
    ! -name "$ITL_MANIFEST_SUFFIX" ! -name 'manifiesto.json' | sort)

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

# --- las seis capas y el gate ----------------------------------------------
# Se pasa el filtro REAL —el mismo handler que corre en Lambda, contra un S3 y
# un DynamoDB simulados— antes de subir nada.
#
# No es un control de seguridad: raw esta DENTRO del CDE y esta hecho para
# recibir CHD; el canary planta PANes ahi a proposito cada hora. Es que subir
# un lote que va a acabar en cuarentena cuesta el viaje de ida, deja partes
# sueltas en clean que no se enviaran nunca, y dispara BatchesQuarantined a
# quien recibe las alarmas. Todo eso se sabe aqui, gratis y sin salir del
# portatil.
if $SIN_FILTRO; then
  paso "Filtro local"
  printf '    %s--sin-filtro: no se comprueba nada. Lo decide el sanitizer.%s\n' "$AMBAR" "$R"
else
  paso "Pasando el filtro local (las 6 capas y el gate)"
  FILTRO=""
  for candidato in "${RAIZ}/.venv/bin/python" python3 python; do
    command -v "$candidato" >/dev/null 2>&1 && { FILTRO="$candidato"; break; }
  done
  [[ -n "$FILTRO" ]] || die "no hay interprete de Python para pasar el filtro.
       Crea el venv (make venv) o repite con --sin-filtro."

  if "$FILTRO" -c 'import boto3' 2>/dev/null; then
    if "$FILTRO" "${RAIZ}/scripts/probar_filtro.py" "${PARTES[@]}" 2>&1 | sed 's/^/    /'; then
      ok "las ${#PARTES[@]} partes cruzarian a la zona limpia"
    else
      die "el filtro rechazaria alguna parte, y un lote es una unidad: si una
       cae, NO se envia ninguna. No se ha subido nada.

       Arriba tienes que peticion y que capa. Si quieres subirlo igualmente
       para ver el veredicto real, repite con --sin-filtro."
    fi
  else
    # Fallar en frio y no seguir a ciegas: si el filtro no puede correr, quien
    # sube tiene que saberlo y decidir, no enterarse por una alarma.
    die "el interprete '${FILTRO}' no tiene boto3, asi que no se puede pasar el
       filtro. Crea el venv (make venv) o repite con --sin-filtro."
  fi
fi

# --- el manifiesto ---------------------------------------------------------
MANIFIESTO="$(jq -nc \
  --arg batch "$NOMBRE" \
  --argjson files "$(printf '%s\n' "${NOMBRES[@]}" | jq -R . | jq -sc .)" \
  --argjson total "$TOTAL" \
  '{batch: $batch, files: $files, total_requests: $total}')"

paso "Manifiesto"
jq . <<<"$MANIFIESTO" | sed 's/^/    /'

if $SOLO_MANIFIESTO; then
  printf '\n    %s--solo-manifiesto: no se ha subido nada%s\n\n' "$AMBAR" "$R"
  exit 0
fi

# --- subir -----------------------------------------------------------------
AWS=(aws --region "$REGION" --output json)
# Los nombres de bucket ya no llevan el id de cuenta, pero la llamada se queda:
# es la forma barata de fallar aqui si las credenciales caducaron, en vez de a
# mitad de subida con medio lote arriba.
"${AWS[@]}" sts get-caller-identity >/dev/null \
  || die "credenciales AWS invalidas o expiradas"
RAW="$(itl_bucket "$ENTORNO" raw)"
CLEAN="$(itl_bucket "$ENTORNO" clean)"
TABLA="$(itl_table "$ENTORNO")"
DESTINO="${PREFIJO}/${NOMBRE}"

paso "Subiendo las partes a s3://${RAW}/${DESTINO}/"

for ruta in "${PARTES[@]}"; do
  base="$(basename "$ruta")"
  "${AWS[@]}" s3api put-object --bucket "$RAW" --key "${DESTINO}/${base}" \
    --body "$ruta" --content-type application/json >/dev/null
  ok "$base"
done

# El manifiesto AL FINAL, y en clean. Es la señal de "ya esta todo": subirlo
# antes no rompe nada —el lote queda esperando y el barrido lo recoge— pero
# retrasa el envio hasta el siguiente tick.
#
# La carpeta del lote es la misma clave en los dos buckets (input/<lote>), que
# es lo que permite al submitter emparejar el manifiesto con sus partes sin
# tener que leer raw.
paso "Subiendo el manifiesto a s3://${CLEAN}/${DESTINO}/"
MANIFEST_KEY="${DESTINO}/${ITL_MANIFEST_SUFFIX}"
echo "$MANIFIESTO" | "${AWS[@]}" s3api put-object --bucket "$CLEAN" \
  --key "$MANIFEST_KEY" --body /dev/stdin \
  --content-type application/json >/dev/null
printf '    %s✓%s %s  %s(dispara el envio)%s\n' \
  "$VERDE" "$R" "$ITL_MANIFEST_SUFFIX" "$D" "$R"

# --- que mirar -------------------------------------------------------------

paso "Seguimiento"
cat <<SEGUIR
    Estado del lote:

      aws dynamodb get-item --table-name ${TABLA} \\
        --key '{"batch_id":{"S":"batch#${DESTINO}"}}' --region ${REGION} \\
        | jq '.Item | {status:.status.S, clean:.clean_parts.N,
                       expected:.expected_parts.N,
                       batch_ids:[.batch_ids.L[]?.S], reason:.reason.S}'

    Parte de estado de cada fichero:

      aws s3 ls s3://${CLEAN}/${ITL_PREFIX_STATUS} --region ${REGION}

    ${D}submitted = ya esta en Anthropic · awaiting_parts = el sanitizer sigue
    quarantined = alguna parte fue rechazada, no se envio ninguna${R}

SEGUIR
