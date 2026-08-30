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
        self.deleted: list = []

    def put_object(self, Bucket, Key, Body, **_kw):
        self.objetos[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()
        return {}

    def get_object(self, Bucket, Key, **_kw):
        if (Bucket, Key) not in self.objetos:
            raise KeyError(f"no existe s3://{Bucket}/{Key}")
        return {"Body": io.BytesIO(self.objetos[(Bucket, Key)])}

    def head_object(self, Bucket, Key, **_kw):
        data = self.objetos[(Bucket, Key)]
        return {"ContentLength": len(data), "ETag": '"etag-simulado"'}

    def delete_object(self, Bucket, Key, **_kw):
        self.deleted.append((Bucket, Key))
        self.objetos.pop((Bucket, Key), None)
        return {}

    def upload_file(self, path, Bucket, Key, **_kw):
        with open(path, "rb") as file_:
            self.objetos[(Bucket, Key)] = file_.read()

    def keys_of(self, bucket):
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
        key_ = Item["batch_id"]
        if ConditionExpression == "attribute_not_exists(batch_id)" and key_ in self.items:
            raise self._error("ConditionalCheckFailedException")
        self.items[key_] = dict(Item)
        return {}

    def get_item(self, Key, **_kw):
        item = self.items.get(Key["batch_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ConditionExpression=None, **_kw):
        key_ = Key["batch_id"]
        item = self.items.setdefault(key_, {"batch_id": key_})
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}

        if ConditionExpression:
            if ConditionExpression == "attribute_not_exists(#r) OR #r <= :headroom":
                field = names["#r"]
                current = item.get(field)
                if current is not None and current > values[":headroom"]:
                    raise self._error("ConditionalCheckFailedException")
            elif ConditionExpression == "inflight_counted = :true":
                if item.get("inflight_counted") is not True:
                    raise self._error("ConditionalCheckFailedException")
            elif "IN (" in ConditionExpression and "attribute_not_exists" in ConditionExpression:
                # attribute_not_exists(#s) OR #s IN (:a, :b)
                field = names.get("#s", "status")
                current = item.get(field)
                permitidos = [v for k, v in values.items() if k != ":submitting"
                              and not k.startswith(":t")]
                if current is not None and current not in permitidos:
                    raise self._error("ConditionalCheckFailedException")

        # Una UpdateExpression puede combinar clausulas: "ADD c :n SET x = :y".
        # El doble tiene que soportarlo porque el codigo real lo usa para sumar
        # al contador y refrescar updated_at en la misma escritura atomica.
        for clausula, body in self._clauses(UpdateExpression):
            if clausula == "ADD":
                parts = body.split()
                for i in range(0, len(parts), 2):
                    field = names.get(parts[i], parts[i]).lstrip("#")
                    item[field] = item.get(field, 0) + values[parts[i + 1]]
            elif clausula == "SET":
                for asignacion in self._assignments(body):
                    izq, der = [x.strip() for x in asignacion.split("=", 1)]
                    field = names.get(izq, izq)
                    if der.startswith("if_not_exists("):
                        dentro = der[len("if_not_exists("):-1]
                        objetivo, alterno = [x.strip() for x in dentro.split(",", 1)]
                        objetivo = names.get(objetivo, objetivo)
                        item.setdefault(objetivo, values[alterno])
                    else:
                        item[field] = values[der]
        return {}

    @staticmethod
    def _clauses(expresion):
        """Parte 'ADD a :1 SET b = :2' en [(ADD, 'a :1'), (SET, 'b = :2')]."""
        import re as _re
        trozos = _re.split(r"\b(ADD|SET|REMOVE|DELETE)\b", expresion.strip(),
                           flags=_re.IGNORECASE)
        out, current = [], None
        for trozo in trozos:
            if not trozo.strip():
                continue
            if trozo.upper() in ("ADD", "SET", "REMOVE", "DELETE"):
                current = trozo.upper()
            elif current:
                out.append((current, trozo.strip()))
        return out

    @staticmethod
    def _assignments(body):
        """Separa por comas sin romper if_not_exists(a, b)."""
        out, nivel, current = [], 0, ""
        for caracter in body:
            if caracter == "(":
                nivel += 1
            elif caracter == ")":
                nivel -= 1
            if caracter == "," and nivel == 0:
                out.append(current)
                current = ""
            else:
                current += caracter
        if current.strip():
            out.append(current)
        return out

    def query(self, IndexName=None, KeyConditionExpression=None, Limit=100, **_kw):
        # Solo se consulta status-index con igualdad sobre 'status'.
        buscado = getattr(KeyConditionExpression, "_values", [None, None])[1]
        encontrados = [dict(v) for v in self.items.values() if v.get("status") == buscado]
        encontrados.sort(key=lambda i: i.get("created_at", ""))
        return {"Items": encontrados[:Limit]}


class FakeResource:
    def __init__(self, table_: FakeTable) -> None:
        self._tabla = table_

    def Table(self, _name):
        return self._tabla


class FakeCloudWatch:
    def __init__(self) -> None:
        self.metricas: list = []

    def put_metric_data(self, Namespace, MetricData, **_kw):
        for dato in MetricData:
            self.metricas.append((Namespace, dato["MetricName"], dato["Value"]))
        return {}

    def names(self):
        return [m[1] for m in self.metricas]
