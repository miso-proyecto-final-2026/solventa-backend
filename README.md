# solventa-backend

Monorepo de los microservicios de Solventa. Cada servicio es un bounded context del
[ADR-0001](docs/adr/0001-descomposicion-microservicios-bounded-contexts.md): se construye, se prueba y se despliega
por separado, tiene su propio paquete `app`, su `Dockerfile` y su propio esquema de base de datos.

```
services/
  identidad/     MS Identidad    HU01–HU08 (registro, KYC, consentimiento)
  cotizacion/    MS Cotización   HU09–HU14, HU37–HU38 (proveedores de datos, perfil, motor de rating)
docs/adr/        decisiones de arquitectura (compartidas)
pyproject.toml   configuración de ruff
requirements-dev.txt   dependencias de prueba comunes
```

Los demás servicios del ADR (Perfilamiento, Suscripción, Pagos, Siniestros, Socios) se agregan como
`services/<nombre>/` con la misma forma.

## Reglas de frontera

- Un servicio **no importa código de otro** ni lee sus tablas. Se relacionan por identificador, por API (REST
  síncrono) o por evento (ADR-0002).
- Cada servicio es dueño de su esquema de PostgreSQL y lo versiona con sus propias migraciones.
- Lo que dos servicios necesiten compartir se expone como contrato de API, no como paquete común.

## Trabajar con un servicio

```bash
cd services/identidad          # o services/cotizacion
pip install -r requirements.txt -r ../../requirements-dev.txt
pytest                         # pruebas de ese servicio
uvicorn app.main:app --reload
cd ../.. && ruff check services/identidad   # el lint se corre desde la raíz
```

El CI corre `test` y `build` en matriz por servicio.

## Configuración entre servicios

| Variable | Servicio | Uso |
|---|---|---|
| `DATABASE_URL` | identidad | Base de datos; el esquema es `identidad` (Alembic: `alembic upgrade head`). |
| `SERVICE_API_KEYS` | identidad | Claves (separadas por coma) que aceptan los servicios internos en `X-Service-Key`. |
| `KYC_PROVEEDOR` | identidad | Proveedor de KYC: `simulado` (por defecto) o `proveedor_b`. Cambiarlo no requiere tocar código (HA06). |
| `KYC_PROVEEDOR_B_URL`, `KYC_PROVEEDOR_B_API_KEY` | identidad | URL y clave del proveedor B; obligatorias si `KYC_PROVEEDOR=proveedor_b`. La clave debe venir de un secreto, nunca de git. |
| `KYC_PROVEEDOR_B_TIMEOUT_S` | identidad | Timeout del cliente HTTP de B (4 s por defecto, por debajo del `KYC_TIMEOUT_S` del núcleo). |
| `SOLVENTA_MODO_DESARROLLO` | cotizacion | `1` habilita los sustitutos locales (stub de consentimiento, caché en memoria, SQLite). **Solo desarrollo**: sin ella, si falta `IDENTIDAD_URL`, `REDIS_URL` o `COTIZACION_DATABASE_URL` el servicio no arranca. |
| `COTIZACION_DATABASE_URL` | cotizacion | Base de datos PostgreSQL (esquema `cotizacion`, Alembic). Obligatoria fuera de desarrollo; admite `DATABASE_URL` como alternativa. |
| `REDIS_URL` | cotizacion | Caché de perfiles (cache-aside, HA01). Obligatoria fuera de desarrollo. |
| `IDENTIDAD_URL` | cotizacion | URL base de MS Identidad para consultar el consentimiento. Obligatoria fuera de desarrollo. |
| `IDENTIDAD_SERVICE_KEY` | cotizacion | Clave de servicio que Cotización envía a Identidad. |
| `IDENTIDAD_TIMEOUT_S` | cotizacion | Timeout de la consulta (por defecto 0,3 s, dentro del presupuesto de HA01). |
| `IDENTIDAD_UMBRAL_FALLOS`, `IDENTIDAD_ENFRIAMIENTO_S` | cotizacion | Circuit breaker de la consulta (5 fallos seguidos; 15 s de enfriamiento). |
| `AUDITOR_API_KEYS` | cotizacion | Claves (separadas por coma) con permiso de auditoría en `X-Service-Key` para `GET /v1/cotizaciones/{id}/desglose` (HU14, ADR-0006). Vacía = nadie accede. Debe venir de un secreto; admite varias claves para rotarla. |
| `TRAZA_COLA_MAX`, `TRAZA_REINTENTOS` | cotizacion | Cola en memoria de la trazabilidad de consultas (10.000 por defecto) y reintentos de escritura (3). Ver ADR-0007. |

