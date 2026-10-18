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
| `IDENTIDAD_URL` | cotizacion | URL base de MS Identidad para consultar el consentimiento. Sin ella se usa un stub (solo desarrollo). |
| `IDENTIDAD_SERVICE_KEY` | cotizacion | Clave de servicio que Cotización envía a Identidad. |
| `IDENTIDAD_TIMEOUT_S` | cotizacion | Timeout de la consulta (por defecto 0,3 s, dentro del presupuesto de HA01). |
| `IDENTIDAD_UMBRAL_FALLOS`, `IDENTIDAD_ENFRIAMIENTO_S` | cotizacion | Circuit breaker de la consulta (5 fallos seguidos; 15 s de enfriamiento). |
| `PERFILES_API_KEYS` | cotizacion | Claves (separadas por coma) con permiso para consultar `GET /perfiles/{cliente_id}` (perfil crudo de HU09, con datos financieros) en `X-Service-Key`. Vacía = nadie accede. Es independiente de `AUDITOR_API_KEYS`: cada rol tiene sus claves. Debe venir de un secreto. |
| `AUDITOR_API_KEYS` | cotizacion | Claves (separadas por coma) con permiso de auditoría en `X-Service-Key` para `GET /v1/cotizaciones/{id}/desglose` (HU14, ADR-0006). Vacía = nadie accede. Debe venir de un secreto; admite varias claves para rotarla. |
| `TRAZA_COLA_MAX`, `TRAZA_REINTENTOS` | cotizacion | Cola en memoria de la trazabilidad de consultas (10.000 por defecto) y reintentos de escritura (3). Ver ADR-0007. |

Patrones de comunicación (ADR-0002 y documento de arquitectura): la consulta de consentimiento es REST síncrono con timeout duro y circuit breaker; Kafka se reserva para los flujos de larga duración (suscripción, pagos, siniestros, notificaciones). Los contextos se relacionan por id, nunca por llave foránea ni por tabla compartida.

Si Identidad no responde, Cotización asume `NO_OTORGADO` (falla cerrado): no consulta proveedores y calcula con perfil por defecto.
