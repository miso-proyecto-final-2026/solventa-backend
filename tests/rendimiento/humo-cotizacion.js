// Humo k6 de HU12: presupuesto de latencia de la cotización.
//  - interno  (latencia_interna_ms, que devuelve el servicio): p95 <= 150 ms
//  - cliente  (medida por k6 desde el generador de carga):     p95 <= 250 ms (RQ-L1)
// Cada cotización reporta su latencia medida por k6 al servicio (criterio 9).
// Ejecutar con el servicio levantado (docker compose up -d && alembic upgrade head && uvicorn ...):
//   k6 run -e BASE_URL=http://localhost:8000 tests/rendimiento/humo-cotizacion.js
import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const latenciaInterna = new Trend('latencia_interna_ms', true);

export const options = {
  vus: 20,
  duration: '30s',
  thresholds: {
    http_req_failed: ['rate==0'],
    latencia_interna_ms: ['p(95)<=150'],
    'http_req_duration{name:cotizar}': ['p(95)<=250'],
  },
};

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const CABECERAS = { headers: { 'Content-Type': 'application/json' } };

const SOLICITUDES = [
  { producto: 'microseguro_vida', datos: { edad: 35, suma_asegurada: 50000000 } },
  {
    producto: 'proteccion_dispositivos',
    datos: { tipo_dispositivo: 'celular', valor_dispositivo: 2500000, antiguedad_meses: 8 },
  },
];

export default function () {
  // Un cliente por VU: tras la primera iteración el perfil sale de la caché (escenario típico).
  const cuerpo = { cliente_id: `k6-cliente-${__VU}`, ...SOLICITUDES[__ITER % SOLICITUDES.length] };
  const r = http.post(`${BASE}/v1/cotizaciones`, JSON.stringify(cuerpo), {
    ...CABECERAS,
    tags: { name: 'cotizar' },
  });
  const ok = check(r, { 'status 201': (x) => x.status === 201 });
  if (!ok) return;

  const c = r.json();
  latenciaInterna.add(c.metadata.latencia_interna_ms);
  http.put(
    `${BASE}/v1/cotizaciones/${c.id}/latencia-cliente`,
    JSON.stringify({ latencia_cliente_ms: r.timings.duration }),
    { ...CABECERAS, tags: { name: 'latencia_cliente' } },
  );
}
