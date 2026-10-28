# solventa-backend

Monorepo de los microservicios de Solventa. Cada servicio es un bounded context del
[ADR-0001](docs/adr/0001-descomposicion-microservicios-bounded-contexts.md): se construye, se prueba y se despliega
por separado, tiene su propio paquete `app`, su `Dockerfile` y su propio esquema de base de datos.

```
services/
  identidad/     MS Identidad    HU01–HU08 (registro, KYC, consentimiento)
  cotizacion/    MS Cotización   HU09–HU14, HU37–HU38 (proveedores de datos, perfil, motor de rating)
  perfilamiento/ MS Perfilamiento HU23 (enriquecimiento del perfil de riesgo, linaje)
  suscripcion/   MS Suscripción   HU15 y HU18 (saga de suscripción: decisión automática y paso de cobro; firma y emisión llegan con HU21 y HU20)
  siniestros/    MS Siniestros   HU27 (recepción de eventos paramétricos; HU28 validará la condición)
  pagos/         MS Pagos        HU18 (cobro de la prima con pasarela simulada, idempotencia y conciliación)
docs/adr/        decisiones de arquitectura (compartidas)
pyproject.toml   configuración de ruff
requirements-dev.txt   dependencias de prueba comunes
```

Los demás servicios del ADR (hoy solo Socios) se agregan como
`services/<nombre>/` con la misma forma.

## Reglas de frontera

- Un servicio **no importa código de otro** ni lee sus tablas. Se relacionan por identificador, por API (REST
  síncrono) o por evento (ADR-0002).
- Cada servicio es dueño de su esquema de PostgreSQL y lo versiona con sus propias migraciones.
- Lo que dos servicios necesiten compartir se expone como contrato de API, no como paquete común.

## Trabajar con un servicio

