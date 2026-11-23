# ADR-0030: Contratos de la API de socios — conformidad con Schemathesis y compatibilidad con oasdiff

- **Estado:** Aceptado (HA07, Sprint 3).
  - El experimento del 2026-11-22 dejó HA07 **parcialmente confirmada**.
  - Las decisiones de este ADR se cumplen: criterios 2, 3 y 5.
  - Lo que no se cumple no depende de ellas:
    - el orden «contrato primero» (criterio 1) ya no se puede recuperar para HU32 a HU34;
    - el objetivo de ≤ 5 persona-día (criterio 4) queda pendiente de una medición con una persona.
- **Fecha:** 2026-11-22 (el mismo día se agregan los resultados del experimento)
- **Historia:** HA07 (arquitectura, Sprint 3) — [issue #48](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/48)
- **Depende de:** [ADR-0025](0025-ms-socios-fachada-oauth-cuota.md) (fachada, OAuth y versión en la ruta) y
  [ADR-0027](0027-ms-socios-catalogo-de-errores.md) (catálogo de errores de `/partners/v1`)
- **Experimento:** `solventa-experimentos/ha07/` (kit del integrador y `RUNBOOK.md`).
- **Informe:** `ha07/resultados/INFORME.md`, con los defectos y el programa del integrador en `ha07/resultados/integrador/`.

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
  fueron en `excepciones.txt` solo para la transición de su PR (#95) y se vaciaron después del merge. No se agregan
  excepciones nuevas sin justificarlas en el PR.
- Schemathesis corre con 50 casos por operación (`SOCIOS_CONTRATO_EJEMPLOS`), con semilla fija para que el CI sea
  reproducible. Subir el número encuentra más casos raros a cambio de tiempo.
- La conformidad se prueba contra la app con dobles, no contra staging. Lo que pase entre servicios reales lo cubren la
  colección de Postman y el experimento de integración de HA07.
- Dar a un integrador solo el contrato, `errores-y-reintentos.md` y `guia-de-inicio.md` es lo que mide el experimento de HA07
  (`solventa-experimentos/ha07`).

## Resultados (2026-11-22)

**Sesión en staging:** 22 y 23 de noviembre (UTC), en la cuenta del autor (`us-east-1`), desplegada desde `main` (`101d23b`).
Identidad usó la imagen temporal `101d23b-scripts`. El kit del integrador salió de la rama de HA07 (`95b1d98`), que solo
cambia el contrato.

**Veredicto de HA07: parcialmente confirmada.**

| # | Criterio de HA07 | Veredicto | Evidencia |
|---|---|---|---|
| 1 | Contrato primero | **No cumple** (en el orden pedido) | HU32 a HU34 ya estaban mezcladas: el contrato se generó desde el código. Desde #95 queda fijado y vigilado por el CI (criterios 2 y 3) |
| 2 | Pruebas de contrato en el CI | **Cumple** | Schemathesis 4.32.7 en `pruebas (socios)`: 13 operaciones con 50 casos cada una. Encontró una discrepancia real: el 202 de `firma/codigo` documentado con cuerpo (decisión 6) |
| 3 | Un cambio que rompe hace fallar el CI; uno compatible no | **Cumple** | Job `contrato (socios)` con oasdiff 1.35.0: los 5 casos que rompen y los 4 compatibles salen bien clasificados en cada corrida |
| 4 | Esfuerzo de integración ≤ 5 persona-día | **Medido con desviación; objetivo no verificable** | Agente aislado: 42 min 18 s de reloj, con 14 min 36 s de espera de soporte. No se convierte a persona-día (ver abajo) |
| 5 | 100 % de los fallos con código del catálogo | **Cumple** | CI de #95: 883 de 883 fallos provocados. En staging, los 401, 400, 409 y 429 (con `Retry-After`) que provocó el integrador salieron como dice el catálogo |
| 6 | Preguntas del integrador anotadas como defectos | **Cumple** | 8 defectos de documentación, D-01 a D-08 |
| 7 | Informe con veredicto | **Cumple** | `ha07/resultados/INFORME.md` |

La colección de Postman de socios dio 114 de 114 aserciones antes y después de cada cambio del contrato (decisión 1).

### Esfuerzo del integrador

**Quién integró:**
- una sesión nueva de Claude Code con solo el kit: el contrato, `guia-de-inicio.md`, `errores-y-reintentos.md`, la URL de
  staging y las credenciales de `socio-ha07`;
- sin acceso a GitHub, al código ni a la web, y la transcripción lo confirma;
- un integrante del equipo respondió como soporte.

**Resultado:**
- completó dos ventas hasta la póliza `EMITIDA` y provocó los errores del catálogo;
- el programa de punta a punta pasó 15 de 15;
- tardó **42 min 18 s** (23:37:23 a 00:19:41 UTC);
- recibió 2 respuestas de soporte, de 10 min 45 s (D-05) y 3 min 51 s (D-08): en total, 14 min 36 s de espera.

**Desviación: el integrador fue un agente, no una persona externa.** Mide si la documentación alcanza, no el esfuerzo humano.
Por eso los 42 minutos no se convierten a persona-día y el objetivo de ≤ 5 persona-día no se declara cumplido.

### Defectos de documentación

| ID | Severidad | Qué faltaba | Corrección |
|---|---|---|---|
| D-01 | menor | Referencias a un ADR y a «la guía de integración §4.5», que el socio no recibe | #97 |
| D-02 | menor | `X-Idempotency-Key` con `required: false`; no dice si `firma/codigo` la lleva | #97 |
| D-03 | demora | Sin catálogo de productos ni de los campos de `datos` | #97 |
| D-04 | demora | Sin estados cerrados ni definición de «lista para firmar» | #97 |
| D-05 | **bloqueante** | La venta quedó `DERIVADA` y la guía no lo explica (causa en staging: H1) | #97 (documentación); H1 |
| D-06 | menor | `motivos` dice «perfil incompleto» con la decisión ya `APROBADA` | #97 documenta lo actual; H2 |
| D-07 | menor | No dice en qué llamada se valida cada condición del cliente | #97 |
| D-08 | demora | La segunda venta también quedó `DERIVADA` (misma causa de D-05) | #97 (nota de staging); H1 |

### Hallazgos

- **H1. Caché compartida entre Cotización y Perfilamiento**
  ([backend#84](https://github.com/miso-proyecto-final-2026/solventa-backend/issues/84)). Es la causa de D-05 y D-08.
  - Las dos usan la clave `perfil:{cliente_id}:{fuente}` en el mismo Redis.
  - El simulador de Cotización entrega 5 de los 9 campos canónicos. Como Cotización consultó primero al cliente demo,
    Perfilamiento reutilizó esa respuesta y el perfil quedó con completitud 0,5556.
  - Suscripción deriva todo perfil con completitud menor a 0,8, así que las dos ventas del socio quedaron en revisión manual
    y se aprobaron como `asesor1`.
  - No apareció con newman en local, porque ahí el perfil se enriqueció antes.
- **H2. `motivos` desactualizado tras la revisión asistida**
  ([backend#98](https://github.com/miso-proyecto-final-2026/solventa-backend/issues/98)).
  - Al aprobar o ajustar, Suscripción cambia `decision` pero conserva los motivos de la derivación.
  - Cambiarlo es un cambio de comportamiento (decisión 1), así que se decide en ese issue. Mientras tanto, #97 documenta lo
    que la API devuelve hoy.

### Decisiones derivadas

8. **Vaciar las excepciones transitorias de oasdiff después de cada corrección del contrato.**
   - #95 → [#96](https://github.com/miso-proyecto-final-2026/solventa-backend/pull/96).
   - [#97](https://github.com/miso-proyecto-final-2026/solventa-backend/pull/97) agrega 3 más: `X-Idempotency-Key` pasa a
     `required`, como ya la exigía la API. Se vacían en un PR aparte después de mezclarlo.
9. **La documentación del socio cubre los estados de una venta.** Con #97:
   - estados y pasos con sus valores, la condición de «lista para firmar» y el intervalo de consulta;
   - qué hacer con `DERIVADA` y `AJUSTADA`;
   - el catálogo de productos y el orden de validación del cliente;
   - sin referencias internas.

   Los valores se documentan en las descripciones, no como `Literal`: un enum en una respuesta haría que FastAPI convirtiera
   un valor nuevo en un 500, y eso cambiaría el comportamiento.
10. **Arreglar backend#84 antes de cualquier demo de socios en staging.** Mientras tanto, la guía avisa que en staging una
    venta puede quedar en revisión manual.
11. **Repetir la medición del criterio 4 con una persona** que no haya construido la API. Esta corrida queda como línea base
    de defectos.
