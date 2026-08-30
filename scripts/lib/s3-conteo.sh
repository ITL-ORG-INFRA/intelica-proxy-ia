#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Contar lo que impide borrar un bucket de S3.
#
# Existe porque el modo listado de limpiar-recursos-viejos.sh daba un FALSO
# NEGATIVO: informaba de cero objetos y despues S3 rechazaba el borrado con
# BucketNotEmpty. Tres causas, las tres reales:
#
#   1. '--max-keys 1000' fija el tamano de pagina Y corta el recuento en la
#      primera pagina. Un bucket con 1.001 versiones se contaba como 1.000, y
#      uno cuya primera pagina venia vacia por los marcadores, como 0.
#   2. Los multipart uploads a medias no se contaban en absoluto, y son
#      invisibles: no salen en 'ls', no salen en list-object-versions, y
#      bastan por si solos para que el bucket no se pueda borrar.
#   3. Las versiones no actuales y las marcas de borrado se sumaban en el
#      mismo numero que las actuales, asi que no habia forma de ver por que
#      un bucket "vacio" no estaba vacio.
#
# La cuenta se separa en cuatro para que el listado diga lo que hay que
# borrar, no solo cuanto. Y la paginacion es explicita —con KeyMarker y
# VersionIdMarker— en vez de delegada en el auto-paginado de la CLI, porque
# asi se puede probar sin AWS delante: ver tests/limpieza_test.py.
#
# El listado se obtiene a traves de dos funciones indirectas que el llamante
# define. La prueba las sustituye por paginas fijas.
#
#   s3_pagina_versiones <bucket> <key-marker> <version-id-marker>  -> JSON
#   s3_pagina_multipart <bucket> <key-marker> <upload-id-marker>   -> JSON
# ---------------------------------------------------------------------------

#: tope de paginas. 1.000 paginas x 1.000 claves = un millon de versiones; si
#: un bucket de dev pasa de ahi, el problema no es el recuento.
S3_MAX_PAGINAS="${S3_MAX_PAGINAS:-1000}"

# s3_contar_bucket <bucket>
# Imprime: "<actuales> <no_actuales> <marcas_de_borrado> <multipart>"
s3_contar_bucket() {
  local bucket="$1"
  local actuales=0 no_actuales=0 marcas=0 multipart=0
  local key_marker="" version_marker="" upload_marker=""
  local pagina n paginas=0

  # --- versiones y marcas de borrado ---
  while :; do
    paginas=$((paginas + 1))
    [[ $paginas -gt $S3_MAX_PAGINAS ]] && break

    pagina="$(s3_pagina_versiones "$bucket" "$key_marker" "$version_marker")" || break
    [[ -n "$pagina" ]] || break

    n="$(jq -r '[(.Versions // [])[] | select(.IsLatest == true)] | length' <<<"$pagina")"
    actuales=$((actuales + n))
    n="$(jq -r '[(.Versions // [])[] | select(.IsLatest != true)] | length' <<<"$pagina")"
    no_actuales=$((no_actuales + n))
    n="$(jq -r '(.DeleteMarkers // []) | length' <<<"$pagina")"
    marcas=$((marcas + n))

    # IsTruncated es lo que dice si hay mas, no que la pagina venga llena:
    # una pagina puede volver vacia y seguir habiendo contenido detras.
    [[ "$(jq -r '.IsTruncated // false' <<<"$pagina")" == "true" ]] || break
    key_marker="$(jq -r '.NextKeyMarker // ""' <<<"$pagina")"
    version_marker="$(jq -r '.NextVersionIdMarker // ""' <<<"$pagina")"
    [[ -n "$key_marker" || -n "$version_marker" ]] || break
  done

  # --- multipart uploads a medias ---
  key_marker=""; paginas=0
  while :; do
    paginas=$((paginas + 1))
    [[ $paginas -gt $S3_MAX_PAGINAS ]] && break

    pagina="$(s3_pagina_multipart "$bucket" "$key_marker" "$upload_marker")" || break
    [[ -n "$pagina" ]] || break

    n="$(jq -r '(.Uploads // []) | length' <<<"$pagina")"
    multipart=$((multipart + n))

    [[ "$(jq -r '.IsTruncated // false' <<<"$pagina")" == "true" ]] || break
    key_marker="$(jq -r '.NextKeyMarker // ""' <<<"$pagina")"
    upload_marker="$(jq -r '.NextUploadIdMarker // ""' <<<"$pagina")"
    [[ -n "$key_marker" || -n "$upload_marker" ]] || break
  done

  printf '%s %s %s %s' "$actuales" "$no_actuales" "$marcas" "$multipart"
}
