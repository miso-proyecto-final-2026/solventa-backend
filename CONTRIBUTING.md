# Guía de contribución — Solventa (Grupo 10)

Aplica a los cuatro repositorios: `solventa-backend`, `solventa-web`, `solventa-mobile`, `solventa-infra`.

## 1. Flujo de trabajo: GitHub Flow (sin `develop`)

- `main` siempre está estable y desplegable. **Nadie hace push directo a `main`.**
- Todo cambio nace en una rama corta desde `main` y vuelve a `main` por Pull Request.
- Una rama = una historia (HU) o una historia de arquitectura (HA). Ramas de vida corta (idealmente < 3 días).

### Nombre de ramas

```
feature/HU-12-cotizacion-seguro
feature/HA01-cache-cotizacion
fix/HU-12-error-validacion-placa
docs/HU-05-guia-usuario
infra/HA04-plan-dr
chore/actualizar-dependencias
```

Formato: `<tipo>/<ID>-<descripcion-corta-en-kebab-case>`. El ID es el de la historia en el tablero.

### Ciclo de una historia

1. Mover el issue a **In progress** y asignarse.
2. `git switch main && git pull && git switch -c feature/HU-12-cotizacion-seguro`
3. Commits pequeños con Conventional Commits (ver sección 2).
4. Abrir PR hacia `main` (se puede abrir como *Draft* desde el primer commit).
5. El CI debe pasar en verde y debe haber **mínimo 1 aprobación** de otro integrante.
6. **Squash and merge** (el título del PR queda como commit en `main`, así que también sigue Conventional Commits).
7. La rama se borra automáticamente. El issue se cierra solo gracias a `Closes #n`.

## 2. Convención de commits (Conventional Commits)

```
<tipo>(<alcance>): <descripción en imperativo, minúscula, sin punto final>
```

| Tipo | Uso |
|---|---|
| `feat` | Funcionalidad nueva |
| `fix` | Corrección de bug |
| `docs` | Solo documentación |
| `test` | Agregar o corregir pruebas |
| `refactor` | Cambio de código sin cambiar comportamiento |
| `ci` | Pipelines y automatización |
| `build` | Dependencias, Dockerfile, Gradle, etc. |
| `chore` | Mantenimiento |

El **alcance** es el ID de la historia: `HU-12`, `HA01`.

Ejemplos:

```
feat(HU-12): agregar endpoint de cotización de seguro
fix(HU-12): validar formato de placa antes de cotizar
test(HA01): agregar prueba de cache-aside para cotizaciones
ci(HA09): ejecutar pytest con cobertura en cada pull request
```

`commitlint` revisa los commits de cada PR; si no cumplen, el CI falla.

## 3. Pull Requests

- Título con formato Conventional Commit (será el mensaje del squash).
- Usar la plantilla: qué cambia, historia relacionada (`Closes #n`), cómo se probó, checklist.
- PRs pequeños (idealmente < 400 líneas cambiadas).
- Quien revisa comenta o aprueba en menos de 24 h hábiles. Quien abre el PR no se auto-aprueba.
- Las conversaciones de la revisión deben quedar resueltas antes del merge.

## 4. Versionamiento y releases

- SemVer: `vMAYOR.MENOR.PARCHE`. Mientras el proyecto sea académico se usa `v0.X.0`.
- **Un tag por sprint**, creado sobre `main` al cerrar el sprint: `v0.1.0` (Sprint 1), `v0.2.0` (Sprint 2), etc.
- Correcciones urgentes posteriores al tag incrementan el parche: `v0.1.1`.
- Cada tag se publica como **GitHub Release** con notas generadas automáticamente.
- Los cuatro repos usan el mismo número de versión por sprint, para saber qué versiones van juntas.

```bash
git switch main && git pull
git tag -a v0.1.0 -m "Sprint 1"
git push origin v0.1.0
```

El workflow `release.yml` crea la Release al subir el tag.

## 5. Pruebas y calidad mínima

| Repo | Pruebas | Comando local |
|---|---|---|
| backend | pytest (+ FastAPI TestClient) | `pytest --cov=app` |
| web | Jasmine + Karma | `ng test --watch=false --browsers=ChromeHeadless` |
| mobile | JUnit + MockK | `./gradlew test` |
| infra | terraform fmt / validate / plan | `terraform fmt -check && terraform validate` |

Todo `feat` o `fix` debe incluir o actualizar pruebas. El CI es requisito para el merge.

## 6. Definición de terminado (DoD)

- [ ] Criterios de aceptación de la historia cumplidos
- [ ] Pruebas escritas y en verde en CI
- [ ] PR aprobado por al menos 1 compañero
- [ ] Mergeado a `main` y issue cerrado
- [ ] Story Points y estado actualizados en el tablero
