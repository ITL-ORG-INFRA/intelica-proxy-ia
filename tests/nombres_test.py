"""La nomenclatura de recursos y los nombres de artefacto.

Un nombre mal construido no da un error: da un "no existe" que parece otra
cosa —una Lambda sin desplegar, un bucket sin crear, un permiso que falta— y
se investiga por el lado equivocado. Por eso se comprueba el nombre EXACTO
contra la infraestructura ya desplegada, no la forma de la plantilla.

Se prueba el helper de shell de verdad, invocandolo, porque es el que corren
los scripts. Reimplementar la convencion aqui en Python solo probaria que dos
copias coinciden, que es justo lo que se quiere evitar.

    .venv/bin/python tests/nombres_test.py
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(REPO, "scripts", "lib", "nombres.sh")

FAILURES = []


def ck(name, condition, detail=""):
    print(("  OK   " if condition else "  FALLA ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        FAILURES.append(name)


def sh(expression, env=None):
    """Evalua una expresion con el helper cargado y devuelve su salida."""
    entorno = dict(os.environ)
    entorno.update(env or {})
    out = subprocess.run(
        ["bash", "-c", f'source "{HELPER}"; {expression}'],
        capture_output=True, text=True, env=entorno, check=False)
    if out.returncode != 0:
        return f"<error rc={out.returncode}: {out.stderr.strip()}>"
    return out.stdout.strip()


#: Lo que hay desplegado. Si esta tabla y AWS discrepan, manda AWS y esta
#: tabla es la que hay que corregir — pero entonces alguien tiene que verlo,
#: y para eso esta escrita a mano y no generada.
DESPLEGADO = {
    'itl_lambda dev sanitizer':  "itl-0003-proxy-ia-dev-lambda-sanitizer-03",
    'itl_lambda dev submitter':  "itl-0003-proxy-ia-dev-lambda-submitter-03",
    'itl_lambda dev reconciler': "itl-0003-proxy-ia-dev-lambda-reconciler-03",
    'itl_lambda dev fetcher':    "itl-0003-proxy-ia-dev-lambda-fetcher-03",
    'itl_lambda dev verifier':   "itl-0003-proxy-ia-dev-lambda-verifier-03",
    'itl_lambda dev canary':     "itl-0003-proxy-ia-dev-lambda-canary-03",
    'itl_layer dev':             "itl-0003-proxy-ia-dev-lambda-deps-03",
    'itl_bucket dev raw':        "itl-0003-proxy-ia-dev-s3-raw-03",
    'itl_bucket dev clean':      "itl-0003-proxy-ia-dev-s3-clean-03",
    'itl_bucket dev quarantine': "itl-0003-proxy-ia-dev-s3-quarantine-03",
    'itl_bucket dev results':    "itl-0003-proxy-ia-dev-s3-results-03",
    'itl_table dev':             "itl-0003-proxy-ia-dev-ddb-batches-03",
    'itl_role dev ci':           "itl-0003-proxy-ia-dev-role-ci-03",
    'itl_topic dev':             "itl-0003-proxy-ia-dev-sns-alarms-03",
    'itl_log_group dev canary':  "/aws/lambda/itl-0003-proxy-ia-dev-lambda-canary-03",
}

#: los seis zip que build.sh tiene que dejar en dist/
ARTEFACTOS = ["sanitizer.zip", "submitter.zip", "reconciler.zip",
              "fetcher.zip", "verifier.zip", "canary.zip"]


def main():
    print("\n[1] los nombres coinciden EXACTAMENTE con lo desplegado")
    for expresion, esperado in DESPLEGADO.items():
        obtenido = sh(expresion)
        ck(f"{expresion} -> {esperado}", obtenido == esperado, obtenido)

    print("\n[2] el ARN del rol de CI es el que espera GitHub")
    arn = "arn:aws:iam::891376942769:role/" + sh("itl_role dev ci")
    ck("AWS_ROLE_DEV",
       arn == "arn:aws:iam::891376942769:role/itl-0003-proxy-ia-dev-role-ci-03", arn)

    print("\n[3] la convencion es una sola, y se aplica igual a todo")
    # Siete tramos separados por guion, con el asset id y la secuencia en su
    # sitio. Es lo que distingue un nombre de la convencion de uno inventado.
    for expresion in DESPLEGADO:
        if expresion.startswith("itl_log_group"):
            continue
        tramos = sh(expresion).split("-")
        ck(f"{expresion}: itl/asset/app/entorno/tipo/descriptor/secuencia",
           tramos[0] == "itl" and tramos[1] == "0003"
           and tramos[2:4] == ["proxy", "ia"] and tramos[-1] == "03",
           tramos)

    print("\n[4] el entorno y la secuencia son parametros, no literales")
    ck("qa sale de la misma funcion",
       sh("itl_lambda qa sanitizer") == "itl-0003-proxy-ia-qa-lambda-sanitizer-03",
       sh("itl_lambda qa sanitizer"))
    ck("ITL_STACK cambia la secuencia",
       sh("itl_lambda dev sanitizer", {"ITL_STACK": "07"})
       == "itl-0003-proxy-ia-dev-lambda-sanitizer-07",
       sh("itl_lambda dev sanitizer", {"ITL_STACK": "07"}))

    print("\n[5] no queda rastro de la nomenclatura anterior")
    for expresion in DESPLEGADO:
        nombre = sh(expresion)
        ck(f"{expresion} no dice 'intelica-proxy-ia'",
           "intelica-proxy-ia" not in nombre, nombre)

    print("\n[6] los seis artefactos, con el nombre que espera publish.sh")
    funciones = sh('printf "%s\\n" "${ITL_FUNCTIONS[@]}"').split()
    ck("las seis funciones estan declaradas en el helper",
       sorted(funciones) == sorted(z[:-4] for z in ARTEFACTOS), funciones)
    for zipname in ARTEFACTOS:
        ck(f"{zipname} sale de una funcion declarada",
           zipname[:-4] in funciones, funciones)
    ck("ninguna funcion conserva el nombre castellano",
       not ({"canario", "reconciliador", "verificador"} & set(funciones)), funciones)

    print("\n[7] cada funcion tiene su carpeta en src/")
    for funcion in funciones:
        ck(f"src/{funcion}/ existe",
           os.path.isdir(os.path.join(REPO, "src", funcion)), funcion)

    print("\n[8] los prefijos de S3 son los definitivos")
    for variable, esperado in (("ITL_PREFIX_INPUT", "input/"),
                               ("ITL_PREFIX_STATUS", "status/"),
                               ("ITL_PREFIX_CANARY", "canary/"),
                               ("ITL_PREFIX_CLEAN", "clean/"),
                               ("ITL_PREFIX_RESULTS", "results/"),
                               ("ITL_PREFIX_QUARANTINE", "quarantine/")):
        ck(f"{variable} = {esperado}", sh(f'printf "%s" "${variable}"') == esperado,
           sh(f'printf "%s" "${variable}"'))
    ck("el manifiesto termina en _MANIFEST.json",
       sh('printf "%s" "$ITL_MANIFEST_SUFFIX"') == "_MANIFEST.json",
       sh('printf "%s" "$ITL_MANIFEST_SUFFIX"'))

    print("\n" + ("TODO OK" if not FAILURES else f"{len(FAILURES)} FALLOS: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
