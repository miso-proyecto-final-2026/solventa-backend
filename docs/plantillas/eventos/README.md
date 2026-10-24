# Plantilla del cliente de eventos (HS2)

Cliente de Kafka (Amazon MSK) para los servicios de Solventa: publica y consume eventos con el sobre estándar, de forma
idempotente y con reintentos. **Cada servicio la copia** en su propio `app/eventos/`; no es una librería compartida
(ADR-0001 y ADR-0009: los servicios no comparten paquetes). Decisión completa en el [ADR-0010](../../adr/0010-eventos-sobre-msk-plantilla-por-servicio.md).

## Qué incluye

| Módulo | Responsabilidad |
|---|---|
| `sobre.py` | `Evento`: el sobre de la documentación técnica §4.3 (`event_id`, `event_type`, `source`, `timestamp`, `correlation_id`, `trace_id`, `version`, `payload`). Los campos desconocidos se ignoran. |
| `conexion.py` | Autenticación **SASL/IAM** con las credenciales de EKS Pod Identity (staging) o `PLAINTEXT` (solo pruebas locales). |
| `publicador.py` | `PublicadorKafka` (idempotente, `acks=all`, clave de partición obligatoria) y `PublicadorMemoria` para pruebas unitarias. |
| `registro.py` | Tabla `eventos_procesados` y `RegistroEventos`: la clave de idempotencia se valida contra la base antes de actuar. |
| `consumidor.py` | `ConsumidorEventos`: idempotente, reintenta con retroceso exponencial (base 1 s, tope 30 s, 5 intentos) y envía lo irrecuperable a `<topic>.dlq`. |

## Cómo copiarla a un servicio

```bash
cp -r docs/plantillas/eventos/eventos services/<servicio>/app/eventos
cp -r docs/plantillas/eventos/tests    services/<servicio>/tests/eventos
# En las pruebas copiadas, los imports pasan a ser del servicio:
find services/<servicio>/tests/eventos -name '*.py' -exec sed -i 's/from eventos/from app.eventos/g; s/import eventos/import app.eventos/g' {} +
```

1. Agrega a `services/<servicio>/requirements.txt` lo de [`requirements.txt`](requirements.txt) (`aiokafka`, `aws-msk-iam-sasl-signer-python`).
2. Declara en `solventa-infra` (`environments/staging/services.yaml`) qué topics usa:
   `eventos: { produce: [...], consume: [...] }`. Solo recibe permisos sobre esos topics y sobre los grupos de consumo que
   empiezan por el nombre del servicio. La plataforma le inyecta `KAFKA_BOOTSTRAP_SERVERS`.
3. Crea la tabla de idempotencia en el **esquema del servicio** con Alembic:

   ```python
   from eventos.registro import tabla_eventos_procesados   # app.eventos.registro al copiarla
   def upgrade() -> None:
       op.create_table(
           "eventos_procesados",
           sa.Column("consumidor", sa.String(100), primary_key=True),
           sa.Column("event_id", sa.String(64), primary_key=True),
           sa.Column("procesado_en", sa.DateTime(timezone=True), nullable=False),
           sa.Column("reentregas", sa.Integer(), nullable=False, server_default="0"),
           schema=SCHEMA,
       )
   def downgrade() -> None:
       op.drop_table("eventos_procesados", schema=SCHEMA)
   ```

## Uso

**Publicar** (desde un caso de uso, con el cliente creado una vez en el arranque):

```python
evento = Evento(event_type="cobro_prima_solicitado", source="ms-suscripcion",
                correlation_id=saga_id, payload={"suscripcion_id": sid, "monto": 45000, "moneda": "COP"})
await publicador.publicar("solventa.suscripcion.comandos", evento, clave=sid)   # la clave es la de partición (§4.2)
```

**Consumir**: el manejador recibe el evento y **la sesión de la transacción**; todo lo que escriba con ella se confirma o se
deshace junto con la reserva de idempotencia.

```python
async def manejar(evento: Evento, sesion: Session) -> None:
    ...  # escribir con `sesion`; no hacer commit

consumidor = ConsumidorEventos(
    brokers=brokers_desde_entorno(), seguridad=parametros_seguridad(), topic="solventa.pagos.resultados",
    grupo="solventa-suscripcion.pagos", manejador=manejar, fabrica_sesiones=SessionLocal,
    registro=RegistroEventos(tabla), publicador=publicador)
await consumidor.iniciar()
asyncio.create_task(consumidor.ejecutar())      # y consumidor.detener() al apagar
```

## Reglas

- **Grupo de consumo = nombre del servicio + sufijo** (`solventa-suscripcion.pagos`): es lo único que la política de IAM le permite.
- **Entrega al menos una vez** (ADR-0002). Un efecto fuera de la base de datos (llamar a una API, enviar un correo) no se deshace
  si el manejador falla después: hay que hacerlo idempotente por su cuenta.
- Un mensaje que no es un sobre válido va **directo** a `<topic>.dlq`, sin reintentos. Si la cola de fallidos también falla,
  el offset no se confirma y el mensaje se vuelve a entregar.
- Las pruebas del servicio usan `PublicadorMemoria`; las de esta plantilla usan un Kafka real en Docker.

## Limitaciones conocidas

- **Publicar después de confirmar la base de datos puede perder el evento** si el proceso cae entre ambos pasos (el problema de la
  doble escritura). Un patrón *outbox* lo resuelve; no se incluye todavía. HU15 decide si lo necesita para la saga.
- La sesión de SQLAlchemy es síncrona y se usa dentro del bucle asíncrono: es aceptable al volumen actual, no a escala.
- La autenticación IAM se verifica con la prueba de humo contra MSK (`scripts/prueba-humo-kafka.sh` en `solventa-infra`);
  localmente solo se prueba con `PLAINTEXT`, con un firmador simulado y construyendo los clientes con los parámetros IAM (así se
  detectó que `aiokafka` exige `ssl_context` con `SASL_SSL`, un defecto que solo había aparecido contra MSK real).

## Pruebas

```bash
cd docs/plantillas/eventos
pip install -r requirements.txt -r ../../../requirements-dev.txt
pytest            # arranca un Kafka real (apache/kafka) con testcontainers: requiere Docker
```
