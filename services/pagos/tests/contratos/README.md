# Contratos de eventos entre MS Suscripción y MS Pagos (HU18)

Ejemplos generados con el código real de cada productor. Los servicios no comparten código (ADR-0001, ADR-0009), así
que cada uno guarda una copia: el productor prueba que sigue emitiendo esta forma y el consumidor, que la sabe procesar.

| Archivo | Productor | Consumidor | Topic |
|---|---|---|---|
| `cobro_prima_solicitado.json` | MS Suscripción | MS Pagos (`solventa-pagos.cobros`) | `solventa.suscripcion.comandos` |
| `cobro_prima_confirmado.json` | MS Pagos | MS Suscripción (`solventa-suscripcion.pagos`) | `solventa.pagos.resultados` |
| `cobro_prima_rechazado.json` | MS Pagos | MS Suscripción (`solventa-suscripcion.pagos`) | `solventa.pagos.resultados` |
| `reembolso_cobro_solicitado.json` | MS Suscripción (HU21) | MS Pagos (`solventa-pagos.cobros`) | `solventa.suscripcion.comandos` |
| `cobro_prima_reembolsado.json` | MS Pagos (HU21) | MS Suscripción (`solventa-suscripcion.pagos`) | `solventa.pagos.resultados` |

Si cambia la forma de un evento, se regenera en el productor y se copia al consumidor en el mismo PR. Las copias de
`services/pagos/tests/contratos/` y `services/suscripcion/tests/contratos/` deben ser idénticas. Los tokens son de prueba
(`tok_sim_…`) y los identificadores, aleatorios.
