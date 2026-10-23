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
| MS Identidad / Consentimientos | `POST /v1/clientes/{id}/consentimientos`, `GET /v1/clientes/{id}/consentimientos/{tipo}` (cliente y servicio; `id` del consentimiento; 401/403/404/422) |
| MS Identidad / Revocación de consentimiento (HU07) | `DELETE /v1/clientes/{id}/consentimientos/{tipo}` (revocar, repetir con la misma fecha, consultar, 401/403/404/422) y la cotización posterior, que sale `aproximada`. Usa el segundo cliente para no quitar el consentimiento que otras carpetas necesitan. |
| MS Identidad / KYC | `POST /v1/clientes/{id}/verificaciones` (multipart), `GET /v1/clientes/{id}/verificacion` (idempotencia, 401/403/404/415/422) |
| MS Cotización / Salud | `GET /health` (y cabecera `X-Correlation-Id`) |
| MS Cotización / Cotizaciones | `POST /v1/cotizaciones`, `GET /v1/cotizaciones/{id}`, `PUT /v1/cotizaciones/{id}/latencia-cliente` (ambos productos, validaciones, `X-Correlation-Id`, 404) |
| MS Cotización / Auditoría | `GET /v1/cotizaciones/{id}/desglose` y `GET /v1/cotizaciones/{id}/consultas-externas` (auditor; 401/403/404/422) |
| MS Perfilamiento (HU23) | `POST /v1/perfilamiento/{id}/enriquecer`, `GET …/perfil` y `GET …/linaje` (con credencial; 401/403/404/422) |
| MS Cotización / Perfiles | `GET /perfiles/{cliente_id}` (exige clave de servicio; 401/403) |
| MS Identidad / Asesor - login y búsqueda (HU38) | `POST /v1/sesiones-internas`, `GET /v1/clientes/busqueda` (200/401/404/422) |
| MS Cotización / Cotización asistida (HU38) | `GET /v1/productos` (200/304), `GET /v1/asesor/clientes/{id}/perfil`, `POST /v1/asesor/cotizaciones` (cliente existente, sin cuenta, 401/422) |

Cada ruta de ambas apps tiene al menos una petición, y cada código de respuesta que alcanza un cliente tiene un caso que
lo provoca. Quedan fuera, porque dependen de configuración que la colección no cambia:

- `429` y `RECHAZADO` en KYC (requieren `KYC_SIMULADOR_MODO=rechazo` para agotar los intentos).
- `413` en KYC (imagen de más de 5 MB; no se versiona un archivo así).
- `400` «Se requiere HTTPS» en KYC (solo con `KYC_EXIGIR_HTTPS=true`).
- `403` de las rutas del asesor (HU38): exige un token válido con otro rol, y Identidad solo emite el de asesor.
- Caídas de proveedores (`TIMEOUT`, `DEGRADADO` en consultas externas; `degradado: true`), que requieren los modos de falla de los simuladores de Cotización.

## Levantar los servicios en local

Desde la raíz del repo, con las dependencias de cada servicio instaladas
(`pip install -r services/<servicio>/requirements.txt`). Las claves de abajo son de desarrollo; no las uses fuera de local.

```bash
# MS Identidad (puerto 8001) — SQLite local por defecto
cd services/identidad
SERVICE_API_KEYS=dev-service-key ASESOR_JWT_SECRETO=dev-secreto-asesor-de-al-menos-32-bytes \nuvicorn app.main:app --port 8001

# Una vez (HU38): asesor demo y cinco clientes demo (CC 1000000001 a 1000000005)
ASESOR_DEMO_EMAIL=asesor@solventa.test ASESOR_DEMO_PASSWORD=<clave-de-desarrollo> python scripts/sembrar_demo.py

# MS Cotización (puerto 8000) — en otra terminal
cd services/cotizacion
SOLVENTA_MODO_DESARROLLO=1 ASESOR_JWT_SECRETO=dev-secreto-asesor-de-al-menos-32-bytes \
IDENTIDAD_URL=http://localhost:8001 IDENTIDAD_SERVICE_KEY=dev-service-key \
AUDITOR_API_KEYS=dev-auditor-key PERFILES_API_KEYS=dev-perfiles-key \
uvicorn app.main:app --port 8000

# MS Perfilamiento (puerto 8002, HU23) — en otra terminal
cd services/perfilamiento
SOLVENTA_MODO_DESARROLLO=1 PERFILAMIENTO_API_KEYS=postman=dev-perfilamiento-key \
uvicorn app.main:app --port 8002
```

