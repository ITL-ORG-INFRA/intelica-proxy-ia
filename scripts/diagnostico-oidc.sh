#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Revisa el lado de AWS de la autenticacion OIDC con GitHub.
#
# El workflow .github/workflows/diagnostico-oidc.yml dice que claims MANDA
# GitHub. Este script dice que ESPERA AWS. Cuando el despliegue falla con
# "Assuming role with OIDC" reintentandose sin fin, la causa esta en que esos
# dos no coinciden, y hace falta ver los dos lados para saber cual mover.
#
# Solo lee. No modifica nada.
#
#   ./scripts/diagnostico-oidc.sh                     revisa los roles *-rol-ci
#   ./scripts/diagnostico-oidc.sh <nombre-del-rol>    revisa uno concreto
# ---------------------------------------------------------------------------
set -Eeuo pipefail

REGION="${AWS_REGION:-eu-south-2}"
PROYECTO="${PROYECTO:-intelica-proxy-ia}"
ROL_PEDIDO="${1:-}"

if [[ -t 1 ]]; then
  R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'
  VERDE=$'\033[32m'; AMBAR=$'\033[33m'; ROJO=$'\033[31m'; AZUL=$'\033[34m'
else
  R=""; B=""; D=""; VERDE=""; AMBAR=""; ROJO=""; AZUL=""
