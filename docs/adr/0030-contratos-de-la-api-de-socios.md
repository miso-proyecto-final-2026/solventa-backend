# ADR-0030: Contratos de la API de socios — conformidad con Schemathesis y compatibilidad con oasdiff

- **Estado:** Aceptado (HA07, Sprint 3)
- **Depende de:** [ADR-0025](0025-ms-socios-fachada-oauth-cuota.md) (fachada, OAuth y versión en la ruta) y
  [ADR-0027](0027-ms-socios-catalogo-de-errores.md) (catálogo de errores de `/partners/v1`)

## Contexto

HA07 pide fijar el contrato de `/partners/v1`, verificar en el CI que MS Socios lo cumple, que un cambio incompatible haga
fallar el CI y uno compatible no, y que el 100 % de los fallos devuelva un código del catálogo (EC-I1.1 y EC-I1.2).

Cuando empezó HA07, HU32, HU33 y HU34 ya estaban mezcladas, así que el contrato no se escribió antes del código: se genera
desde FastAPI (`docs/api/openapi.json`). Lo que había:

- `test_contrato.py` comprueba que el archivo publicado coincide con lo que expone la app y que los errores usan `ErrorOut`.
  Detecta que el archivo quedó desactualizado, pero no que las respuestas reales cumplan el esquema, los códigos y los
  encabezados.
- Un cambio que rompe a los socios (quitar un campo, volver obligatorio un parámetro) pasaba el CI si se regeneraba el archivo.
- El contrato no declaraba la autenticación: el `Bearer` solo aparecía en el README.
- La API de socios ya funciona y tiene su colección de Postman: HA07 no debe cambiar su comportamiento.

## Decisión

1. **El contrato describe lo que existe.** Las diferencias se corrigen en el contrato, no en la API. Cambiar una respuesta
   exige decidirlo aparte. La colección de Postman de socios se corre antes y después de cada cambio en MS Socios.
2. **Conformidad con Schemathesis** (`tests/test_conformidad_contrato.py`, versión fija en `requirements-dev.txt`):
   - genera solicitudes válidas e inválidas a partir del `openapi.json` publicado y valida cada respuesta: código documentado,
     `Content-Type`, encabezados, esquema del cuerpo, que los datos inválidos se rechacen y que falte un encabezado
     obligatorio (`Authorization`, `X-Idempotency-Key`) responda 4xx;
   - corre dentro de pytest contra la app, con los dobles de Cotización, Identidad y Suscripción: no necesita red ni
     contenedores y entra en el job `pruebas (socios)` que ya existe;
   - un segundo caso apunta a una venta existente del socio, para validar también los esquemas de éxito de cada ruta;
   - queda fuera `positive_data_acceptance`: un documento o una cotización inventados reciben, con razón, un 400 o un 404.
3. **Catálogo al 100 %.** Una verificación propia exige, en toda respuesta de error de `/partners/v1`, un `code` del
   `CATALOGO` con su código HTTP, un `reason` de `MOTIVOS` si viene, un `trace_id` UUID y `X-Correlation-Id`. El 405 con
   `INVALID_REQUEST` es la única excepción documentada (ADR-0027 §11). `/oauth/token` (RFC 6749) y `/status` no usan el
   catálogo. El conteo queda en `reports/conformidad-errores.json`. Los fallos que no se provocan por HTTP (500, 503, circuito
   abierto, token vencido) ya tienen sus pruebas en `test_catalogo_errores.py` y `test_circuito.py`.
4. **Compatibilidad con oasdiff** (`oasdiff breaking --fail-on ERR`, imagen `tufin/oasdiff` con versión fija), en el job
   `contrato (socios)`:
   - en un PR compara el contrato con el de la rama base; en `main`, con el del commit anterior;
   - `scripts/contrato/casos/` tiene cinco cambios que rompen y cuatro compatibles, como parches JSON sobre el contrato actual;
     el job exige que oasdiff los clasifique bien, así que el criterio queda demostrado en cada corrida y no solo una vez;
   - `scripts/contrato/excepciones.txt` (`--err-ignore`) admite un cambio que oasdiff marca pero que no cambia lo que recibe el
     socio. Cada línea es un cambio concreto con su justificación. Sirve para corregir la documentación, no para romper la
     API: un cambio de comportamiento incompatible se publica como `v2` (ADR-0025 §8).
5. **El contrato declara `bearerAuth`** (`http`, `bearer`, `JWT`) en toda ruta de `/partners/v1` menos `/status`. La validación
   del token sigue igual; solo se documenta.
6. **El 202 de `firma/codigo` se publica sin cuerpo,** como responde. Antes el contrato decía `application/json` con un esquema
   vacío.
7. **Pact no.** Es una herramienta guiada por el consumidor: cada consumidor publica sus expectativas y el proveedor las
   verifica. No hay socios reales que publiquen contratos, y los consumidores de prueba serían nuestros: la verificación
   dependería de lo que escribimos nosotros mismos. Si llega un socio real que mantenga sus pruebas, se puede agregar.

## Consecuencias

- El CI falla si una respuesta deja de cumplir el contrato, si un error sale sin un código del catálogo o si el contrato
  cambia de forma incompatible.
- Corregir el contrato puede disparar oasdiff aunque la API no cambie. Las dos correcciones de HA07 (`bearerAuth` y el 202)
  van en `excepciones.txt` solo para la transición de su PR, y se vacían después del merge. No se agregan excepciones nuevas sin
  justificarlas en el PR.
- Schemathesis corre con 50 casos por operación (`SOCIOS_CONTRATO_EJEMPLOS`), con semilla fija para que el CI sea
  reproducible. Subir el número encuentra más casos raros a cambio de tiempo.
- La conformidad se prueba contra la app con dobles, no contra staging. Lo que pase entre servicios reales lo cubren la
  colección de Postman y el experimento de integración de HA07.
- Dar a un integrador solo el contrato, `errores-y-reintentos.md` y `guia-de-inicio.md` es lo que mide el experimento de HA07
  (`solventa-experimentos/ha07`).
