# ADR-0008 — Autenticación provisional del asesor y cotización sin cuenta

- **Estado:** Propuesto (el mecanismo de autenticación es provisional hasta que exista la gestión de usuarios internos y OIDC)
- **Fecha:** 2026-10-20
- **Historia:** HU38 (E01 · Cotización asistida desde web, Sprint 1)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (contextos acotados), [ADR-0003](0003-motor-de-rating-reglas-versionadas-y-migraciones-alembic.md) (motor compartido) y [ADR-0006](0006-desglose-del-precio-persistido-y-acceso-de-auditoria.md) (lo que no se expone).

## Contexto

Un asesor debe cotizar desde el portal web a nombre de un cliente. Hasta ahora no existía el rol de asesor ni un inicio de sesión del personal: Identidad solo conoce clientes (token opaco entregado al registrarse) y servicios (`X-Service-Key`), y `POST /v1/cotizaciones` no tiene autenticación. El diseño de la semana 8 prevé OIDC con PKCE y JWT validado en el API Gateway, pero no está construido. La tarjeta pide que quien no sea asesor no vea la pantalla **ni pueda invocar la API**.

## Decisión

1. **Login del asesor en Identidad.** Tabla `identidad.usuarios_internos` (correo único, nombre, hash bcrypt, `rol`, `activo`) y `POST /v1/sesiones-internas`. Responde un JWT HS256 de 30 minutos (`ASESOR_JWT_TTL_MIN`) con `sub`, `rol`, `nombre`, `iss`, `iat` y `exp`. Los errores de credenciales son siempre 401 con el mismo mensaje y se verifica una contraseña aunque el usuario no exista, para no revelar cuentas por tiempo. No hay renovación ni segundo factor: al vencer, el portal pide iniciar sesión de nuevo y conserva lo ingresado.
2. **Puerto en cada servicio.** Identidad emite y verifica detrás de `EmisorToken`; Cotización verifica detrás de `VerificadorToken` (`app/acceso.py`). Pasar a OIDC es escribir otro adaptador (por ejemplo, validación contra JWKS) sin tocar los endpoints. No se comparte código entre servicios (ADR-0001): el verificador son unas 30 líneas duplicadas a propósito. El secreto (`ASESOR_JWT_SECRETO`) es el único acoplamiento y viene de un secreto del ambiente; sin él, solo se acepta un secreto fijo en modo desarrollo (falla cerrado).
3. **Rutas del asesor agrupadas** bajo `/v1/asesor` con la autorización a nivel de router. Sin token o con token inválido o vencido: 401 `NO_AUTENTICADO` (el portal lo trata como sesión vencida). Con un token válido de otro rol: 403 `SIN_PERMISO`. No se tocó `POST /v1/cotizaciones`, para no cambiar el contrato que usa móvil.
4. **Mismo motor.** `POST /v1/asesor/cotizaciones` llama a `ServicioCotizacion.cotizar`; solo agrega el origen (`canal = WEB_ASESOR`, `asesor_id`). Una prueba verifica que, para la misma entrada y perfil, prima, componentes y coberturas son idénticos a los de `POST /v1/cotizaciones`.
5. **Búsqueda por documento solo en Identidad** (`GET /v1/clientes/busqueda`, rol asesor). Devuelve lo mínimo para identificar al cliente (nombre, inicial del apellido, documento, estado KYC y consentimiento); no devuelve correo ni celular. Cotización nunca lee tablas de Identidad.
6. **Resumen de perfil en niveles.** `GET /v1/asesor/clientes/{id}/perfil` pasa por el mismo camino que la cotización (consentimiento, caché, circuit breaker, trazabilidad HU11) y entrega `BAJO/MEDIO/ALTO` por señal con cortes en `reglas/niveles_perfil.yaml`. No devuelve cifras del cliente. Como efecto útil, deja el perfil en la caché y la cotización siguiente sale con `hit_cache`.
7. **Cotización sin cuenta.** Si el documento no existe en Identidad, el asesor puede cotizar enviando `documento` en lugar de `cliente_id`. Se usa un perfil nulo (`con_consentimiento = false`, sin I/O ni consultas a proveedores), así que la cotización sale `aproximada` con aviso y `perfil_fuente = POR_DEFECTO`. Se guarda el documento consultado y un `cliente_id` de prospecto nuevo para que `GET /v1/cotizaciones/{id}` siga funcionando. El asesor **no otorga** consentimiento a nombre del cliente.
8. **Origen persistido.** Migración `0004` de Cotización: `canal` (por defecto `API`), `asesor_id`, `sin_cuenta`, `documento_tipo`, `documento_numero`. Las cotizaciones anteriores quedan como canal `API`.
9. **Catálogo de productos** (`GET /v1/productos`, público, con `ETag` = versión de reglas), proyectado desde las reglas al arrancar, para que el formulario no copie rangos del YAML.
10. **Perfiles demo** (solo desarrollo): con `SIMULADOR_PERFILES_DEMO`, los simuladores asignan un perfil financiero por `cliente_id`; sin él, todo sigue igual. Permite ver cómo cambia el precio con el consentimiento.

## Consecuencias

- **Pendiente (E11/F26):** gestión real de usuarios internos, roles y permisos; paso a OIDC con PKCE. El rol de auditor sigue con clave de servicio (ADR-0006).
- **El token no se puede revocar** antes de vencer (30 minutos). Aceptable para el prototipo; con OIDC se resuelve con sesiones del proveedor.
- **Secreto compartido:** `ASESOR_JWT_SECRETO` debe ser idéntico en Identidad y Cotización, venir de un secreto y rotarse; con HS256 cualquiera que lo tenga puede emitir tokens.
- **Usuarios:** no hay endpoint para crear asesores; se crean con `scripts/sembrar_demo.py` (demo) o por base de datos. No hay datos reales en el repositorio.
- **Despliegue:** las migraciones `0004` de Identidad y de Cotización deben correr antes de desplegar esta versión. Las variables nuevas están en el README.
- **Idempotencia:** `POST /v1/asesor/cotizaciones`, como el de móvil, no es idempotente; un doble envío crea dos cotizaciones. El portal deshabilita el envío mientras espera.
