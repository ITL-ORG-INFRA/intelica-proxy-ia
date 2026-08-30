#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Prepara el terreno para el renombrado: borra lo que haria fallar al apply.
#
# El problema que resuelve es concreto. Cuando Terraform pasa a la nomenclatura
# nueva, el plan es "destruir y crear" para casi todo. Y ese destroy FALLA a
# medias en tres sitios:
#
#   - un bucket con objetos dentro no se borra (force_destroy viene a false)
#   - una tabla con deletion_protection tampoco
#   - un log group que AWS creo solo no esta en el estado, y al crear el nuevo
#     Terraform choca con uno que ya existe
#
# Un apply que revienta a mitad deja el sistema MEDIO RENOMBRADO: la mitad de
# las Lambdas apuntando a buckets que ya no existen. Eso es peor que no haber
# empezado, y es lo que este script evita.
#
# Por defecto SOLO LISTA. Para borrar de verdad hay que pasar --borrar y
# teclear el nombre del entorno.
#
#   ./scripts/limpiar-recursos-viejos.sh dev              lista, no toca nada
#   ./scripts/limpiar-recursos-viejos.sh dev --borrar     borra lo que bloquea
#   ./scripts/limpiar-recursos-viejos.sh dev --borrar --todo   ademas lo que
#                                                              Terraform sabe
#                                                              destruir solo
#
# NUNCA borra: la clave KMS (solo su alias), el secreto de Anthropic, ni el rol
# de CI. Ver el apartado "Lo que este script no toca nunca" al final.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTORNO="${1:-}"; shift || true

BORRAR=0; TODO=0; FORZAR=0
for arg in "$@"; do
  case "$arg" in
    --borrar) BORRAR=1 ;;
    --todo)   TODO=1 ;;
    --forzar) FORZAR=1 ;;
    *) echo "opcion desconocida: $arg" >&2; exit 2 ;;
  esac
done

# shellcheck source=scripts/lib/nombres.sh
source "${RAIZ}/scripts/lib/nombres.sh"
# shellcheck source=scripts/lib/s3-conteo.sh
source "${RAIZ}/scripts/lib/s3-conteo.sh"

REGION="${AWS_REGION:-eu-south-2}"

if [[ -t 1 ]]; then
  R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
  VERDE=$'\033[32m'; AMBAR=$'\033[33m'; ROJO=$'\033[31m'; AZUL=$'\033[34m'
else
  R=""; B=""; D=""; VERDE=""; AMBAR=""; ROJO=""; AZUL=""
fi
paso() { printf '\n%s==>%s %s%s%s\n' "$AZUL" "$R" "$B" "$*" "$R"; }
dato() { printf '    %s·%s %s\n' "$D" "$R" "$*"; }
bien() { printf '    %s✓%s %s\n' "$VERDE" "$R" "$*"; }
ojo()  { printf '    %s!%s %s\n' "$AMBAR" "$R" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$ROJO" "$R" "$*" >&2; exit 1; }

[[ "$ENTORNO" =~ ^(dev|qa)$ ]] \
  || die "uso: $0 <dev|qa> [--borrar] [--todo] [--forzar]
       prod no se limpia desde aqui: alli el corte necesita ventana, drenaje y
       una decision humana, no un script."

for bin in aws jq; do command -v "$bin" >/dev/null 2>&1 || die "falta '$bin'"; done

AWS=(aws --region "$REGION" --output json)
CUENTA="$("${AWS[@]}" sts get-caller-identity | jq -r .Account)" \
  || die "credenciales AWS invalidas o expiradas"
QUIEN="$("${AWS[@]}" sts get-caller-identity | jq -r .Arn)"

P="$(itl_prefix "$ENTORNO")"
BUCKETS=()
for b in raw clean quarantine results; do
  BUCKETS+=("$(itl_bucket "$ENTORNO" "$b")")
done
TABLA="$(itl_table "$ENTORNO")"
FUNCIONES=("${ITL_FUNCTIONS[@]}")
ROLES=(sanitizer submitter verifier canary)
ALIAS=("$(itl_kms_alias "$ENTORNO" raw)" "$(itl_kms_alias "$ENTORNO" clean)")

REGISTRO="${RAIZ}/dist/limpieza-${ENTORNO}-$(date +%Y%m%dT%H%M%S).log"
mkdir -p "${RAIZ}/dist"
anota() { printf '%s\n' "$*" >> "$REGISTRO"; }

paso "Destino"
dato "cuenta ${CUENTA} · region ${REGION} · entorno ${ENTORNO}"
dato "identidad ${QUIEN}"
if [[ $BORRAR -eq 0 ]]; then
  printf '\n    %sModo listado.%s No se toca nada. Anade --borrar para ejecutar.\n' "$B" "$R"
else
  printf '\n    %sMODO BORRADO.%s Esto destruye datos de forma irreversible.\n' "$ROJO" "$R"
  [[ $TODO -eq 1 ]] && printf '    %sCon --todo:%s ademas Lambdas, roles, alias, reglas y alarmas.\n' "$ROJO" "$R"
  printf '\n    Escribe el nombre del entorno para confirmar: '
  read -r confirmacion
  [[ "$confirmacion" == "$ENTORNO" ]] \
    || die "escribiste '${confirmacion}', esperaba '${ENTORNO}'. No se ha tocado nada."
fi

# ---------------------------------------------------------------------------
# 0. Comprobacion previa: nada en vuelo
#
# Un lote en 'enviado' o 'terminado' esta EN ANTHROPIC. Se va a facturar y su
# resultado sigue disponible 29 dias. Si se borra la tabla, nadie lo recogera:
# la tabla nueva no sabra que existe. Es la unica perdida real de esta ventana.
# ---------------------------------------------------------------------------
paso "Comprobacion previa · lotes en vuelo"
EN_VUELO="$("${AWS[@]}" dynamodb scan --table-name "$TABLA" \
  --filter-expression "#s IN (:e, :t)" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":e":{"S":"submitted"},":t":{"S":"completed"}}' \
  --query 'Count' 2>/dev/null || echo "sin-tabla")"

