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