```bash
cd services/identidad          # o services/cotizacion, services/perfilamiento, services/suscripcion, services/siniestros, services/pagos
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
| `PERFILES_API_KEYS` | cotizacion | Claves (separadas por coma) con permiso para consultar `GET /perfiles/{cliente_id}` (perfil crudo de HU09, con datos financieros) en `X-Service-Key`. Vacía = nadie accede. Es independiente de `AUDITOR_API_KEYS`: cada rol tiene sus claves. Debe venir de un secreto. |
| `AUDITOR_API_KEYS` | cotizacion | Claves (separadas por coma) con permiso de auditoría en `X-Service-Key` para `GET /v1/cotizaciones/{id}/desglose` (HU14, ADR-0006). Vacía = nadie accede. Debe venir de un secreto; admite varias claves para rotarla. |
| `PERFILAMIENTO_DATABASE_URL` | perfilamiento | Base de datos PostgreSQL (esquema `perfilamiento`, Alembic). Obligatoria fuera de desarrollo; admite `DATABASE_URL`. |
| `PERFILAMIENTO_API_KEYS` | perfilamiento | Claves de servicio en `X-Service-Key`, como pares `nombre=clave` separados por coma; el nombre queda como «origen» de los intentos bloqueados. Vacía = nadie accede. Debe venir de un secreto. |
| `REDIS_URL`, `IDENTIDAD_URL`, `IDENTIDAD_SERVICE_KEY` | perfilamiento | Igual que en cotización: caché de fuentes y consulta de consentimiento; obligatorias fuera de desarrollo. |
| `PROVEEDORES_TTL_OPEN_FINANCE_S`, `PROVEEDORES_TTL_OPEN_DATA_S` | perfilamiento | TTL por tipo de dato (3.600 s y 86.400 s por defecto), con dispersión de ±10–15 %. Ver ADR-0009. |
| `SUSCRIPCION_DATABASE_URL` | suscripcion | Base de datos PostgreSQL (esquema `suscripcion`, Alembic). Obligatoria fuera de desarrollo; admite `DATABASE_URL`. |
| `IDENTIDAD_URL`, `IDENTIDAD_SERVICE_KEY` | suscripcion | MS Identidad y la clave de servicio con la que Suscripción valida el token del cliente (`GET /v1/sesion/actual`). `IDENTIDAD_URL` es obligatoria siempre (no hay sustituto). |
| `COTIZACION_URL` | suscripcion | MS Cotización, de donde se lee la cotización (`GET /v1/cotizaciones/{id}`). Obligatoria siempre. |
| `PERFILAMIENTO_URL`, `PERFILAMIENTO_SERVICE_KEY` | suscripcion | MS Perfilamiento y su clave de servicio (debe estar en su `PERFILAMIENTO_API_KEYS`). Si Perfilamiento no responde, el caso se deriva a revisión asistida. |
| `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SEGURIDAD`, `AWS_REGION` | suscripcion | Brokers de MSK que la plataforma inyecta al servicio, `IAM` (por defecto) o `PLAINTEXT`, y la región para firmar. Sin brokers solo se arranca en modo desarrollo, con un publicador en memoria. Ver ADR-0010. |
| `SUSCRIPCION_ESPERA_BARRIDO_S`, `SUSCRIPCION_TIMEOUT_S` | suscripcion | Segundos tras los que el barrido retoma una saga sin decidir (30) y timeout de las llamadas a servicios vecinos (2). |
| `SINIESTROS_DATABASE_URL` | siniestros | Base de datos PostgreSQL (esquema `siniestros`, Alembic). Obligatoria fuera de desarrollo; admite `DATABASE_URL`. |
| `SINIESTROS_API_KEYS` | siniestros | Llaves de los proveedores de eventos en `X-Service-Key`, como pares `fuente=clave` separados por coma; el evento debe traer esa misma `fuente`. Una clave sin nombre (la que entrega la plataforma en staging) es de la fuente `proveedor-simulado`. Vacía = nadie accede. Debe venir de un secreto. |
| `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SEGURIDAD` | siniestros | Brokers de MSK (los inyecta la plataforma a los servicios con `eventos`) y `IAM` (por defecto) o `PLAINTEXT` (solo local). Obligatorias fuera de desarrollo; sin ellas, en desarrollo se publica en memoria y no hay consumidor. Ver ADR-0010 y ADR-0011. |
| `PAGOS_DATABASE_URL` | pagos | Base de datos PostgreSQL (esquema `pagos`, Alembic). Obligatoria fuera de desarrollo; admite `DATABASE_URL`. |
| `PAGOS_API_KEYS` | pagos | Claves de servicio en `X-Service-Key` (pares `nombre=clave` separados por coma; sin nombre, «servicio») para `POST /v1/pagos/cobros`, `POST /v1/pagos/tokenizar` y `GET /v1/pagos/transacciones/{id}`; no abre los cobros fallidos (HU19), que exigen el JWT del asesor u operador. Vacía = nadie accede. Debe venir de un secreto. |
| `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SEGURIDAD`, `AWS_REGION` | pagos | Como en suscripción. Con brokers, Pagos consume `solventa.suscripcion.comandos` (grupo `solventa-pagos.cobros`) y publica en `solventa.pagos.resultados`; sin ellos solo arranca en desarrollo, sin consumidor. |
| `PASARELA_PROVEEDOR`, `PASARELA_TIMEOUT_S` | pagos | Adaptador de pasarela (`simulador`, el único hoy; ADR-0004 y ADR-0014) y tiempo máximo de espera de su respuesta (5 s). |
| `PASARELA_SIM_MODO`, `PASARELA_SIM_DEMORA_S` | pagos | Modo por defecto del simulador (`APROBAR`, `RECHAZAR`, `TARDAR`, `PERDER_RESPUESTA`) y demora del modo `TARDAR` (10 s). Ver `services/pagos/README.md`. |
| `PERIODO_GRACIA_DIAS` | pagos | Días del período de gracia tras un rechazo. **Provisional** (3 por defecto) hasta que negocio lo confirme; la gestión es de HU19. |
| `PAGOS_HORAS_POR_VENCER` | pagos | Horas de gracia restantes por debajo de las cuales un cobro fallido sale «próximo a vencer» en la bandeja (24 por defecto, HU19; supuesto a confirmar). |
| `PAGOS_DETECTOR_GRACIA_S` | pagos | Cada cuántos segundos se buscan cobros fallidos con la gracia vencida para avisar al operador (60 por defecto, HU19). |
| `PAGOS_REINTENTO_BASE_S`, `PAGOS_REINTENTO_MAXIMO_S` | pagos | Espera antes de reintentar o conciliar un cobro sin respuesta (5 s, se duplica en cada intento, tope 300 s). |
| `TRAZA_COLA_MAX`, `TRAZA_REINTENTOS` | cotizacion | Cola en memoria de la trazabilidad de consultas (10.000 por defecto) y reintentos de escritura (3). Ver ADR-0007. |
| `ASESOR_JWT_SECRETO` | identidad, cotizacion, pagos | Secreto HS256 con el que Identidad firma y Cotización y Pagos validan el token del asesor u operador (HU38, HU19, ADR-0008). **Debe ser el mismo en los tres** y venir de un secreto. Sin él solo arranca con `SOLVENTA_MODO_DESARROLLO=1` (secreto fijo de desarrollo). |
| `ASESOR_JWT_TTL_MIN` | identidad | Vida del token del asesor en minutos (30 por defecto). |
| `ASESOR_JWT_EMISOR` | cotizacion, pagos | `iss` esperado en el token (`solventa-identidad` por defecto). |
| `SIMULADOR_PERFILES_DEMO` | cotizacion | Ruta del YAML con perfiles financieros demo por `cliente_id` (`app/proveedores/perfiles_demo.yaml`). **Solo desarrollo y demostración.** Sin ella los simuladores devuelven el perfil neutro de siempre. |
| `ASESOR_DEMO_EMAIL`, `ASESOR_DEMO_PASSWORD` | identidad | Entrada de `scripts/sembrar_demo.py`, que crea el asesor demo y cinco clientes demo (CC `1000000001` a `1000000005`). Nunca en git. |
| `OPERADOR_DEMO_EMAIL`, `OPERADOR_DEMO_PASSWORD` | identidad | Opcionales (las dos o ninguna): `sembrar_demo.py` crea además un usuario interno con rol `OPERADOR` (HU19). Nunca en git. |

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
| `ASESOR_JWT_SECRETO` | Sí | Secreto | Mismo valor que en Identidad. Valida el token del asesor (HU38, ADR-0008). Sin él, el servicio no arranca fuera de desarrollo. |
| `ASESOR_JWT_EMISOR` | No | Config | `solventa-identidad` por defecto. |
| `SIMULADOR_PERFILES_DEMO` | **No definirla** | — | Perfiles demo por cliente; solo desarrollo y demostración. |
| `REGLAS_VERSION`, `REGLAS_DIR` | No | Config | Por defecto `v1`, incluida en la imagen. `REGLAS_DIR` permite montar reglas externas. |
| `PROVEEDORES_*`, `IDENTIDAD_TIMEOUT_S`, `IDENTIDAD_UMBRAL_FALLOS`, `IDENTIDAD_ENFRIAMIENTO_S` | No | Config | Timeouts, circuit breaker y TTL de la caché; los valores por defecto están alineados con el presupuesto de HA01. |
| `TRAZA_COLA_MAX`, `TRAZA_REINTENTOS` | No | Config | Cola en memoria y reintentos de la trazabilidad de consultas (HU11, ADR-0007). |
| `SOLVENTA_MODO_DESARROLLO` | **No definirla** | — | Habilita sustitutos locales (consentimiento siempre otorgado, caché en memoria, SQLite). Nunca en staging ni en producción. |

### MS Identidad

| Variable | Obligatoria | Tipo | Notas |
|---|---|---|---|
| `DATABASE_URL` | Sí | Secreto | PostgreSQL, esquema `identidad`. |
| `SERVICE_API_KEYS` | Sí | Secreto | Claves aceptadas en `X-Service-Key`; incluye la que usa Cotización. |
| `ASESOR_JWT_SECRETO` | Sí | Secreto | Firma el token del asesor (HU38, ADR-0008); el mismo valor que en Cotización. |
| `ASESOR_JWT_TTL_MIN` | No | Config | Vida del token en minutos (30 por defecto). |
| `KYC_PROVEEDOR` | No | Config | `simulado` (por defecto) o `proveedor_b`. |
| `KYC_PROVEEDOR_B_URL`, `KYC_PROVEEDOR_B_API_KEY` | Si `KYC_PROVEEDOR=proveedor_b` | Config / secreto | La clave nunca va en git. |

### Comportamiento al arrancar

- **Falta configuración obligatoria de Cotización:** el proceso termina con `ErrorConfiguracion` que nombra la variable. En Kubernetes se verá como `CrashLoopBackOff`; es intencional: un pod mal configurado no debe recibir tráfico.
- **Reglas de rating inválidas o inexistentes:** tampoco arranca (se validan al cargar).
- **`GET /health`** responde `ok` sin consultar Postgres, Redis ni Identidad: sirve como liveness, no como prueba de que las dependencias estén sanas.

### Migraciones

La imagen incluye `alembic.ini` y `migrations/`, pero **no ejecuta las migraciones al iniciar**. Hay que correr `alembic upgrade head` (con `COTIZACION_DATABASE_URL` definida) antes de promover una versión que cambie el esquema, por ejemplo como Job previo al rollout. Alembic solo corre contra PostgreSQL (rechaza SQLite); la migración `0002` (HU14) crea un trigger que hace el desglose de solo inserción. HU38 agrega la `0004` en **ambos** servicios (`usuarios_internos` en Identidad; columnas de origen en `cotizaciones`): corren antes del rollout.

### Puntos a confirmar en `solventa-infra`

- El despliegue a staging es manual y por demanda: un colaborador comenta `cicd/deploy-staging` en un PR y `deploy-staging.yml` publica en ECR, migra y hace el rollout de cada servicio que el PR modifica (mapa en `.github/deploy-services.json`). Requiere la variable de repositorio `AWS_DEPLOY_ROLE_ARNS` (JSON `{servicio: arn}`, salida `service_deploy_role_arns` del bootstrap de solventa-infra).
- Contenedor escucha en el puerto `8000`.
- Los secretos deben llegar desde el gestor de secretos del ambiente (no ConfigMap ni git).
