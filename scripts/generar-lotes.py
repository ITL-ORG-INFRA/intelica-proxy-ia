#!/usr/bin/env python3
"""Genera lotes para probar el flujo con volumen.

Dos modos:

  Sintetico — inventa peticiones, con un porcentaje configurable de "sucias"
  para ver como se comporta el gate a escala:

      ./scripts/generar-lotes.py --ficheros 20 --peticiones 500 --sucio 0.5

  Desde JSONL — convierte datos reales al envelope que espera el proxy. La
  ENTRADA del proxy no es JSONL: es un .json con un array 'requests'. JSONL es
  el formato de SALIDA de los resultados, y confundirlos es el primer rechazo
  que se lleva todo el mundo:

      ./scripts/generar-lotes.py --desde-jsonl datos.jsonl --peticiones 1000

  Cada linea del JSONL puede ser:
    · {"custom_id": "...", "params": {...}}   ya en formato, se pasa tal cual
    · {"id": "...", "texto": "..."}           se envuelve
    · {"texto": "..."} o texto plano          se envuelve y se numera

El porcentaje sucio importa: con el gate al 1%, un 0.5% pasa y las limpias
cruzan; un 2% aborta el lote entero. Generar los dos cases enseña la diferencia
mejor que cualquier explicacion.
"""
import argparse
import json
import os
import sys

#: numeros de prueba publicos de las marcas. No son datos reales.
#: Ver docs/SOBRE-LOS-PANES-DE-PRUEBA.md
SUCIEDAD = [
    "El cliente pago con la 4111111111111111 el martes.",
    "Cargo a la tarjeta 5555 5555 5555 4444 por el importe pendiente.",
    "Referencia de pago AMEX 3782-822463-10005.",
    "Se registro el numero ４１１１１１１１１１１１１１１１ en el formulario.",
]

LIMPIAS = [
    "Resume en tres lineas el motivo de la reclamacion adjunta.",
    "Clasifica la urgencia de este caso: alta, media o baja.",
    "Extrae la fecha de vencimiento mencionada en el texto.",
    "Indica si el cliente solicita baja, cambio de titular o revision.",
    "Redacta una respuesta breve confirmando la recepcion del caso.",
    "Identifica el producto contratado a partir de la descripcion.",
]


def request(custom_id, text, modelo, max_tokens):
    return {"custom_id": custom_id,
            "params": {"model": modelo, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": text}]}}


def desde_jsonl(path, modelo, max_tokens):
    """Convierte cada linea del JSONL en una peticion del envelope."""
    requests = []
    with open(path, encoding="utf-8") as file_:
        for numero, linea in enumerate(file_, 1):
            linea = linea.strip()
            if not linea:
                continue
            try:
                registro = json.loads(linea)
            except json.JSONDecodeError:
                # Una linea que no es JSON se trata como texto suelto.
                requests.append(request(f"linea-{numero}", linea, modelo, max_tokens))
                continue

            if isinstance(registro, dict) and "params" in registro:
                # Ya viene en formato; se respeta tal cual.
                registro.setdefault("custom_id", f"linea-{numero}")
                requests.append(registro)
                continue

            if isinstance(registro, dict):
                text = (registro.get("texto") or registro.get("text")
                         or registro.get("content") or registro.get("prompt"))
                if text is None:
                    # Sin campo de texto reconocible, se serializa el registro.
                    text = json.dumps(registro, ensure_ascii=False)
                bruto = str(registro.get("id") or registro.get("custom_id")
                            or f"linea-{numero}")
            else:
                text, bruto = str(registro), f"linea-{numero}"

            # El custom_id viaja a Anthropic: tiene que ser opaco y del alfabeto
            # que acepta el envelope.
            clean_text = "".join(c if c.isalnum() or c in "-_" else "-" for c in bruto)[:64]
            requests.append(request(clean_text or f"linea-{numero}", str(text),
                                       modelo, max_tokens))
    return requests


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ficheros", type=int, default=5, help="cuantos lotes generar")
    p.add_argument("--peticiones", type=int, default=200, help="peticiones por lote")
    p.add_argument("--sucio", type=float, default=0.0,
                   help="porcentaje de peticiones con tarjetas de prueba (0-100)")
    p.add_argument("--desde-jsonl", metavar="FICHERO",
                   help="convierte un JSONL en lotes del envelope")
    p.add_argument("--modelo", default="claude-sonnet-4-5")
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--salida", default="carga", help="carpeta destino")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if args.desde_jsonl:
        if not os.path.isfile(args.desde_jsonl):
            print(f"no existe {args.desde_jsonl}", file=sys.stderr)
            return 2
        todas = desde_jsonl(args.desde_jsonl, args.modelo, args.max_tokens)
        if not todas:
            print("el JSONL no tenia ninguna linea util", file=sys.stderr)
            return 2
        # Se parte en lotes del tamano pedido.
        trozos = [todas[i:i + args.requests]
                  for i in range(0, len(todas), args.requests)]
        print(f"{len(todas)} peticiones -> {len(trozos)} lote(s)")
    else:
        trozos = []
        # El determinismo importa: dos ejecuciones con los mismos argumentos dan
        # los mismos ficheros, asi que una prueba se puede repetir tal cual.
        cada_cuantas = int(100 / args.sucio) if args.sucio > 0 else 0
        for index in range(args.files):
            batch = []
            for j in range(args.requests):
                global_j = index * args.requests + j
                if cada_cuantas and global_j % cada_cuantas == 0 and global_j > 0:
                    text = SUCIEDAD[global_j % len(SUCIEDAD)]
                else:
                    text = f"{LIMPIAS[global_j % len(LIMPIAS)]} (caso {global_j})"
                batch.append(request(f"c-{index}-{j}", text,
                                     args.modelo, args.max_tokens))
            trozos.append(batch)

    for index, batch in enumerate(trozos):
        sucias = sum(1 for r in batch
                     if any(s.split()[3] in r["params"]["messages"][0]["content"]
                            for s in SUCIEDAD[:1]))
        documento = {
            "requests": batch,
            "metadata": {
                "caso": f"carga sintetica · {len(batch)} peticiones"
                        + (f" · {args.sucio}% sucias" if args.sucio else " · todas limpias"),
                "generado_por": "scripts/generar-lotes.py",
            },
        }
        path = os.path.join(args.out, f"carga-{index:03d}.json")
        with open(path, "w", encoding="utf-8") as file_:
            json.dump(documento, file_, ensure_ascii=False)
        tam = os.path.getsize(path)
        print(f"  {os.path.basename(path):22} {len(batch):6} peticiones  {tam / 1024:8.1f} KB")

    print(f"\n{len(trozos)} fichero(s) en {args.out}/")
    if args.sucio:
        veredicto = ("por DEBAJO del umbral: las limpias cruzan"
                     if args.sucio < 1.0 else
                     "por ENCIMA del umbral: el gate aborta el lote entero")
        print(f"Con {args.sucio}% sucias y el gate al 1%, {veredicto}.")
    print(f"\nSubirlos:  ./scripts/probar-flujo.sh dev {args.out}/*.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