if [[ "$EN_VUELO" == "sin-tabla" ]]; then
  ojo "la tabla ${TABLA} no existe o no se puede leer; sigo"
elif [[ "$EN_VUELO" == "0" ]]; then
  bien "no hay lotes en vuelo"
else
  ojo "${EN_VUELO} lote(s) en Anthropic ahora mismo"
  printf '\n    Esos lotes se estan facturando y su resultado se perderia: la\n'
  printf '    tabla nueva no sabra que existen. Espera a que lleguen a\n'
  printf '    "delivered" antes de cortar.\n\n'
  printf '      aws dynamodb scan --table-name %s --region %s \\\n' "$TABLA" "$REGION"
  printf "        --filter-expression '#s IN (:e, :t)' \\\\\n"
  printf "        --expression-attribute-names '{\"#s\":\"status\"}' \\\\\n"
  printf "        --expression-attribute-values '{\":e\":{\"S\":\"submitted\"},\":t\":{\"S\":\"completed\"}}'\n"
  [[ $FORZAR -eq 1 ]] || die "aborto. Si de verdad quieres perderlos, repite con --forzar"
  ojo "--forzar dado: sigo pese a los lotes en vuelo"
fi

# ---------------------------------------------------------------------------
# 1. Buckets S3 — EL bloqueante de verdad
# ---------------------------------------------------------------------------
paso "Buckets S3 · lo que hace fallar el destroy"

# Versiones Y marcas de borrado, en un solo objeto listo para delete-objects.
# Las dos listas pueden faltar por completo en la respuesta, de ahi el '// []'.
FILTRO_VERSIONES='{Objects: [((.Versions // []) + (.DeleteMarkers // []))[]
                             | {Key: .Key, VersionId: .VersionId}], Quiet: true}'

