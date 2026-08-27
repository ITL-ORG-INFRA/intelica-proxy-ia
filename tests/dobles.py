"""Dobles en memoria de S3, DynamoDB y CloudWatch.

No pretenden ser AWS. Implementan exactamente las operaciones que usa el
codigo, incluidas las expresiones condicionales de las que depende la
admision — que es donde esta la unica condicion de carrera del sistema y por
tanto lo que mas interesa poder probar.
"""
import io
from typing import Any, Dict, Tuple


class FakeS3:
    def __init__(self) -> None:
        self.objetos: Dict[Tuple[str, str], bytes] = {}
        self.borrados: list = []

    def put_object(self, Bucket, Key, Body, **_kw):
        self.objetos[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()
        return {}

    def get_object(self, Bucket, Key, **_kw):
        if (Bucket, Key) not in self.objetos:
            raise KeyError(f"no existe s3://{Bucket}/{Key}")
        return {"Body": io.BytesIO(self.objetos[(Bucket, Key)])}

    def head_object(self, Bucket, Key, **_kw):
        datos = self.objetos[(Bucket, Key)]
        return {"ContentLength": len(datos), "ETag": '"etag-simulado"'}

    def delete_object(self, Bucket, Key, **_kw):
        self.borrados.append((Bucket, Key))
        self.objetos.pop((Bucket, Key), None)
        return {}

    def upload_file(self, ruta, Bucket, Key, **_kw):
        with open(ruta, "rb") as fichero:
            self.objetos[(Bucket, Key)] = fichero.read()

    def claves(self, bucket):
        return sorted(k for b, k in self.objetos if b == bucket)


class FakeTable:
    """Lo justo de DynamoDB: put/get/update/query y las tres condiciones que
    usa store.py."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}

    # --- helpers ---
    @staticmethod
    def _error(codigo):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": codigo, "Message": codigo}}, "op")

    def put_item(self, Item, ConditionExpression=None, **_kw):
        clave = Item["batch_id"]
        if ConditionExpression == "attribute_not_exists(batch_id)" and clave in self.items:
            raise self._error("ConditionalCheckFailedException")
        self.items[clave] = dict(Item)
        return {}

    def get_item(self, Key, **_kw):
        item = self.items.get(Key["batch_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ConditionExpression=None, **_kw):
        clave = Key["batch_id"]
        item = self.items.setdefault(clave, {"batch_id": clave})
        nombres = ExpressionAttributeNames or {}
        valores = ExpressionAttributeValues or {}

        if ConditionExpression:
            if ConditionExpression == "attribute_not_exists(#r) OR #r <= :headroom":
                campo = nombres["#r"]
                actual = item.get(campo)
                if actual is not None and actual > valores[":headroom"]:
                    raise self._error("ConditionalCheckFailedException")
            elif ConditionExpression == "inflight_counted = :true":
                if item.get("inflight_counted") is not True:
                    raise self._error("ConditionalCheckFailedException")
            elif "IN (" in ConditionExpression and "attribute_not_exists" in ConditionExpression:
                # attribute_not_exists(#s) OR #s IN (:a, :b)
                campo = nombres.get("#s", "status")
                actual = item.get(campo)
                permitidos = [v for k, v in valores.items() if k != ":enviando"
                              and not k.startswith(":t")]
                if actual is not None and actual not in permitidos:
                    raise self._error("ConditionalCheckFailedException")

        # Una UpdateExpression puede combinar clausulas: "ADD c :n SET x = :y".
        # El doble tiene que soportarlo porque el codigo real lo usa para sumar
        # al contador y refrescar updated_at en la misma escritura atomica.
        for clausula, cuerpo in self._clausulas(UpdateExpression):
            if clausula == "ADD":
                partes = cuerpo.split()
                for i in range(0, len(partes), 2):
                    campo = nombres.get(partes[i], partes[i]).lstrip("#")
                    item[campo] = item.get(campo, 0) + valores[partes[i + 1]]
            elif clausula == "SET":
                for asignacion in self._asignaciones(cuerpo):
                    izq, der = [x.strip() for x in asignacion.split("=", 1)]
                    campo = nombres.get(izq, izq)
                    if der.startswith("if_not_exists("):
                        dentro = der[len("if_not_exists("):-1]
                        objetivo, alterno = [x.strip() for x in dentro.split(",", 1)]
                        objetivo = nombres.get(objetivo, objetivo)
                        item.setdefault(objetivo, valores[alterno])
                    else:
                        item[campo] = valores[der]
        return {}

    @staticmethod
    def _clausulas(expresion):
        """Parte 'ADD a :1 SET b = :2' en [(ADD, 'a :1'), (SET, 'b = :2')]."""
        import re as _re
        trozos = _re.split(r"\b(ADD|SET|REMOVE|DELETE)\b", expresion.strip(),
                           flags=_re.IGNORECASE)
        salida, actual = [], None
        for trozo in trozos:
            if not trozo.strip():
                continue
            if trozo.upper() in ("ADD", "SET", "REMOVE", "DELETE"):
                actual = trozo.upper()
            elif actual:
                salida.append((actual, trozo.strip()))
        return salida

    @staticmethod
    def _asignaciones(cuerpo):
        """Separa por comas sin romper if_not_exists(a, b)."""
        salida, nivel, actual = [], 0, ""
        for caracter in cuerpo:
            if caracter == "(":
                nivel += 1
            elif caracter == ")":
                nivel -= 1
            if caracter == "," and nivel == 0:
                salida.append(actual)
                actual = ""
            else:
                actual += caracter
        if actual.strip():
            salida.append(actual)
        return salida

    def query(self, IndexName=None, KeyConditionExpression=None, Limit=100, **_kw):
        # Solo se consulta status-index con igualdad sobre 'status'.
        buscado = getattr(KeyConditionExpression, "_values", [None, None])[1]
        encontrados = [dict(v) for v in self.items.values() if v.get("status") == buscado]
        encontrados.sort(key=lambda i: i.get("created_at", ""))
        return {"Items": encontrados[:Limit]}


class FakeResource:
    def __init__(self, tabla: FakeTable) -> None:
        self._tabla = tabla

    def Table(self, _nombre):
        return self._tabla


class FakeCloudWatch:
    def __init__(self) -> None:
        self.metricas: list = []

    def put_metric_data(self, Namespace, MetricData, **_kw):
        for dato in MetricData:
            self.metricas.append((Namespace, dato["MetricName"], dato["Value"]))
        return {}

    def nombres(self):
        return [m[1] for m in self.metricas]
