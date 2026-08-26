# Frontera con el repo de Terraform

Dos repos tocan los mismos recursos de AWS. Esto define quién manda sobre qué,
y qué hay que configurar en el repo de Terraform para que no se peleen.

## Reparto

| | Repo de Terraform | Este repo |
|---|---|---|
| Buckets, KMS, IAM, DynamoDB | **dueño** | no toca |
| EventBridge, alarmas, SNS, secreto | **dueño** | no toca |
| Existencia de las 6 Lambdas | **dueño** | no toca |
| Memoria, timeout, `/tmp`, concurrencia | **dueño** | no toca |
| Variables de entorno | **dueño** | no toca |
| **Código de las Lambdas** | no toca | **dueño** |
| **Versión del layer y su contenido** | no toca | **dueño** |

Regla práctica: si es un número de configuración, se cambia en Terraform. Si es
Python, se cambia aquí.

## Lo que hay que añadir en Terraform (obligatorio)

Sin esto, el siguiente `terraform apply` revierte el código desplegado a lo que
haya en el estado de Terraform, y el despliegue de este repo se pierde en silencio.

En **cada** `aws_lambda_function`:

```hcl
resource "aws_lambda_function" "sanitizer" {
  # ...

  # El código y el layer los publica el repo intelica-proxy-ia desde su CI.
  # Terraform crea la función y es dueño de su configuración, pero no del
  # contenido. Sin este ignore_changes, un apply revierte el último despliegue.
  lifecycle {
    ignore_changes = [
      filename,
      source_code_hash,
      s3_key,
      s3_object_version,
      layers,
      last_modified,
    ]
  }
}
```

Terraform necesita *algo* como código inicial para poder crear la función. Un zip
mínimo de arranque vale:

```hcl
data "archive_file" "arranque" {
  type        = "zip"
  output_path = "${path.module}/arranque.zip"
  source {
    content  = "def lambda_handler(event, context):\n    raise RuntimeError('sin desplegar: corre el CI de intelica-proxy-ia')\n"
    filename = "handler.py"
  }
}
```

Que falle a propósito es intencionado: una función con el arranque puesto no está
desplegada, y es mejor que lo diga a que aparente funcionar.

## Roles OIDC para el CI

El pipeline de este repo usa OIDC de GitHub — **no hay claves de acceso guardadas
en secretos**. Hacen falta dos roles, creados por Terraform:

- Proveedor OIDC: `token.actions.githubusercontent.com`
  (audiencia `sts.amazonaws.com`).
- **Un rol por entorno**, cada uno restringido a su *environment* de GitHub:

  | Rol | Condición `sub` del trust policy |
  |---|---|
  | dev | `repo:<org>/intelica-proxy-ia:environment:dev` |
  | qa | `repo:<org>/intelica-proxy-ia:environment:qa` |
  | prod | `repo:<org>/intelica-proxy-ia:environment:prod` |

  Acotarlos al *environment* y no a la rama es lo que hace que la aprobación
  manual de GitHub signifique algo: sin pasar por ella no se emite el token.
  Un rol acotado a `ref:refs/heads/main` se lo puede llevar cualquier job que
  corra en esa rama, aprobación incluida o no.

Permisos que necesitan ambos roles (y sólo estos):

```
lambda:GetFunction
lambda:GetFunctionConfiguration
lambda:UpdateFunctionCode
lambda:UpdateFunctionConfiguration    # únicamente para adjuntar el layer
lambda:PublishVersion
lambda:PublishLayerVersion
lambda:ListLayerVersions
```

Sobre las funciones `intelica-proxy-ia-<entorno>-*` y el layer
`intelica-proxy-ia-<entorno>-deps`, **acotado cada rol a su propio entorno**:
el rol de qa no debe poder tocar prod. **Nada de S3, DynamoDB, KMS ni IAM**: el CI
despliega código, no toca datos ni permisos. Si el rol del CI puede leer el bucket
raw, la frontera del sistema se ha roto por la puerta de atrás.

Los ARN de los roles se configuran como **variables** (no secretos: un ARN no lo
es). Hay dos sitios donde ponerlas y el workflow acepta los dos:

**Opción A — por environment (recomendada).** Settings → Environments → `dev` →
Variables → `AWS_ROLE` = el ARN. Lo mismo en `qa` y en `prod`. Mismo nombre de
variable en los tres, cada uno con su valor. Es lo natural: el ARN del rol de dev
es propiedad del entorno dev.

**Opción B — a nivel de repositorio.** Settings → Secrets and variables → Actions
→ Variables:

- `AWS_ROLE_DEV`
- `AWS_ROLE_QA`
- `AWS_ROLE_PROD`

Si están definidas las dos, gana la del environment. El job dice en su log de
dónde tomó el ARN, así que si algo no cuadra se ve enseguida.

## Configuración en GitHub

### Qué entornos están activos

Variable de repositorio **`ENTORNOS_ACTIVOS`** con la lista separada por comas de
los entornos cuya infraestructura ya existe:

| Valor | Efecto |
|---|---|
| sin definir | sólo `dev` (el valor por defecto) |
| `dev` | sólo dev |
| `dev,qa` | dev y qa |
| `dev,qa,prod` | la cadena completa |

Un entorno que no esté en la lista se salta, no falla. Así el pipeline no sale en
rojo en cada merge por intentar desplegar a algo que todavía no existe — y un
pipeline que siempre está rojo deja de ser una señal.

- Environment **`dev`**: sin protección.
- Environment **`qa`**: sin protección por defecto. Si el equipo de QA prefiere
  que su entorno no cambie bajo sus pies a mitad de una tanda de pruebas,
  añádele *required reviewers* — es una opción de GitHub, no hace falta tocar
  código.
- Environment **`prod`**: con *required reviewers*. Ahí es donde alguien aprueba
  antes de que el despliegue a producción corra.

Cada environment lleva su variable `AWS_ROLE_<ENTORNO>` (o, si preferís, la
variable puede definirse a nivel de environment y el workflow la recoge igual).

## Cómo saber si la frontera se rompió

```bash
make verificar-todo      # dev, qa y prod de una vez
```

Compara el `CodeSha256` de cada función desplegada con el sha256 del zip local.
Si un `terraform apply` revirtió el código, esto lo dice.

Merece la pena correrlo también después de cada apply del repo de Terraform, hasta
que os fiéis de que el `ignore_changes` está bien puesto.
