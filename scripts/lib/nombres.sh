#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# La UNICA construccion de nombres de recursos AWS de este repo.
#
# La convencion es la de la cuenta:
#
#     itl-<assetid>-<app>-<environment>-<type>-<descriptor>-<stack>
#      │      │       │         │          │         │         │
#      │      │       │         │          │         │         └─ secuencia
#      │      │       │         │          │         └─────────── descriptor
#      │      │       │         │          └───────────────────── tipo
#      │      │       │         └──────────────────────────────── entorno
#      │      │       └────────────────────────────────────────── aplicacion
#      │      └────────────────────────────────────────────────── asset id
#      └───────────────────────────────────────────────────────── organizacion
#
#     itl-0003-proxy-ia-dev-lambda-sanitizer-03
#
# Esta aqui y no repartido por los scripts porque la forma anterior
# —"${PROYECTO}-${ENTORNO}-${funcion}" concatenado en cada fichero— ya se
# habia desincronizado una vez: un script apuntaba a un recurso y el de al
# lado a otro, y eso no da un error, da un "no existe" que parece otra cosa.
#
# Se usa asi:
#
#     source "$(dirname "${BASH_SOURCE[0]}")/lib/nombres.sh"
#     itl_lambda dev sanitizer   ->  itl-0003-proxy-ia-dev-lambda-sanitizer-03
#
# Todo es sobreescribible por entorno con variables, para no tener que tocar
# el codigo si qa o prod salen con otra secuencia:
#
#     ITL_ORG ITL_ASSET_ID ITL_APP ITL_STACK
# ---------------------------------------------------------------------------

# Esto es una libreria: casi todo lo que define lo consume quien la carga, no
# ella misma. shellcheck no puede saberlo mirando este fichero solo.
# shellcheck disable=SC2034

ITL_ORG="${ITL_ORG:-itl}"
ITL_ASSET_ID="${ITL_ASSET_ID:-0003}"
ITL_APP="${ITL_APP:-proxy-ia}"
ITL_STACK="${ITL_STACK:-03}"

# --- prefijos de S3 --------------------------------------------------------
# Tambien viven aqui: son parte del contrato con los productores igual que los
# nombres de bucket, y tenerlos sueltos en cada script es como tenerlos mal.
ITL_PREFIX_INPUT="input/"
ITL_PREFIX_STATUS="status/"
ITL_PREFIX_CANARY="canary/"
ITL_PREFIX_CLEAN="clean/"
ITL_PREFIX_RESULTS="results/"
ITL_PREFIX_QUARANTINE="quarantine/"

#: sufijo que cierra un lote de varias partes
ITL_MANIFEST_SUFFIX="_MANIFEST.json"

#: las seis funciones, en el orden del pipeline
ITL_FUNCTIONS=(sanitizer verifier submitter reconciler fetcher canary)

# itl_name <environment> <type> <descriptor>
itl_name() {
  local environment="$1" type="$2" descriptor="$3"
  printf '%s-%s-%s-%s-%s-%s-%s' \
    "$ITL_ORG" "$ITL_ASSET_ID" "$ITL_APP" "$environment" \
    "$type" "$descriptor" "$ITL_STACK"
}

itl_lambda()    { itl_name "$1" lambda "$2"; }
itl_layer()     { itl_name "$1" lambda deps; }
itl_bucket()    { itl_name "$1" s3 "$2"; }
itl_table()     { itl_name "$1" ddb batches; }
itl_role()      { itl_name "$1" role "$2"; }
itl_topic()     { itl_name "$1" sns alarms; }
itl_rule()      { itl_name "$1" evb "$2"; }
itl_kms_alias() { printf 'alias/%s' "$(itl_name "$1" kms "$2")"; }

# El log group lo nombra AWS a partir del nombre de la funcion.
itl_log_group() { printf '/aws/lambda/%s' "$(itl_lambda "$1" "$2")"; }

# Prefijo comun a todos los recursos de un entorno. Sirve para los --*-prefix
# de la CLI (events list-rules, cloudwatch describe-alarms), no para construir
# nombres: para eso estan las funciones de arriba.
itl_prefix() {
  printf '%s-%s-%s-%s-' "$ITL_ORG" "$ITL_ASSET_ID" "$ITL_APP" "$1"
}
