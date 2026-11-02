# Cómo probar HU19 (gestión de cobros fallidos)

Guía para recorrer HU19 en la consola web sin preparar datos a mano. `scripts/sembrar-HU19.sh` crea tres cobros de
prima rechazados por la API real de MS Pagos, en local (docker compose) o en staging (kubectl). Después se recorren
los 8 flujos de abajo. Los videos de referencia de cada flujo están en el
[issue #72](https://github.com/miso-proyecto-final-2026/proyecto-final/issues/72) (staging, 2026-11-01).

Decisiones y verificación: enmienda del [ADR-0014](adr/0014-ms-pagos-cobro-idempotente-y-conciliacion.md).

## Qué crea el script

| Caso | Suscripción | Monto | Gracia | Sirve para |
|---|---|---|---|---|
| A | `hu19-normal-<corrida>` | 48.500 | 3 días (normal) | Reintento aprobado (flujo 04) |
| B | `hu19-por-vencer-<corrida>` | 72.300 | 6 h (próximo a vencer) | Reintento rechazado (flujo 05): su token `tok_sim_rechazar_…` siempre se rechaza |
| C | `hu19-vencido-<corrida>` | 39.900 | Vencida hace 1 h | Aviso al operador (flujo 02) y pasarela caída (flujos 06 y 07) |

`<corrida>` es la fecha y hora UTC de la corrida, así que el script se puede repetir: cada corrida crea tres casos
nuevos y no toca los anteriores. Los clientes son los clientes demo de `services/identidad/scripts/sembrar_demo.py`.
La bandeja de la consola no muestra la suscripción: los casos se reconocen por el monto y por la etiqueta de gracia.

Pasos:

1. Pone el simulador de Pagos en `RECHAZAR`.
2. Crea los tres cobros con `POST /v1/pagos/cobros` (ruta interna, con la clave de servicio). En staging, por
   port-forward y con la clave leída del secreto del clúster.
3. Ajusta la gracia de B y C en la base de Pagos. `PERIODO_GRACIA_DIAS` va en días enteros, así que no hay otra forma
   de tener un caso a 6 h o ya vencido.
4. Devuelve Pagos a `APROBAR`, también si algo falla a mitad.
5. Muestra los tres casos.

El detector de gracia pasa C a `VENCIDO` y avisa al operador en su siguiente vuelta: 5 s en local y hasta 60 s en
staging (`PAGOS_DETECTOR_GRACIA_S`).

El script no imprime la clave de servicio ni las contraseñas.

## En local

Requisitos: Docker con Compose, Git Bash (Windows) o una terminal bash, y Node para la consola web.

```bash
# En solventa-backend
scripts/sembrar-HU19.sh --local
```

La primera corrida hace lo siguiente:

- Genera `services/pagos/.env` con claves locales al azar. Git ignora ese archivo y el script no lo imprime.
- Construye las imágenes y levanta `services/pagos/docker-compose.yml`: PostgreSQL, Kafka, Identidad en el puerto
  8001 y Pagos en el 8005, con migraciones.
- Crea el asesor y el operador demo.

Luego, en solventa-web:

```bash
npx ng serve      # http://localhost:4200; el proxy envía /api/identidad y /api/pagos a 8001 y 8005
```

Para entrar a la consola:

- Usuarios: `asesor@solventa.test` y `operador@solventa.test`.
- Contraseñas: `ASESOR_DEMO_PASSWORD` y `OPERADOR_DEMO_PASSWORD` de `services/pagos/.env`.

Apagar y borrar los datos:

```bash
docker compose --project-directory services/pagos down -v
```

## En staging

Requisitos:

- Staging levantado, con Pagos e Identidad desplegados.
- `kubectl` apuntando al clúster:

  ```bash
  aws eks update-kubeconfig --name solventa-staging --region us-east-1
  ```

- Permisos en el namespace `staging` para leer secretos, cambiar el deployment de Pagos, `exec`, `cp` y
  `port-forward`.

```bash
scripts/sembrar-HU19.sh --staging
```

El script muestra el contexto de kubectl y pide confirmación antes de tocar Pagos. Con `--si` no pregunta. Por
defecto el namespace es `staging` y el túnel usa el puerto 18005; se cambian con `NAMESPACE` y `PUERTO_TUNEL`.

Si el asesor y el operador demo no existen todavía, agregue `--usuarios`. Las contraseñas las elige usted y se pasan
por variables de entorno, sin dejarlas en el historial:

```bash
read -rs -p 'Contraseña del asesor: ' ASESOR_DEMO_PASSWORD; echo
read -rs -p 'Contraseña del operador: ' OPERADOR_DEMO_PASSWORD; echo
export ASESOR_DEMO_PASSWORD OPERADOR_DEMO_PASSWORD
scripts/sembrar-HU19.sh --staging --usuarios
```

La consola se abre en la URL pública de staging:

```bash
terraform -chdir=environments/staging/platform output -raw public_url   # en solventa-infra
```

`kubectl set env` deja `PASARELA_SIM_MODO=APROBAR` escrito en el deployment, que es el mismo valor de
`services.yaml`. El siguiente `apply` de `platform` lo vuelve a dejar como lo declara Terraform.

## Cambiar el modo de la pasarela

Los flujos 06 y 07 necesitan la pasarela caída. Este comando solo cambia el modo y no siembra nada:

```bash
scripts/sembrar-HU19.sh --local --pasarela NO_DISPONIBLE     # o --staging
scripts/sembrar-HU19.sh --local --pasarela APROBAR           # al terminar
```

Modos: `APROBAR`, `RECHAZAR`, `TARDAR`, `NO_DISPONIBLE` y `PERDER_RESPUESTA` (ver `services/pagos/README.md`).

## Los 8 flujos

Recórralos en este orden: el 04 y el 05 cambian el estado de A y B, y el 06 y el 07 dejan C recuperado al final.

| # | Usuario | Caso | Pasarela | Qué hacer | Qué debe pasar |
|---|---|---|---|---|---|
| 01 | Asesor | Todos | `APROBAR` | Entrar y abrir «Cobros fallidos». Probar los filtros. | La bandeja muestra primero el vencido y el próximo a vencer, resaltados. «Menos de 24 h» deja B y C. «Gracia vencida» deja C. «Recuperado» sale vacío. |
| 02 | Operador | C | `APROBAR` | Entrar como operador y abrir C. | El menú muestra «Cobros fallidos» y no «Cotización asistida». C aparece vencido, con «Operador avisado». |
| 03 | Asesor | A | `APROBAR` | Abrir el detalle de A y agregar una nota. Luego intentar una nota con un número de tarjeta. | El historial muestra el intento y el código de la pasarela. La nota se guarda con autor y fecha. La nota con tarjeta se rechaza. |
| 04 | Asesor | A | `APROBAR` | «Reintentar cobro» en A. | El reintento se aprueba. A queda recuperado, el botón se deshabilita y A sale de los abiertos. |
| 05 | Asesor | B | `APROBAR` | «Reintentar cobro» en B. | El reintento se rechaza otra vez y B sigue pendiente. |
| 06 | Operador | C | `NO_DISPONIBLE` | Cambiar el modo (`--pasarela NO_DISPONIBLE`) y reintentar C. | El reintento queda en proceso, con el mensaje de pasarela no disponible. Tras recargar, el historial muestra los intentos `NO_DISPONIBLE` que Pagos repite solo. |
| 07 | Asesor | C | `NO_DISPONIBLE` | Abrir C con el reintento todavía pendiente y recargar. | «Reintentar cobro» está deshabilitado, con el aviso de reintento en proceso, también después de recargar. Al volver a `APROBAR` (`--pasarela APROBAR`), la conciliación aprueba el reintento con la misma clave y C queda recuperado. |
| 08 | Sin sesión y operador | — | — | Abrir `/failed-payments` sin sesión. Luego, como operador, abrir `/assisted-quote`. | Sin sesión, la consola lleva al login y vuelve a la bandeja tras entrar. El operador, que no tiene permiso, vuelve al inicio. |

Para repetir los flujos 04 a 07, corra el script otra vez: crea tres casos nuevos.

## Si algo falla

- **«el entorno no quedó sano»:** ver `docker compose --project-directory services/pagos ps` y `… logs pagos`.
  Revise también que los puertos 8001 y 8005 estén libres.
- **«no se encontró deploy/solventa-pagos»:** kubectl apunta a otro clúster o staging está apagado.
- **Pagos quedó en otro modo:** `scripts/sembrar-HU19.sh --local|--staging --pasarela APROBAR`.
- **C no aparece vencido:** espere una vuelta del detector (hasta 60 s en staging) y recargue la bandeja.
