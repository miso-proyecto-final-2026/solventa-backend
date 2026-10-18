# Colección de Postman — solventa-backend

Pruebas manuales y automatizables de los endpoints de **MS Identidad** y **MS Cotización**.
Cada petición lleva pruebas (`pm.test`) y guarda las variables que necesita la siguiente
(`cliente_id`, `access_token`, `cotizacion_id`, …), así que la colección se puede ejecutar completa y en orden.

| Archivo | Contenido |
|---|---|
| `solventa-backend.postman_collection.json` | La colección (v2.1), con valores por defecto para ejecutar en local. |
| `solventa-backend-local.postman_environment.json` | Entorno «Local» con URLs y claves de desarrollo. |
| `fixtures/` | Imágenes mínimas para los endpoints de KYC (JPEG, PNG y dos archivos inválidos a propósito). |

## Qué cubre

| Carpeta | Endpoints |
|---|---|
| MS Identidad / Salud | `GET /health` |
| MS Identidad / Clientes | `POST /v1/clientes` (alta, idempotencia, duplicado, validaciones) |
| MS Identidad / Consentimientos | `POST /v1/clientes/{id}/consentimientos`, `GET /v1/clientes/{id}/consentimientos/{tipo}` (cliente y servicio; 401/403/404/422) |
| MS Identidad / KYC | `POST /v1/clientes/{id}/verificaciones` (multipart), `GET /v1/clientes/{id}/verificacion` (idempotencia, 401/403/415/422) |
| MS Cotización / Salud | `GET /health` |
| MS Cotización / Cotizaciones | `POST /v1/cotizaciones`, `GET /v1/cotizaciones/{id}`, `PUT /v1/cotizaciones/{id}/latencia-cliente` (ambos productos, validaciones, 404) |
| MS Cotización / Desglose | `GET /v1/cotizaciones/{id}/desglose` (auditor; 401/403/404) |
| MS Cotización / Perfiles | `GET /perfiles/{cliente_id}` |

## Levantar los servicios en local

Desde la raíz del repo, con las dependencias de cada servicio instaladas
(`pip install -r services/<servicio>/requirements.txt`). Las claves de abajo son de desarrollo; no las uses fuera de local.

```bash
# MS Identidad (puerto 8001) — SQLite local por defecto
cd services/identidad
SERVICE_API_KEYS=dev-service-key uvicorn app.main:app --port 8001

# MS Cotización (puerto 8000) — en otra terminal
cd services/cotizacion
SOLVENTA_MODO_DESARROLLO=1 \
IDENTIDAD_URL=http://localhost:8001 IDENTIDAD_SERVICE_KEY=dev-service-key \
AUDITOR_API_KEYS=dev-auditor-key \
uvicorn app.main:app --port 8000
```

Con `IDENTIDAD_URL` definida, Cotización consulta el consentimiento real en Identidad (si el cliente no lo otorgó,
cotiza con perfil por defecto: `degradado` y `aproximada`). Si la omites, `SOLVENTA_MODO_DESARROLLO=1` usa un stub que
siempre otorga el consentimiento. En PowerShell define las variables con `$env:NOMBRE = "valor"` antes de `uvicorn`.

## Usarla

**Postman:** importa los dos archivos `.postman_*.json`, selecciona el entorno «Solventa Backend - Local» y ejecuta la
colección con el Collection Runner. Para los endpoints de KYC, en *Settings → General → Working directory* apunta a esta
carpeta `postman/`, para que Postman encuentre `fixtures/`.

**Newman (CLI):**

```bash
cd postman
npx newman run solventa-backend.postman_collection.json \
  -e solventa-backend-local.postman_environment.json --working-dir .
```

## Variables

| Variable | Origen | Uso |
|---|---|---|
| `identidad_url`, `cotizacion_url` | Entorno | URL base de cada servicio (por defecto `localhost:8001` y `localhost:8000`). |
| `service_key` | Entorno | `X-Service-Key`; debe estar en `SERVICE_API_KEYS` de Identidad. |
| `auditor_key` | Entorno | `X-Service-Key` del auditor; debe estar en `AUDITOR_API_KEYS` de Cotización. |
| `cliente_id`, `access_token` | «Registrar cliente» | Cliente principal y su sesión. |
| `otro_cliente_id`, `otro_access_token` | «Registrar segundo cliente» | Para las pruebas 403 y los casos negativos de KYC. |
| `cotizacion_id` | «Crear cotización - microseguro de vida» | Consultas, latencia y desglose. |

El documento, correo y celular de los clientes se generan en cada ejecución, así que la colección se puede repetir sin
chocar con registros previos.

## Notas

- **Orden:** las carpetas dependen de las variables que deja la anterior. Si ejecutas una petición suelta, corre antes
  «Registrar cliente» (Identidad) y «Crear cotización - microseguro de vida» (Cotización).
- **Resultado del KYC:** depende del simulador de Identidad (`KYC_SIMULADOR_MODO`): `exito` (por defecto) → `VERIFICADO`;
  `rechazo` → `RECHAZADO`; `timeout` o `error_5xx` → `EN_REVISION`. Las pruebas aceptan los tres estados, salvo
  «cliente ya verificado», que espera 409 solo si el cliente quedó `VERIFICADO`.
- **Idempotencia del alta:** la caché de `Idempotency-Key` de Identidad vive en memoria del proceso (5 min); reiniciar el
  servicio la vacía.
- **Sin secretos:** las claves del entorno son valores de desarrollo local. Para otro ambiente, crea un entorno aparte y
  no subas sus claves a git.