vaciar_bucket() {
  local bucket="$1" versionado lote borrados=0
  versionado="$("${AWS[@]}" s3api get-bucket-versioning --bucket "$bucket" \
    --query 'Status' --output text 2>/dev/null || echo None)"

  if [[ "$versionado" == "Enabled" || "$versionado" == "Suspended" ]]; then
    # Con versionado hay que borrar versiones Y marcas de borrado. Un
    # 's3 rm --recursive' deja las versiones vivas y el bucket sigue sin poder
    # borrarse, que es donde se atasca casi todo el mundo.
    while :; do
      # En jq y no en --query: JMESPath aqui es dificil de leer y de probar, y
      # esta consulta es la unica pieza del script cuyo fallo es silencioso —si
      # devuelve vacio de mas, el bucket parece vaciado y no lo esta.
      lote="$("${AWS[@]}" s3api list-object-versions --bucket "$bucket" --max-keys 1000 \
        2>/dev/null | jq -c "$FILTRO_VERSIONES" || echo '{"Objects":[],"Quiet":true}')"
      [[ "$(jq -r '.Objects | length' <<<"$lote")" -gt 0 ]] || break
      "${AWS[@]}" s3api delete-objects --bucket "$bucket" --delete "$lote" >/dev/null
      borrados=$(( borrados + $(jq -r '.Objects | length' <<<"$lote") ))
      printf '        %s… %s versiones borradas%s\r' "$D" "$borrados" "$R"
    done
    [[ $borrados -gt 0 ]] && printf '\n'
  else
    aws s3 rm "s3://${bucket}" --recursive --region "$REGION" --only-show-errors || true
  fi
  # Los multipart a medias tambien impiden borrar el bucket, y no se ven.
  "${AWS[@]}" s3api list-multipart-uploads --bucket "$bucket" \
    --query 'Uploads[].[Key,UploadId]' --output text 2>/dev/null \
    | while read -r k u; do
        [[ -n "${k:-}" ]] || continue
        "${AWS[@]}" s3api abort-multipart-upload --bucket "$bucket" --key "$k" --upload-id "$u" || true
      done
  echo "$borrados"
}

# Las dos paginaciones que usa s3_contar_bucket. Van contra la CLI de verdad;
# la prueba las sustituye por paginas fijas para poder comprobar el recuento
# sin AWS delante.
# '--no-paginate' es lo que hace que la respuesta llegue tal cual la da S3,
# con IsTruncated y los NextMarker. Sin el, la CLI pagina por su cuenta y
# devuelve un NextToken propio: los marcadores desaparecen y este bucle no
# tendria por donde seguir.
s3_pagina_versiones() {  # <bucket> <key-marker> <version-id-marker>
  local args=(s3api list-object-versions --bucket "$1" --max-keys 1000 --no-paginate)
  [[ -n "${2:-}" ]] && args+=(--key-marker "$2")
  [[ -n "${3:-}" ]] && args+=(--version-id-marker "$3")
  "${AWS[@]}" "${args[@]}" 2>/dev/null || echo '{}'
}

s3_pagina_multipart() {  # <bucket> <key-marker> <upload-id-marker>
  local args=(s3api list-multipart-uploads --bucket "$1" --max-uploads 1000 --no-paginate)
  [[ -n "${2:-}" ]] && args+=(--key-marker "$2")
  [[ -n "${3:-}" ]] && args+=(--upload-id-marker "$3")
  "${AWS[@]}" "${args[@]}" 2>/dev/null || echo '{}'
}

for bucket in "${BUCKETS[@]}"; do
  if ! "${AWS[@]}" s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    dato "${bucket} — no existe"
    continue
  fi
  if [[ $BORRAR -eq 0 ]]; then
    # Se cuentan las cuatro cosas por separado y con paginacion completa. Un
    # solo numero agregado fue lo que produjo el falso negativo: decia 0 y
    # despues S3 rechazaba el delete-bucket con BucketNotEmpty.
    read -r n_act n_ant n_mrk n_mpu <<<"$(s3_contar_bucket "$bucket")"
    total=$(( n_act + n_ant + n_mrk + n_mpu ))
    if [[ $total -eq 0 ]]; then
      bien "${bucket} — vacio, el destroy no se atascara"
    else
      ojo "${bucket} — ${total} cosa(s) que hay que vaciar"
      printf '        %sversiones actuales %s · no actuales %s · marcas de borrado %s · multipart a medias %s%s\n' \
        "$D" "$n_act" "$n_ant" "$n_mrk" "$n_mpu" "$R"
    fi
  else
    printf '    vaciando %s …\n' "$bucket"
    vaciar_bucket "$bucket" >/dev/null
    "${AWS[@]}" s3api delete-bucket --bucket "$bucket" >/dev/null 2>&1 \
      && { bien "${bucket} vaciado y borrado"; anota "s3 borrado ${bucket}"; } \
      || { bien "${bucket} vaciado (el bucket lo borrara Terraform)"; anota "s3 vaciado ${bucket}"; }
  fi
done

