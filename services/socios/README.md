# MS Socios

API para socios de distribución (HU32 y HU33, F19/E08). Es una fachada versionada: autentica al socio con OAuth2 *client
credentials*, aplica su contrato (endpoints habilitados y cuota por segundo) y la idempotencia, le pide la cotización a
MS Cotización y le ofrece la venta embebida sobre MS Suscripción. **No guarda cotizaciones, ventas ni pólizas** (ADR-0001).
Las decisiones de diseño están en [ADR-0025](../../docs/adr/0025-ms-socios-fachada-oauth-cuota.md) (fachada) y
[ADR-0026](../../docs/adr/0026-ms-socios-venta-embebida.md) (venta embebida).

## Endpoints

| Método y ruta | Auth | Qué hace |
|---|---|---|
| `POST /oauth/token` | `client_id` y `client_secret` (cuerpo `x-www-form-urlencoded` o HTTP Basic) | Emite un JWT `Bearer` de 1 hora. Los errores siguen RFC 6749 (`invalid_client`, `invalid_request`, `unsupported_grant_type`). |
| `POST /partners/v1/cotizaciones` | `Bearer` + contrato con `cotizaciones:crear` | Cotiza para un cliente del socio (`producto`, `referencia_cliente`, `datos`). Exige `X-Idempotency-Key`. Devuelve 201 con precio y coberturas. |
| `POST /partners/v1/suscripciones` | `Bearer` + `suscripciones:crear` | Inicia la saga de suscripción (HU15) de una cotización para un cliente registrado (`cotizacion_id`, `cliente`, `metodo_pago`, `acepta_terminos`, `periodicidad_pago`). Exige `X-Idempotency-Key`. Devuelve 202 con el `id` de la venta y `poliza_url`. |
| `GET /partners/v1/polizas/{id}` | `Bearer` + `polizas:consultar` | Estado de la venta (`id` de la suscripción) y, al emitirse, la póliza (número, vigencia, hash). La venta de otro socio responde 404. |
| `GET /partners/v1/suscripciones/{id}/documento-firma` y `/pdf` | `Bearer` + `suscripciones:firmar` | Hash y vencimiento del documento por firmar, y el PDF para que el cliente lo lea en la app del socio. |
| `POST /partners/v1/suscripciones/{id}/firma/codigo` | `Bearer` + `suscripciones:firmar` | Envía al cliente el código de un solo uso (OTP). |
| `POST /partners/v1/suscripciones/{id}/firma` | `Bearer` + `suscripciones:firmar` | Firma del cliente (hash, revisión, código, canal). Exige `X-Idempotency-Key`; repetirla devuelve la misma firma. |
| `GET /partners/v1/status` | Pública | Estado del API (`operativo`, `degradado` o `no_disponible`) y de sus dependencias, sin datos internos. 503 si no puede cotizar. |
| `GET /health` | Interna (Kubernetes) | Liveness. Fuera del borde. |

Errores de `/partners/v1`, con el formato `{"error": {"code", "message", "trace_id", "reason"?, "details"}}`. La referencia completa
(catálogo, motivos y política de reintentos) está en [`docs/api/errores-y-reintentos.md`](docs/api/errores-y-reintentos.md) y el diseño en
[ADR-0027](../../docs/adr/0027-ms-socios-catalogo-de-errores.md):

| Código | HTTP | Qué hacer |
|---|---|---|
| `INVALID_TOKEN` | 401 | Token ausente o inválido: solicite uno en `/oauth/token`. |
| `TOKEN_EXPIRED` | 401 | Pida un token nuevo. |
| `ENDPOINT_NOT_AUTHORIZED` | 403 | El contrato no habilita el endpoint. |
| `RESOURCE_NOT_FOUND` | 404 | La venta no existe o es de otro socio (no se distingue). |
| `INVALID_REQUEST` | 400 | La cabecera, el cuerpo o los datos no son válidos (`details`), o una regla de negocio no se cumple (`reason`: `KYC_NO_VERIFICADO`, `COTIZACION_YA_USADA`, `OTP_INVALIDO`...). |
| `IDEMPOTENCY_CONFLICT` | 409 | La clave ya se usó con otro cuerpo: no reintente con ella. Con `reason: SOLICITUD_EN_PROCESO`, la original sigue en curso: reintente con la misma clave tras `Retry-After`. |
| `RATE_LIMIT_EXCEEDED` | 429 | Espere `Retry-After` segundos. |
| `SERVICE_UNAVAILABLE` | 503 | Cotización, Identidad, Suscripción o el almacén no responden; reintente con la misma clave. |
| `SERVICE_DEGRADED` | 503 | El circuito hacia una dependencia está abierto: reintente tras `Retry-After`. |
| `INTERNAL_ERROR` | 500 | Reporte el `trace_id`. |

