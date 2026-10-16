# ADR-0004 — Patrón de adaptadores para cambiar de proveedor externo

- **Estado:** Propuesto (pendiente de revisión por un integrante distinto del autor)
- **Fecha:** 2026-10-16
- **Historia:** HA06 (arquitectura, Sprint 1) — [issue #47](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/47)
- **Depende de:** [ADR-0001](0001-descomposicion-microservicios-bounded-contexts.md) (MS Identidad dueño de KYC) y [ADR-0002](0002-comunicacion-sincrona-asincrona-por-flujo.md) (KYC es síncrono).
- **Fuente:** `docs/arquitectura-8-semanas/documento-arquitectura-refinado-y-detallado.pdf` del hub, sección 4.3 «Hexagonal (puertos y adaptadores)» y requisito RQ-M2 («sustituir un proveedor sin tocar la lógica de negocio»).

## Contexto

El documento de arquitectura define que los proveedores de Open Finance, KYC y pasarela de pagos pueden cambiar durante la vida del producto, y que sustituir uno debe limitarse al adaptador (RQ-M2). Esta historia lo demuestra con una medición: implementar un **segundo adaptador con un contrato distinto** y registrar qué se toca y cuánto cuesta.

La historia propone como sujeto el adaptador de Open Finance (HU09) y deja el de KYC (HU02) como alternativa. Se eligió **KYC** porque su puerto devuelve tipos del dominio y está probado; el de Open Finance filtra el contrato del proveedor al núcleo (ver «Hallazgo»), de modo que con él la medición habría terminado en el caso del criterio 7 sin permitir demostrar el patrón.

## Decisión

Se adopta, para todo proveedor externo, este patrón:

1. **Puerto del dominio** (`VerificadorIdentidad`): recibe y devuelve solo tipos del dominio. Ningún dato con la forma de un proveedor cruza el puerto.
2. **Un adaptador por proveedor**, y **todo** el mapeo al contrato de ese proveedor vive dentro de él: autenticación, rutas, nombres de campo, vocabulario (p. ej. el reto de vida) y formato de errores.
3. **Selección por configuración** en el cableado (`KYC_PROVEEDOR`), sin tocar el núcleo. Es el único punto, junto con el adaptador, que cambia al sustituir un proveedor.
4. **Suite de contrato obligatoria**: cualquier adaptador debe pasar `tests/contrato_verificador.py` antes de ser aceptado.
5. **Degradación por el dominio, no por el proveedor**: todo lo que el proveedor no pueda resolver (5xx, caído, vacío, revisión manual) se traduce a `ProveedorKycError`, que el núcleo convierte en `EN_REVISION`. El dominio no crece con estados de cada proveedor.

### Qué se implementó

| Componente | Archivo |
|---|---|
| Puerto y tipos del dominio | `services/identidad/app/kyc/dominio.py` (sin cambios) |
| Adaptador 1 (simulado, HU02) | `services/identidad/app/kyc/simulador.py` (sin cambios) |
| **Adaptador 2 (proveedor B)** | `services/identidad/app/kyc/proveedor_b.py` (nuevo) |
| **Selección por configuración** | `services/identidad/app/kyc/wiring.py` (`construir_verificador`) |
| Suite de contrato | `services/identidad/tests/contrato_verificador.py` |

El **contrato del proveedor B** es distinto al del simulado en lo que importa a un adaptador:

| Aspecto | Simulado (adaptador 1) | Proveedor B (adaptador 2) |
|---|---|---|
| Autenticación | ninguna (en proceso) | cabecera `X-Api-Key` |
| Transporte | llamada en proceso | `POST /v2/identity-checks`, JSON con imágenes en base64 |
| Resultado | `match` y `liveness.passed` | `outcome`: `PASS` / `FAIL` / `REVIEW` |
| Reto de vida | `GIRAR_IZQUIERDA` | `TURN_LEFT` (el adaptador traduce) |
| Errores | excepción de conexión | HTTP 4xx/5xx con `{"error": {"code", "message"}}` |
| Idempotencia | clave del caso | campo `reference` |

Decisión de mapeo relevante: `REVIEW` (el proveedor B pide revisión manual) se traduce a `ProveedorKycError`, es decir, `EN_REVISION`. Así no se añade un estado al dominio. El costo es que un resultado «no concluyente» y un fallo técnico se ven igual en el núcleo; si se necesitara distinguirlos habría que ampliar el puerto, y eso sí tocaría el núcleo.

## Método de medición

- **Línea base:** commit `15ab1fe` de `main` (HU02 fusionada), antes de cualquier cambio de esta historia.
- **Dos fases separadas**, para no atribuir al reemplazo el costo de preparar la frontera:
  - **Preparación** (`4b195da`): la suite de contrato, escrita y ejecutada primero contra el adaptador original.
  - **Sustitución** (`2185ed3`): el segundo adaptador, la selección por configuración y sus pruebas.
- **Qué se cuenta:** archivos y líneas desde el commit previo (`git diff --numstat`), separando producción, configuración y pruebas, y comprobando que el núcleo y las pruebas de negocio previas no aparezcan en el diff.
- **Reproducción:**

```bash
git diff --name-status 15ab1fe..2185ed3
git diff --numstat 4b195da..2185ed3                       # solo la sustitución
git diff --name-status 15ab1fe..2185ed3 -- \
  services/identidad/app/kyc/{dominio,servicio,almacen,config,simulador}.py \
  services/identidad/app/{routers,models.py,schemas.py,main.py,auth.py} \
  services/identidad/migrations                           # núcleo: debe salir vacío
```

## Resultados (datos crudos)

### Archivos tocados desde la línea base (`15ab1fe..2185ed3`)

| Tipo | Archivo | Cambio | Líneas |
|---|---|---|---|
| Producción | `app/kyc/proveedor_b.py` | nuevo | +113 |
| Selección por configuración | `app/kyc/wiring.py` | modificado | +19 −1 |
| Dependencia | `requirements.txt` | modificado | +1 |
| Documentación | `README.md` | modificado | +3 |
| Pruebas (entregable de HA06) | `tests/contrato_verificador.py` | nuevo | +129 |
| Pruebas | `tests/test_kyc_adaptadores.py` | nuevo | +77 |
| Pruebas | `tests/proveedor_b_falso.py` | nuevo | +60 |
| Pruebas | `tests/test_kyc_proveedor_b.py` | nuevo | +204 |
| **Total** | 8 archivos (4 de producción/configuración/documentación) | | +606 −1 |

(Cifras netas desde la línea base. La suite de contrato se creó en la fase de preparación con +122 líneas y, en la sustitución, recibió +8 −1 al añadir el escenario «proveedor caído», por eso queda en +129; ver «Desviaciones».)

### Núcleo y pruebas de negocio

- **Archivos del núcleo y del adaptador original modificados: 0.** `dominio.py`, `servicio.py`, `almacen.py`, `config.py`, `simulador.py`, los routers, modelos, esquemas, `main.py`, la autenticación y las migraciones no aparecen en el diff.
- **Pruebas de negocio preexistentes modificadas: 0.** `test_kyc_api.py`, `test_kyc_contrato.py`, `test_kyc_logs.py`, `test_kyc_almacen.py`, `test_clientes.py`, `test_consentimientos.py`, `test_migraciones.py`, `conftest.py` y `helpers.py` no aparecen en el diff.
- **Pruebas antes y después** (Identidad, con PostgreSQL real): 96 pasan en la línea base; 140 pasan al final (las mismas 96 más 44 nuevas). Cotización: 162 pasan y 9 se omiten, antes y después.

### Suite de contrato

11 pruebas por adaptador, 22 en total, todas en verde: éxito, rechazo, reto inválido, timeout, error 5xx, **proveedor caído**, respuesta vacía, idempotencia por clave y que los errores no filtren el contenido de las imágenes (3 casos). Cubre también los tres escenarios que pide el criterio 6: timeout, error y respuesta vacía.

### Esfuerzo

| Fase | Inicio (UTC) | Fin (UTC) | Tiempo de reloj |
|---|---|---|---|
| Preparación (suite de contrato) | 2026-10-16T20:05:38 | 2026-10-16T20:08:33 | 2 min 55 s |
| Sustitución (adaptador, selección, pruebas) | 2026-10-16T20:08:33 | 2026-10-16T20:11:12 | 2 min 39 s |

Fuente: `date -u` registrado al iniciar y al commitear cada fase. **No es una medición de persona-día** y no permite concluir nada sobre el límite de 2 persona-día del criterio 4 (ver «Limitaciones»).

## Hallazgo: el puerto de Open Finance no aísla al proveedor

Análisis estático (no una medición) sobre `main` del servicio de Cotización:

- `ProveedorOpenFinance.consultar_perfil` devuelve `dict[str, Any]` con los campos del proveedor (`monthlyIncomeCop`, `debtRatio`, `accountsOpenYears`).
- El mapeo de esos nombres a las señales canónicas **no está en el adaptador**: está en `_MAPEO` y `unificar` de `app/cotizacion/perfil.py`, que pertenece al núcleo de cotización (el propio archivo lo marca como PROVISIONAL hasta HU10).
- El endpoint público `GET /perfiles/{cliente_id}` (`app/proveedores/api.py`) devuelve ese `payload` tal cual: **cambiar de proveedor cambiaría la forma de la respuesta de una API pública**.
- Fuera del adaptador, los nombres crudos aparecen en 2 archivos: `app/cotizacion/perfil.py` y `tests/cotizacion/test_perfil.py`.

**Consecuencia (criterio 7):** un segundo proveedor de Open Finance con otros nombres de campo obligaría a tocar, como mínimo, `perfil.py`, sus pruebas y el contrato de `/perfiles`: más de un componente y fuera del adaptador. La frontera mal definida es el **valor de retorno del puerto**, que debería ser un tipo del dominio y no el payload del proveedor.

**Propuesta de rediseño (no implementada; corresponde a HU10):**

1. Que `ProveedorOpenFinance` y `ProveedorOpenData` devuelvan un tipo canónico (p. ej. `SenalesFuente`) con las señales ya normalizadas.
2. Mover `_MAPEO` a cada adaptador; `unificar` deja de conocer nombres de proveedor.
3. Decidir si `/perfiles` expone las señales canónicas o un payload opaco, y versionar el contrato.
4. Exigir a ambos puertos la misma suite de contrato de este ADR antes de aceptar un segundo proveedor.

## Cumplimiento de los criterios de aceptación

| # | Criterio | Resultado |
|---|---|---|
| 1 | Segundo adaptador con contrato distinto | **Cumple** (autenticación, ruta, campos, vocabulario y errores distintos) |
| 2 | Cambio limitado a 1 componente más la selección | **Cumple**: 1 adaptador nuevo + `wiring.py`; además 1 dependencia y 3 líneas de README |
| 3 | Núcleo y pruebas existentes intactos | **Cumple**: 0 archivos del núcleo y 0 pruebas de negocio modificados; 96 pruebas previas siguen pasando |
| 4 | Esfuerzo ≤ 2 persona-día con método | **No verificable** con esta medición (ver «Limitaciones»); las horas humanas quedan por registrar |
| 5 | Selección por configuración | **Cumple**: `KYC_PROVEEDOR`; un valor desconocido falla con un mensaje claro |
| 6 | Misma suite de contrato, con timeout, error y vacío | **Cumple**: 11 pruebas × 2 adaptadores |
| 7 | Si toca el núcleo, documentar la frontera | **Aplica a Open Finance**: documentado arriba, con propuesta de rediseño |

## Desviaciones respecto del diseño

- **La suite creció durante la sustitución.** Al mutar el adaptador B (hacer que un error de red no se traduzca), la suite no lo detectó: los escenarios «5xx» devuelven un HTTP 503 normal, pero un proveedor **caído** es otro camino. Se añadió el escenario `CAIDO` y la mutación pasó a fallar. Esto cuenta como cambio de pruebas durante la sustitución y está reflejado en las cifras.
- **Dos mutaciones adicionales** (no traducir el reto al vocabulario de B; mapear `FAIL` como aprobado) fueron detectadas por la suite sin cambios.

## Limitaciones y supuestos

- **Sesgo a favor del resultado.** El puerto y el primer adaptador los escribió el mismo autor (con asistencia de IA) pensando en una sustitución posterior. El costo medido es el de una frontera diseñada a propósito; uno diseñado sin ese objetivo costaría más.
- **El tiempo de reloj no es esfuerzo humano.** Mide la ejecución asistida por IA y excluye lectura, revisión, aprobaciones y la integración con un proveedor real. El criterio 4 exige el tiempo real de una persona: **las horas efectivas las debe registrar quien revise y valide el cambio**.
- **El proveedor B es falso.** Su contrato es una invención razonable (no una API real) y se simula con `httpx.MockTransport`. Un proveedor real traería detalles que aquí no aparecen (límites de tamaño, firmas de webhook, reintentos propios, subida de imágenes por multipart).
- **Un solo sujeto medido.** Se midió KYC; Open Finance se analizó de forma estática.
- **El error de configuración aparece en el primer uso, no al arrancar.** La dependencia se construye de forma perezosa; un `KYC_PROVEEDOR` inválido o sin URL falla en la primera petición. Mejora posible: validar en el arranque.
- **La clave del proveedor B** debe llegar por un secreto del clúster (`secret_env_vars` de infra), nunca por git; no se incluye en esta historia porque no hay proveedor real.

## Alternativas descartadas

1. **Medir con Open Finance (el sujeto propuesto).** Descartada como sujeto principal: su puerto devuelve el payload del proveedor, así que la medición habría sido el caso del criterio 7. Se conserva como hallazgo y propuesta.
2. **Rediseñar también el puerto de Open Finance en esta historia.** Descartada: es trabajo de HU10 (perfil canónico) y excede los 3 puntos.
3. **Un registro dinámico de adaptadores (plugins).** Descartada por exceso de ingeniería: un `if` explícito en `construir_verificador` es legible, se prueba fácilmente y basta mientras haya pocos proveedores.
4. **Añadir `REVIEW` al dominio como un tercer resultado.** Descartada: obliga a tocar el núcleo para un caso que ya se resuelve con `EN_REVISION`. Se reconsidera si se necesita distinguir «no concluyente» de «fallo técnico».

## Coherencia con el documento de arquitectura

Esta decisión no contradice el documento (sección 4.3 y RQ-M2); lo concreta con una medición. No se detectó nada que obligue a corregir el documento. El hallazgo sobre Open Finance sí señala una **diferencia entre el diseño (puertos que aíslan al proveedor) y la implementación de HU09 (puerto que devuelve el payload crudo)**, a resolver en HU10.

## Consecuencias

- **Positivas:** cambiar de proveedor de KYC es una variable de entorno más un adaptador; la suite de contrato es reutilizable y vuelve verificable la regla «cualquier proveedor futuro debe pasarla».
- **Negativas:** la indirección adicional (puerto, adaptador y selección) cuesta más archivos que una llamada directa; el contrato de la suite hay que mantenerlo cuando cambie el puerto.
- **Seguimiento:** registrar las horas humanas reales; rediseñar el puerto de Open Finance en HU10; repetir la suite con el primer proveedor real; validar `KYC_PROVEEDOR` en el arranque.