# ---------------------------------------------------------------------------
# 2. DynamoDB — la proteccion de borrado bloquea el destroy
# ---------------------------------------------------------------------------
paso "Tabla DynamoDB"
if PROT="$("${AWS[@]}" dynamodb describe-table --table-name "$TABLA" \
            --query 'Table.DeletionProtectionEnabled' --output text 2>/dev/null)"; then
  ITEMS="$("${AWS[@]}" dynamodb describe-table --table-name "$TABLA" \
            --query 'Table.ItemCount' --output text)"
  dato "${TABLA} — ~${ITEMS} items · deletion_protection=${PROT}"
  if [[ "$PROT" == "True" ]]; then
    if [[ $BORRAR -eq 0 ]]; then
      ojo "hay que desactivar deletion_protection o el destroy falla"
    else
      "${AWS[@]}" dynamodb update-table --table-name "$TABLA" \
        --no-deletion-protection-enabled >/dev/null
      bien "deletion_protection desactivada (la tabla la borrara Terraform)"
      anota "ddb deletion_protection off ${TABLA}"
    fi
  else
    bien "sin deletion_protection: Terraform puede destruirla"
  fi
  printf '\n    %sNo borro la tabla.%s Deja que lo haga Terraform, y conservala unos\n' "$B" "$R"
  printf '    dias: es el unico sitio donde queda constancia de lo de antes del corte.\n'
else
  dato "${TABLA} — no existe"
fi

# ---------------------------------------------------------------------------
# 3. Log groups huerfanos — bloquean la CREACION, no el borrado
#
# AWS crea el log group solo la primera vez que se invoca una Lambda. Ese no
# esta en el estado de Terraform. Si el nombre nuevo ya existe por eso,
# 'aws_cloudwatch_log_group' falla con ResourceAlreadyExistsException y el
# apply se para justo despues de haber creado media infraestructura.
# ---------------------------------------------------------------------------
paso "Log groups"
for f in "${FUNCIONES[@]}"; do
  lg="$(itl_log_group "$ENTORNO" "$f")"
  if "${AWS[@]}" logs describe-log-groups --log-group-name-prefix "$lg" \
       --query 'logGroups[?logGroupName==`'"$lg"'`]' | jq -e 'length > 0' >/dev/null 2>&1; then
    if [[ $BORRAR -eq 0 ]]; then
      ojo "${lg} — quedaria huerfano"
    else
      "${AWS[@]}" logs delete-log-group --log-group-name "$lg" >/dev/null 2>&1 \
        && { bien "${lg} borrado"; anota "logs borrado ${lg}"; } || ojo "${lg} no se pudo borrar"
    fi
  fi
done
printf '\n    %sSi quieres conservar los logs antes de borrarlos:%s\n' "$D" "$R"
printf '    %saws logs create-export-task --log-group-name … --destination <bucket>%s\n' "$D" "$R"

# ---------------------------------------------------------------------------
# 4. Lo que Terraform destruye solo — se lista, no se toca (salvo --todo)
# ---------------------------------------------------------------------------
paso "Lo que Terraform sabe destruir solo"
if [[ $TODO -eq 0 ]]; then
  dato "Lambdas:     ${FUNCIONES[*]}"
  dato "Roles IAM:   ${ROLES[*]}"
  dato "Alias KMS:   ${ALIAS[*]}"
  dato "Reglas EventBridge, alarmas y topic SNS con prefijo ${P}"
  printf '\n    Todo eso sale en el plan como destroy+create y no da problemas.\n'
  printf '    Borrarlo a mano solo crea deriva de estado. Si aun asi lo quieres\n'
  printf '    todo limpio de cero, repite con --todo.\n'
