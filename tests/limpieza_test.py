"""El recuento de lo que impide borrar un bucket, con paginacion.

Existe por un fallo concreto: limpiar-recursos-viejos.sh informo de CERO
objetos y versiones, y despues S3 rechazo el borrado con BucketNotEmpty. Un
falso negativo asi es peor que no tener el script — se aplica el Terraform
confiando en que el terreno esta despejado, el destroy revienta a mitad y deja
la infraestructura medio renombrada.

Se prueba la funcion de shell de verdad, sustituyendo solo las dos llamadas a
la CLI por paginas fijas. Asi se ejercita la paginacion —que es donde estaba
el fallo— sin AWS delante.

    .venv/bin/python tests/limpieza_test.py
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO, "scripts", "lib", "s3-conteo.sh")

FAILURES = []

#: El doble de la CLI. Cada llamada consume la siguiente pagina del directorio;
#: cuando se acaban devuelve vacio, que es como se ve "no hay mas".
FALSA_CLI = """
source "%(lib)s"
CARPETA="%(carpeta)s"
echo 0 > "$CARPETA/nv"; echo 0 > "$CARPETA/nm"

siguiente() {           # siguiente <prefijo> <fichero-contador>
  local i; i="$(cat "$CARPETA/$2")"
  echo $((i + 1)) > "$CARPETA/$2"
  if [[ -f "$CARPETA/$1$i.json" ]]; then cat "$CARPETA/$1$i.json"; fi
}
s3_pagina_versiones() { siguiente v nv; }
s3_pagina_multipart()  { siguiente m nm; }
s3_contar_bucket un-bucket
"""


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


def contar(paginas_versiones, paginas_multipart=None):
    """Corre s3_contar_bucket con estas paginas y devuelve los cuatro numeros.

    Las paginas se dejan en ficheros numerados y las funciones falsas hacen
    'cat' de la que toca. Pasarlas por la linea de ordenes las destrozaria:
    son JSON con comillas y espacios dentro.
    """
    if paginas_multipart is None:
        paginas_multipart = [{}]

    with tempfile.TemporaryDirectory() as carpeta:
        for prefijo, paginas in (("v", paginas_versiones), ("m", paginas_multipart)):
            for i, pagina in enumerate(paginas):
                ruta = os.path.join(carpeta, f"{prefijo}{i}.json")
                with open(ruta, "w", encoding="utf-8") as fichero:
                    json.dump(pagina, fichero)

        out = subprocess.run(
            ["bash", "-c", FALSA_CLI % {"lib": LIB, "carpeta": carpeta}],
            capture_output=True, text=True, check=False)

    if out.returncode != 0:
        return f"<rc={out.returncode} {out.stderr.strip()}>"
    return out.stdout.strip()


def v(key, version, latest=True):
    return {"Key": key, "VersionId": version, "IsLatest": latest}


def main():
    print("\n[1] un bucket de verdad vacio cuenta cero")
    ck("cero en las cuatro columnas",
       contar([{"IsTruncated": False}]) == "0 0 0 0",
       contar([{"IsTruncated": False}]))

    print("\n[2] las cuatro cosas se cuentan por separado")
    # Un solo numero agregado no vale: hace falta saber QUE hay que vaciar.
    pagina = {
        "Versions": [v("a", "1"), v("a", "0", latest=False), v("b", "1")],
        "DeleteMarkers": [{"Key": "c", "VersionId": "9", "IsLatest": True}],
        "IsTruncated": False,
    }
    multipart = {"Uploads": [{"Key": "d", "UploadId": "u1"}], "IsTruncated": False}
    ck("2 actuales, 1 no actual, 1 marca, 1 multipart",
       contar([pagina], [multipart]) == "2 1 1 1", contar([pagina], [multipart]))

    print("\n[3] EL FALLO: mas de 1000 versiones, en varias paginas")
    # Con '--max-keys 1000' y UNA sola llamada esto contaba 1000 como mucho.
    p1 = {"Versions": [v(f"k{i}", "1") for i in range(1000)],
          "IsTruncated": True, "NextKeyMarker": "k999", "NextVersionIdMarker": "1"}
    p2 = {"Versions": [v(f"k{i}", "1") for i in range(1000, 1500)],
          "IsTruncated": False}
    ck("cuenta las 1500, no 1000", contar([p1, p2]) == "1500 0 0 0",
       contar([p1, p2]))

    print("\n[4] tres paginas, y las marcas de borrado tambien paginan")
    d1 = {"DeleteMarkers": [{"Key": f"d{i}", "VersionId": "1"} for i in range(1000)],
          "IsTruncated": True, "NextKeyMarker": "d999"}
    d2 = {"DeleteMarkers": [{"Key": f"d{i}", "VersionId": "1"}
                            for i in range(1000, 2000)],
          "IsTruncated": True, "NextKeyMarker": "d1999"}
    d3 = {"DeleteMarkers": [{"Key": "d2000", "VersionId": "1"}], "IsTruncated": False}
    ck("2001 marcas de borrado", contar([d1, d2, d3]) == "0 0 2001 0",
       contar([d1, d2, d3]))

    print("\n[5] una pagina vacia pero truncada NO termina el recuento")
    # Este es el caso que producia el cero. Con marcadores, S3 puede devolver
    # una pagina sin elementos y seguir habiendo contenido detras: quien mira
    # si la pagina viene vacia en vez de mirar IsTruncated, se para aqui.
    e1 = {"Versions": [], "IsTruncated": True, "NextKeyMarker": "z"}
    e2 = {"Versions": [v("z", "1")], "IsTruncated": False}
    ck("sigue y encuentra la version de la pagina 2",
       contar([e1, e2]) == "1 0 0 0", contar([e1, e2]))

    print("\n[6] los multipart a medias, que son invisibles")
    # No salen en 's3 ls' ni en list-object-versions, y bastan por si solos
    # para que delete-bucket falle con BucketNotEmpty. No contarlos era la
    # otra mitad del falso negativo.
    m1 = {"Uploads": [{"Key": f"m{i}", "UploadId": "u"} for i in range(1000)],
          "IsTruncated": True, "NextKeyMarker": "m999", "NextUploadIdMarker": "u"}
    m2 = {"Uploads": [{"Key": "m1000", "UploadId": "u"}], "IsTruncated": False}
    ck("bucket sin objetos pero con 1001 multipart NO figura como vacio",
       contar([{"IsTruncated": False}], [m1, m2]) == "0 0 0 1001",
       contar([{"IsTruncated": False}], [m1, m2]))

    print("\n[7] el script sigue siendo de solo consulta por defecto")
    with open(os.path.join(REPO, "scripts", "limpiar-recursos-viejos.sh"),
              encoding="utf-8") as fichero:
        guion = fichero.read()
    ck("exige --borrar", "--borrar) BORRAR=1" in guion)
    ck("exige teclear el entorno para confirmar",
       'die "escribiste' in guion and "read -r confirmacion" in guion)
    ck("nunca borra la clave KMS, solo el alias",
       "kms delete-alias" in guion
       and "schedule-key-deletion" not in guion
       and "kms delete-key" not in guion)
    ck("no toca el secreto de Anthropic",
       "secretsmanager" not in guion and "delete-secret" not in guion)
    ck("no toca el proveedor OIDC",
       "delete-open-id-connect-provider" not in guion)
    ck("el rol de CI no esta en la lista de roles que borra",
       "ROLES=(sanitizer submitter verifier canary)" in guion
       and 'itl_role "$ENTORNO" ci' in guion)

    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
