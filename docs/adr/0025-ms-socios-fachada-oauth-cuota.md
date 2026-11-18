# ADR-0025: MS Socios — fachada versionada con OAuth2, cuota e idempotencia en el servicio

- **Estado:** Aceptado (HU32, Sprint 3)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (Socios es dueño de `Cliente_API` y
  `Contrato_API` y no guarda datos de negocio), [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (la API de socios es
  síncrona) y [ADR-0003](0003-motor-de-rating-reglas-versionadas-y-migraciones-alembic.md) (el motor de rating lo comparten la app, la web y los socios)

## Contexto

Un socio de distribución necesita consumir la cotización desde su propio canal. La arquitectura de las 8 semanas pone la
autenticación OAuth2, el control de cuota y los contratos versionados en el API Gateway. El borde real es API Gateway HTTP (v2): no
tiene planes de uso ni cuotas por cliente, y hoy no valida OIDC (hallazgo B11 de HA05). Cada servicio valida su propia credencial.

## Decisión

1. **Socios es la fachada.** `services/socios` expone `/oauth/token` y `/partners/v1/*`, y llama a Cotización por REST
   (`POST /v1/cotizaciones`). Con eso se cumple EC-I1.1: no cambia ningún servicio de negocio.
2. **OAuth2 client credentials en el servicio.** `POST /oauth/token` acepta las credenciales en el cuerpo o por HTTP Basic, verifica el
   secreto con bcrypt y emite un JWT HS256 de 1 hora.
   - El token lleva `iss=solventa-socios`, `aud=solventa-partners`, `sub=client_id` y `scope`, y lo firma un secreto propio de Socios
     (`SOCIOS_JWT_SECRETO`), distinto del secreto del asesor.
   - Validarlo no toca la base ni Redis (HA01).
   - Los errores del token siguen RFC 6749 (`invalid_client`, …). El resto del API usa el formato estándar `error.code`.
3. **El contrato decide en cada solicitud, no el token.** El contrato vigente del socio (endpoints habilitados y `cuota_rps`) se lee con
   una caché de 60 s. Deshabilitar un endpoint o un socio surte efecto en un minuto, no al vencer el token. Un endpoint no habilitado
   responde 403 `ENDPOINT_NOT_AUTHORIZED`.
4. **Cuota en Redis.** Se usa una ventana fija de 1 s: `INCR socios:cuota:<client_id>:<segundo>` con vencimiento, compartida entre réplicas.
   - Al excederla responde 429 `RATE_LIMIT_EXCEEDED` con `Retry-After: 1`.
   - Si Redis no responde, la cuota **falla abierta**: deja pasar y lo registra. El WAF sigue limitando por IP.
5. **Idempotencia en Redis.** `X-Idempotency-Key` es obligatoria en las escrituras.
   - La clave es por socio y se guarda su sha256 con `SET NX` en estado `en_proceso`. Al terminar se guarda la respuesta exitosa por 24 h.
   - Repetir la clave con el mismo cuerpo devuelve la respuesta original sin recalcular (`X-Idempotent-Replayed: true`).
   - Con otro cuerpo, o mientras la original sigue en proceso, responde 409 `IDEMPOTENCY_CONFLICT`. Un fallo libera la clave.
   - Sin Redis, la idempotencia **falla cerrada** (503).
6. **Esquema `socios` sin datos de negocio.** Solo `clientes_api` (con `client_secret_hash` bcrypt) y `contratos_api` (versión, endpoints en
   JSONB y `cuota_rps`). La cuota y la idempotencia son estado técnico con vencimiento y viven en Redis, no en la base.
7. **Cliente final del socio.** Cotización recibe `cliente_id = uuid5(espacio de socios, "<client_id>:<referencia_cliente>")`. Es estable,
   no choca con los clientes de Solventa ni entre socios, y cabe en los 36 caracteres de Cotización.
8. **Versionado por ruta.** Cada versión es un paquete y un router (`app/socios/v1`). Una `v2` se monta al lado sin tocar la `v1`.
   `GET /partners/v1/status` es público y no expone URLs ni detalles internos.

## Consecuencias

- La cuota y la autenticación cuestan un viaje a Redis por solicitud y la verificación de una firma. La caché de contratos evita la base en
  la ruta de la cotización.
- `bcrypt` (~100-250 ms) solo se paga en `/oauth/token`, una vez por hora y por socio.
- Guardar la respuesta 24 h en Redis no viola el criterio 9 (sin estado de negocio): la cotización vive en Cotización y esta copia vence.

## A confirmar y límites

- **WAF frente a la cuota.** La regla del WAF de 1000 solicitudes cada 5 min por IP (~3,3 rps) es menor que una cuota típica de socio, así
  que un socio con IP fija recibiría el 429 del WAF, sin `Retry-After`, antes que el de Socios. Hay que ajustarlo en `solventa-infra`.
- **Canal en Cotización.** Cotización no distingue el canal «socio» (solo el asesor guarda origen). El reporte de consumo por socio queda
  para HU33/HU34.
- **Fuera de alcance:** la rotación de credenciales, el bloqueo por fuerza bruta en `/oauth/token` (lo mitigan el WAF por IP y el costo de
  bcrypt) y el catálogo completo de errores (HU34).
- **Latencia (criterio 10).** El presupuesto del sobrecosto está «a confirmar»; se propone ≤ 30 ms en p95 sobre Cotización, medido en
  staging con `services/socios/scripts/carga/medir_latencia.py`.