## Venta embebida (HU33)

1. **Cotizar con el cliente:** `POST /partners/v1/cotizaciones` con `cliente: {tipo_documento, numero_documento}`. El cliente debe
   estar registrado en Solventa, con KYC `VERIFICADO` y el consentimiento Open Finance vigente; si no, 422 con el motivo. Sin
   `cliente` la cotización es informativa (como en HU32) y no se puede suscribir.
2. **Vender:** `POST /partners/v1/suscripciones` con la cotización, el mismo `cliente`, el `metodo_pago` (el token de la pasarela
   que el socio obtuvo al tokenizar; un número de tarjeta se rechaza) y `acepta_terminos`. Responde 202 y el `id` de la venta.
3. **Firmar sin salir de la app del socio:** cuando la venta llega a `COBRO_REALIZADO` y la póliza informa
   `documento_estado: GENERADO` (consulte `GET /polizas/{id}`; antes, el documento responde 409 con el motivo `POLIZA_NO_LISTA`), el socio
   muestra el documento (`documento-firma/pdf`), pide el código (`firma/codigo`) y envía la firma con el código del cliente.
4. **Consultar:** `GET /partners/v1/polizas/{id}` hasta que `estado` sea `EMITIDA`. El cliente recibe el certificado y la
   confirmación por correo (HU22); MS Socios no avisa al socio (no hay webhook): el socio consulta.

El **OTP es simulado y fijo** en el prototipo: el socio podría firmar sin el cliente. Es un riesgo aceptado que debe revisarse con
un proveedor de firma real (ADR-0026).

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
solicitud con la misma clave para comprobar la idempotencia. Con `SOCIO_CLIENTE_DOCUMENTO` (y `SOCIO_CLIENTE_TIPO`, `CC` por
defecto) sigue con la venta completa: suscripción, documento, código, firma y consulta de la póliza emitida. `scripts/carga/medir_latencia.py` mide el sobrecosto de la
fachada frente a Cotización directa (criterio 10).

## Configuración

| Variable | Obligatoria | Notas |
|---|---|---|
| `SOCIOS_DATABASE_URL` o `DATABASE_URL` | Sí | PostgreSQL (esquema `socios`, Alembic). |
| `REDIS_URL` | Sí | Cuota e idempotencia, compartidas entre réplicas. |
| `COTIZACION_URL` | Sí | `http://solventa-backend` en staging. |
| `IDENTIDAD_URL` e `IDENTIDAD_SERVICE_KEY` | Sí | Identifica al cliente por documento (HU33). La clave es una de `SERVICE_API_KEYS` de Identidad. |
| `SUSCRIPCION_URL` y `SUSCRIPCION_SERVICE_KEY` | Sí | Venta embebida (HU33). La clave es una de `SUSCRIPCION_SERVICE_KEYS` de Suscripción. |
| `SOCIOS_JWT_SECRETO` | Sí | ≥ 32 caracteres; propio de Socios. |
| `SOCIOS_JWT_TTL_S` | No | 3600. |
| `SOCIOS_TIMEOUT_COTIZACION_S` | No | 3. |
| `SOCIOS_TIMEOUT_IDENTIDAD_S` | No | 3. |
| `SOCIOS_TIMEOUT_SUSCRIPCION_S` | No | 6. La suma de los tiempos de una operación (Identidad 3 + Suscripción 6) debe quedar por debajo de los 10 s que el socio espera. |
| `SOCIOS_CIRCUITO_UMBRAL_FALLOS` | No | 5 fallos seguidos (timeout, conexión o 5xx) abren el circuito de esa dependencia. |
| `SOCIOS_CIRCUITO_ENFRIAMIENTO_S` | No | 30. Con el circuito abierto, 503 `SERVICE_DEGRADED` con `Retry-After`; vencido, la primera solicitud sondea. |
| `SOLVENTA_MODO_DESARROLLO` | Nunca en staging | Habilita SQLite, almacén en memoria y un secreto JWT de desarrollo. |