Patrones de comunicación (ADR-0002 y documento de arquitectura): la consulta de consentimiento es REST síncrono con timeout duro y circuit breaker; Kafka se reserva para los flujos de larga duración (suscripción, pagos, siniestros, notificaciones). Los contextos se relacionan por id, nunca por llave foránea ni por tabla compartida.

Si Identidad no responde, Cotización asume `NO_OTORGADO` (falla cerrado): no consulta proveedores y calcula con perfil por defecto.

## Despliegue (referencia para `solventa-infra`)

Qué debe proveer el ambiente (staging, producción) para que cada servicio arranque. Los servicios fallan al arrancar si falta configuración obligatoria; no degradan en silencio.

### MS Cotización

| Variable | Obligatoria | Tipo | Notas |
|---|---|---|---|
| `COTIZACION_DATABASE_URL` (o `DATABASE_URL`) | Sí | Secreto (lleva credenciales) | PostgreSQL. El esquema `cotizacion` lo crea Alembic. |
| `REDIS_URL` | Sí | Config | ElastiCache. Caché de perfiles (cache-aside, HA01). |
| `IDENTIDAD_URL` | Sí | Config | URL interna de MS Identidad (consulta de consentimiento). |
| `IDENTIDAD_SERVICE_KEY` | Sí | Secreto | Debe ser una de las claves de `SERVICE_API_KEYS` de Identidad. |
| `AUDITOR_API_KEYS` | No | Secreto | Sin ella nadie accede al desglose (`GET /v1/cotizaciones/{id}/desglose`). |
| `REGLAS_VERSION`, `REGLAS_DIR` | No | Config | Por defecto `v1`, incluida en la imagen. `REGLAS_DIR` permite montar reglas externas. |
| `PROVEEDORES_*`, `IDENTIDAD_TIMEOUT_S`, `IDENTIDAD_UMBRAL_FALLOS`, `IDENTIDAD_ENFRIAMIENTO_S` | No | Config | Timeouts, circuit breaker y TTL de la caché; los valores por defecto están alineados con el presupuesto de HA01. |
| `TRAZA_COLA_MAX`, `TRAZA_REINTENTOS` | No | Config | Cola en memoria y reintentos de la trazabilidad de consultas (HU11, ADR-0007). |
| `SOLVENTA_MODO_DESARROLLO` | **No definirla** | — | Habilita sustitutos locales (consentimiento siempre otorgado, caché en memoria, SQLite). Nunca en staging ni en producción. |

### MS Identidad

| Variable | Obligatoria | Tipo | Notas |
|---|---|---|---|
| `DATABASE_URL` | Sí | Secreto | PostgreSQL, esquema `identidad`. |
| `SERVICE_API_KEYS` | Sí | Secreto | Claves aceptadas en `X-Service-Key`; incluye la que usa Cotización. |
| `KYC_PROVEEDOR` | No | Config | `simulado` (por defecto) o `proveedor_b`. |
| `KYC_PROVEEDOR_B_URL`, `KYC_PROVEEDOR_B_API_KEY` | Si `KYC_PROVEEDOR=proveedor_b` | Config / secreto | La clave nunca va en git. |

### Comportamiento al arrancar

- **Falta configuración obligatoria de Cotización:** el proceso termina con `ErrorConfiguracion` que nombra la variable. En Kubernetes se verá como `CrashLoopBackOff`; es intencional: un pod mal configurado no debe recibir tráfico.
- **Reglas de rating inválidas o inexistentes:** tampoco arranca (se validan al cargar).
- **`GET /health`** responde `ok` sin consultar Postgres, Redis ni Identidad: sirve como liveness, no como prueba de que las dependencias estén sanas.

### Migraciones

La imagen incluye `alembic.ini` y `migrations/`, pero **no ejecuta las migraciones al iniciar**. Hay que correr `alembic upgrade head` (con `COTIZACION_DATABASE_URL` definida) antes de promover una versión que cambie el esquema, por ejemplo como Job previo al rollout. Alembic solo corre contra PostgreSQL (rechaza SQLite); la migración `0002` (HU14) crea un trigger que hace el desglose de solo inserción.

### Puntos a confirmar en `solventa-infra`

- El pipeline (`ci.yml`, job `deploy-staging`) publica solo la imagen de `services/cotizacion` en el repositorio ECR `solventa-backend`; Identidad aún no se despliega.
- Contenedor escucha en el puerto `8000`.
- Los secretos deben llegar desde el gestor de secretos del ambiente (no ConfigMap ni git).