Sin `IDENTIDAD_URL`, Perfilamiento usa el consentimiento «siempre otorgado» (solo desarrollo). Con ella, un cliente sin consentimiento vigente recibe 403 `CONSENTIMIENTO_NO_VIGENTE` y el intento queda registrado con el nombre de la clave (`postman`) como origen.

Con `IDENTIDAD_URL` definida, Cotización consulta el consentimiento real en Identidad (si el cliente no lo otorgó,
cotiza con el perfil por defecto: la cotización sale `aproximada` con `aviso`, y cada proveedor omitido queda auditado como
`SIN_CONSENTIMIENTO`). Si la omites, `SOLVENTA_MODO_DESARROLLO=1` usa un stub que siempre otorga el consentimiento; en ese
caso pon `identidad_integrada` en `false` para que las pruebas que dependen de Identidad no se evalúen.
En PowerShell define las variables con `$env:NOMBRE = "valor"` antes de `uvicorn`.

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
| `perfiles_key` | Entorno | `X-Service-Key` del perfil crudo; debe estar en `PERFILES_API_KEYS` de Cotización. Es independiente de la del auditor. |
| `asesor_email`, `asesor_password` | Entorno | Credenciales del asesor demo (las de `sembrar_demo.py`). La clave se escribe en el entorno de Postman, nunca en git. |
| `asesor_token`, `cliente_demo_id`, `catalogo_etag` | Colección | Las deja la carpeta de HU38 para las peticiones siguientes. |
| `identidad_integrada` | Entorno | `true` si Cotización corre con `IDENTIDAD_URL`; `false` con el stub de consentimiento. |
| `cliente_id`, `access_token` | «Registrar cliente» | Cliente principal y su sesión. |
| `otro_cliente_id`, `otro_access_token` | «Registrar segundo cliente» | Para las pruebas 403 y los casos negativos de KYC. Solo la carpeta de HU07 le otorga y le revoca el consentimiento (queda `REVOCADO`). |
| `consentimiento_id` | «Otorgar consentimiento» | Se compara con el `consentimiento_id` que Cotización deja en la trazabilidad. |
| `cotizacion_id` | «Crear cotización - microseguro de vida» | Consultas, latencia, desglose y consultas externas. |
| `correlacion_id`, `cotizacion_correlada_id` | «Crear cotización - con X-Correlation-Id» | Verifican que la correlación llega a los registros de auditoría. |
| `cotizacion_sin_consentimiento_id` | «Crear cotización - cliente sin consentimiento» | Verifica las omisiones auditadas. |

El documento, correo y celular de los clientes se generan en cada ejecución, así que la colección se puede repetir sin
chocar con registros previos.

## Notas

- **Orden:** las carpetas dependen de las variables que deja la anterior. Si ejecutas una petición suelta, corre antes
  «Registrar cliente» (Identidad) y «Crear cotización - microseguro de vida» (Cotización).
- **Resultado del KYC:** depende del simulador de Identidad (`KYC_SIMULADOR_MODO`): `exito` (por defecto) → `VERIFICADO`;
  `rechazo` → `RECHAZADO`; `timeout` o `error_5xx` → `EN_REVISION`. Las pruebas aceptan los tres estados, salvo
  «cliente ya verificado», que espera 409 solo si el cliente quedó `VERIFICADO`.
- **Consistencia eventual:** la trazabilidad de consultas externas se escribe de forma asíncrona; por eso esas peticiones
  van después de las que generan los registros. Si ejecutas «Consultar consultas externas» justo tras cotizar y sale
  vacía, repite la petición.
- **Idempotencia del alta:** la caché de `Idempotency-Key` de Identidad vive en memoria del proceso (5 min); reiniciar el
  servicio la vacía.
- **Sin secretos:** las claves del entorno son valores de desarrollo local. Para otro ambiente, crea un entorno aparte y
  no subas sus claves a git.
