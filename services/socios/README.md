# MS Socios

API para socios de distribución (HU32, F19/E08). Es una fachada versionada: autentica al socio con OAuth2 *client
credentials*, aplica su contrato (endpoints habilitados y cuota por segundo) y la idempotencia, y le pide la cotización a
MS Cotización. **No guarda cotizaciones ni pólizas** (ADR-0001). Las decisiones de diseño están en
[ADR-0025](../../docs/adr/0025-ms-socios-fachada-oauth-cuota.md).

## Endpoints

| Método y ruta | Auth | Qué hace |
|---|---|---|
| `POST /oauth/token` | `client_id` y `client_secret` (cuerpo `x-www-form-urlencoded` o HTTP Basic) | Emite un JWT `Bearer` de 1 hora. Los errores siguen RFC 6749 (`invalid_client`, `invalid_request`, `unsupported_grant_type`). |
| `POST /partners/v1/cotizaciones` | `Bearer` + contrato con `cotizaciones:crear` | Cotiza para un cliente del socio (`producto`, `referencia_cliente`, `datos`). Exige `X-Idempotency-Key`. Devuelve 201 con precio y coberturas. |
| `GET /partners/v1/status` | Pública | Estado del API (`operativo`, `degradado` o `no_disponible`) y de sus dependencias, sin datos internos. 503 si no puede cotizar. |
| `GET /health` | Interna (Kubernetes) | Liveness. Fuera del borde. |

Errores de `/partners/v1`, con el formato `{"error": {"code", "message", "details", "trace_id"}}`:

| Código | HTTP | Qué hacer |
|---|---|---|
| `INVALID_TOKEN` | 401 | Token ausente o inválido: solicite uno en `/oauth/token`. |
| `TOKEN_EXPIRED` | 401 | Pida un token nuevo. |
| `ENDPOINT_NOT_AUTHORIZED` | 403 | El contrato no habilita el endpoint. |
| `INVALID_REQUEST` | 400 / 422 | Falta `X-Idempotency-Key` (400), o el cuerpo, el producto o los datos no son válidos (422, con `details`). |
| `IDEMPOTENCY_CONFLICT` | 409 | La clave ya se usó con otro cuerpo o la original sigue en proceso. No reintente con esa clave. |
| `RATE_LIMIT_EXCEEDED` | 429 | Espere `Retry-After` segundos. |
| `SERVICE_UNAVAILABLE` | 503 | Cotización o el almacén no responden; reintente con la misma clave. |
| `INTERNAL_ERROR` | 500 | Reporte el `trace_id`. |

El contrato completo está en `docs/api/openapi.json` (lo verifica `tests/test_contrato.py`).

## Modelo

- `socios.clientes_api`: `client_id`, nombre, `client_secret_hash` (bcrypt) y estado (`ACTIVO`/`INACTIVO`).
- `socios.contratos_api`: versión del API, `endpoints_habilitados` (JSONB), `cuota_rps`, `fecha_inicio`/`fecha_fin`.
- Redis: `socios:cuota:<client_id>:<segundo>` (ventana de 1 s) y `socios:idem:<client_id>:<sha256(clave)>` (24 h).

No hay portal de registro de socios (fuera de alcance). Un socio se da de alta con `scripts/sembrar_socio_demo.py`, que es
idempotente, guarda solo el hash y nunca imprime el secreto. En staging lo ejecuta el Job de siembra de `services.yaml`.

## Trabajar en local

```bash
cd services/socios
pip install -r requirements.txt -r ../../requirements-dev.txt
pytest --cov=app                      # sin Docker se omiten las pruebas con PostgreSQL y Redis reales
cd ../.. && ruff check services/socios
```

Para levantarlo de punta a punta (con MS Cotización en el puerto 8000), siga los pasos de `docker-compose.yml`. Luego:

```bash
SOCIOS_BASE_URL=http://localhost:8007 SOCIO_CLIENT_SECRET=<el que sembró> python scripts/ejemplo_socio.py
```

`scripts/ejemplo_socio.py` es el cliente de ejemplo de un socio: consulta el estado, pide el token, cotiza y repite la
solicitud con la misma clave para comprobar la idempotencia. `scripts/carga/medir_latencia.py` mide el sobrecosto de la
fachada frente a Cotización directa (criterio 10).

## Configuración

| Variable | Obligatoria | Notas |
|---|---|---|
| `SOCIOS_DATABASE_URL` o `DATABASE_URL` | Sí | PostgreSQL (esquema `socios`, Alembic). |
| `REDIS_URL` | Sí | Cuota e idempotencia, compartidas entre réplicas. |
| `COTIZACION_URL` | Sí | `http://solventa-backend` en staging. |
| `SOCIOS_JWT_SECRETO` | Sí | ≥ 32 caracteres; propio de Socios. |
| `SOCIOS_JWT_TTL_S` | No | 3600. |
| `SOCIOS_TIMEOUT_COTIZACION_S` | No | 3. |
| `SOLVENTA_MODO_DESARROLLO` | Nunca en staging | Habilita SQLite, almacén en memoria y un secreto JWT de desarrollo. |