fi
paso() { printf '\n%s==>%s %s%s%s\n' "$AZUL" "$R" "$B" "$*" "$R"; }
ok()   { printf '    %s✓%s %s\n' "$VERDE" "$R" "$*"; }
mal()  { printf '    %s✗%s %s\n' "$ROJO" "$R" "$*"; }
avisa(){ printf '    %s!%s %s\n' "$AMBAR" "$R" "$*"; }
dato() { printf '    %s·%s %s\n' "$D" "$R" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$ROJO" "$R" "$*" >&2; exit 1; }

for bin in aws jq; do
  command -v "$bin" >/dev/null 2>&1 || die "falta '$bin' en el PATH"
done

AWS=(aws --output json)
LLAMANTE="$("${AWS[@]}" sts get-caller-identity)" || die "credenciales AWS invalidas o expiradas"
CUENTA="$(jq -r .Account <<<"$LLAMANTE")"

paso "Cuenta"
dato "cuenta ${CUENTA} · region ${REGION}"
dato "identidad: $(jq -r .Arn <<<"$LLAMANTE")"

PROBLEMAS=0

# --- 1. proveedores OIDC ---------------------------------------------------
paso "Proveedores OIDC de la cuenta"

PROVEEDORES="$("${AWS[@]}" iam list-open-id-connect-providers | jq -r '.OpenIDConnectProviderList[].Arn')"
if [[ -z "$PROVEEDORES" ]]; then
  mal "no hay ningun proveedor OIDC en la cuenta"
  die "sin proveedor, ningun token de GitHub puede asumir un rol"
fi

PROVEEDOR_GITHUB=""
while IFS= read -r arn; do
  [[ -z "$arn" ]] && continue
  detalle="$("${AWS[@]}" iam get-open-id-connect-provider --open-id-connect-provider-arn "$arn")"
  url="$(jq -r .Url <<<"$detalle")"
  audiencias="$(jq -r '.ClientIDList | join(", ")' <<<"$detalle")"
  huellas="$(jq -r '.ThumbprintList | length' <<<"$detalle")"

  if [[ "$url" == "token.actions.githubusercontent.com" ]]; then
    PROVEEDOR_GITHUB="$arn"
    ok "GitHub: ${arn}"
    dato "audiencias: ${audiencias:-<ninguna>}"
    dato "huellas registradas: ${huellas}"
    if [[ "$audiencias" == *"sts.amazonaws.com"* ]]; then
      ok "la audiencia incluye sts.amazonaws.com"
    else
      mal "la audiencia NO incluye sts.amazonaws.com"
      dato "la accion configure-aws-credentials pide exactamente esa audiencia"
      PROBLEMAS=$((PROBLEMAS + 1))
    fi
  else
    dato "otro proveedor: ${url}"
  fi
done <<< "$PROVEEDORES"

if [[ -z "$PROVEEDOR_GITHUB" ]]; then
  mal "no hay proveedor para token.actions.githubusercontent.com"
  PROBLEMAS=$((PROBLEMAS + 1))
fi

# --- 2. roles de CI --------------------------------------------------------
paso "Roles de CI"

if [[ -n "$ROL_PEDIDO" ]]; then
  ROLES="$ROL_PEDIDO"
else
  ROLES="$("${AWS[@]}" iam list-roles \
    | jq -r --arg p "$PROYECTO" '.Roles[].RoleName | select(startswith($p) and endswith("rol-ci"))')"
fi

if [[ -z "$ROLES" ]]; then
  avisa "no se encontro ningun rol ${PROYECTO}-*-rol-ci"
  dato "pasa el nombre como argumento: $0 <nombre-del-rol>"
  exit 1
fi

while IFS= read -r rol; do
  [[ -z "$rol" ]] && continue
  printf '\n  %s%s%s\n' "$B" "$rol" "$R"

  if ! info="$("${AWS[@]}" iam get-role --role-name "$rol" 2>/dev/null)"; then
    mal "no existe el rol ${rol}"
    PROBLEMAS=$((PROBLEMAS + 1)); continue
  fi

  dato "arn: $(jq -r .Role.Arn <<<"$info")"
  confianza="$(jq -r '.Role.AssumeRolePolicyDocument' <<<"$info")"

  # --- el proveedor que cita la trust policy existe? ---
  federado="$(jq -r '[.Statement[].Principal.Federated?] | flatten | map(select(.)) | .[0] // ""' <<<"$confianza")"
  if [[ -z "$federado" ]]; then
    mal "la politica de confianza no tiene un Principal.Federated"
    dato "quiza confia en un servicio o en una cuenta en vez de en OIDC"
    PROBLEMAS=$((PROBLEMAS + 1))
  elif [[ "$federado" == "$PROVEEDOR_GITHUB" ]]; then
    ok "confia en el proveedor de GitHub que existe en la cuenta"
  else
    mal "confia en un proveedor que NO coincide con el existente"
    dato "trust policy dice: ${federado}"
    dato "en la cuenta hay:  ${PROVEEDOR_GITHUB:-<ninguno de GitHub>}"
    dato "un ARN de proveedor inexistente falla igual que un sub mal puesto"
    PROBLEMAS=$((PROBLEMAS + 1))
  fi

  # --- accion ---
  acciones="$(jq -r '[.Statement[].Action] | flatten | join(", ")' <<<"$confianza")"
  if [[ "$acciones" == *"AssumeRoleWithWebIdentity"* ]]; then
    ok "permite sts:AssumeRoleWithWebIdentity"
  else
    mal "la accion no es sts:AssumeRoleWithWebIdentity (es: ${acciones})"
    dato "GitHub entra por WebIdentity, no por sts:AssumeRole"
    PROBLEMAS=$((PROBLEMAS + 1))
  fi

  # --- condiciones: aud y sub ---
  aud="$(jq -r '[.Statement[].Condition // {} | to_entries[].value | to_entries[]
                 | select(.key | endswith(":aud")) | .value] | flatten | join(", ")' <<<"$confianza")"
  [[ "$aud" == *"sts.amazonaws.com"* ]] \
    && ok "condicion aud = sts.amazonaws.com" \
    || { mal "condicion aud inesperada: ${aud:-<ninguna>}"; PROBLEMAS=$((PROBLEMAS + 1)); }

  printf '    %s·%s condiciones sobre sub:\n' "$D" "$R"
  jq -r '.Statement[].Condition // {} | to_entries[]
         | .key as $operador | .value | to_entries[]
         | select(.key | endswith(":sub"))
         | "        [\($operador)] \(.value | if type=="array" then join(" | ") else . end)"' \
    <<<"$confianza"

  # StringEquals exige coincidencia exacta; el sub real hay que leerlo del token.
  if jq -e '[.Statement[].Condition // {} | keys[]] | flatten | index("StringLike")' \
       <<<"$confianza" >/dev/null 2>&1; then
    dato "usa StringLike: admite comodines"
  else
    dato "usa StringEquals: el sub tiene que coincidir EXACTAMENTE"
    dato "compara ese valor con el que imprime el workflow Diagnostico OIDC"
  fi

  # --- tiene permisos, no solo confianza? ---
  inline="$("${AWS[@]}" iam list-role-policies --role-name "$rol" | jq -r '.PolicyNames | length')"
  gestionadas="$("${AWS[@]}" iam list-attached-role-policies --role-name "$rol" \
                 | jq -r '.AttachedPolicies | length')"
  if [[ "$inline" -eq 0 && "$gestionadas" -eq 0 ]]; then
    mal "el rol no tiene ninguna politica de permisos"
    dato "se podria asumir, pero no podria desplegar nada"
    PROBLEMAS=$((PROBLEMAS + 1))
  else
    ok "politicas: ${inline} inline, ${gestionadas} gestionadas"
  fi
done <<< "$ROLES"

# --- resumen ---------------------------------------------------------------
paso "Resultado"
if [[ $PROBLEMAS -eq 0 ]]; then
  ok "el lado de AWS esta bien montado"
  printf '    Si el despliegue sigue fallando, el desajuste esta en el valor de sub.\n'
  printf '    Corre el workflow %sDiagnostico OIDC%s en GitHub y compara.\n\n' "$B" "$R"
else
  mal "${PROBLEMAS} problema(s) encontrados arriba"
  printf '\n'
  exit 1
fi