else
  ojo "--todo dado: borro tambien lo que Terraform habria destruido"
  for f in "${FUNCIONES[@]}"; do
    fn="$(itl_lambda "$ENTORNO" "$f")"
    "${AWS[@]}" lambda delete-function --function-name "$fn" >/dev/null 2>&1 \
      && { bien "lambda ${fn}"; anota "lambda borrada ${fn}"; } || true
  done
  for a in "${ALIAS[@]}"; do
    # SOLO el alias. La clave se queda: borrarla dejaria ilegible todo lo que
    # se cifro con ella, incluidos los objetos de otros entornos si se comparte.
    "${AWS[@]}" kms delete-alias --alias-name "$a" >/dev/null 2>&1 \
      && { bien "alias ${a} (la clave NO se toca)"; anota "kms alias borrado ${a}"; } || true
  done
  for r in "${ROLES[@]}"; do
    rol="$(itl_role "$ENTORNO" "$r")"
    "${AWS[@]}" iam list-attached-role-policies --role-name "$rol" \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null \
      | tr '\t' '\n' | while read -r arn; do
          [[ -n "${arn:-}" ]] && "${AWS[@]}" iam detach-role-policy --role-name "$rol" --policy-arn "$arn" || true
        done
    "${AWS[@]}" iam list-role-policies --role-name "$rol" \
      --query 'PolicyNames[]' --output text 2>/dev/null \
      | tr '\t' '\n' | while read -r pol; do
          [[ -n "${pol:-}" ]] && "${AWS[@]}" iam delete-role-policy --role-name "$rol" --policy-name "$pol" || true
        done
    "${AWS[@]}" iam delete-role --role-name "$rol" >/dev/null 2>&1 \
      && { bien "rol ${rol}"; anota "iam rol borrado ${rol}"; } || true
  done
  # Reglas de EventBridge: hay que quitar los targets ANTES o delete-rule falla.
  "${AWS[@]}" events list-rules --name-prefix "$P" --query 'Rules[].Name' --output text 2>/dev/null \
    | tr '\t' '\n' | while read -r regla; do
        [[ -n "${regla:-}" ]] || continue
        ids="$("${AWS[@]}" events list-targets-by-rule --rule "$regla" --query 'Targets[].Id' --output text 2>/dev/null | tr '\t' ' ')"
        [[ -n "${ids// }" ]] && "${AWS[@]}" events remove-targets --rule "$regla" --ids $ids >/dev/null 2>&1 || true
        "${AWS[@]}" events delete-rule --name "$regla" >/dev/null 2>&1 \
          && { bien "regla ${regla}"; anota "events regla borrada ${regla}"; } || true
      done
  alarmas="$("${AWS[@]}" cloudwatch describe-alarms --alarm-name-prefix "$P" \
              --query 'MetricAlarms[].AlarmName' --output text 2>/dev/null | tr '\t' ' ')"
  if [[ -n "${alarmas// }" ]]; then
    "${AWS[@]}" cloudwatch delete-alarms --alarm-names $alarmas >/dev/null 2>&1 \
      && { bien "$(wc -w <<<"$alarmas" | tr -d ' ') alarmas"; anota "cw alarmas borradas"; } || true
  fi
fi

# ---------------------------------------------------------------------------
paso "Lo que este script no toca nunca"
printf '    %sLas claves KMS.%s Solo se borra el alias. Destruir la clave dejaria\n' "$B" "$R"
printf '    ilegible todo lo cifrado con ella, y el borrado de una CMK tiene\n'
printf '    ademas una espera de 7 a 30 dias que no se puede deshacer.\n\n'
printf '    %sEl secreto de Anthropic.%s No cambia de nombre y volver a meter la\n' "$B" "$R"
printf '    clave exige que la teclee una persona.\n\n'
printf '    %sEl rol de CI (%s).%s Es el que usa GitHub para desplegar, y\n' \
  "$B" "$(itl_role "$ENTORNO" ci)" "$R"
printf '    puede ser el que estas usando ahora mismo. Que lo renombre Terraform,\n'
printf '    y acuerdate de actualizar AWS_ROLE_DEV en el entorno dev de GitHub.\n\n'
printf '    %sEl proveedor OIDC de la cuenta.%s Es compartido con otros proyectos.\n' "$B" "$R"

if [[ $BORRAR -eq 1 ]]; then
  paso "Registro"
  if [[ -s "$REGISTRO" ]]; then
    dato "$(wc -l <"$REGISTRO" | tr -d ' ') acciones anotadas en ${REGISTRO#$RAIZ/}"
  else
    dato "no hubo nada que borrar"
  fi
  printf '\n    Ahora si: aplica el Terraform con la nomenclatura nueva, y despues\n'
  printf '    despliega el codigo. El orden esta en docs/PROMPT-TERRAFORM-RENOMBRADO.md\n'
else
  paso "Siguiente paso"
  printf '    Repasa la lista de arriba. Cuando te cuadre:\n\n'
  printf '      ./scripts/limpiar-recursos-viejos.sh %s --borrar\n' "$ENTORNO"
fi
printf '\n'
