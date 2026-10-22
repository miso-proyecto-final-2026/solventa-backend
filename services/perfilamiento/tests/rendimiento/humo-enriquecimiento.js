// Humo k6 de HU23: ~20.000 perfilamientos/hora sostenidos (EC-E1.2, HA02) ≈ 5,6 por segundo.
// Ejecutar con el servicio levantado (PERFILAMIENTO_API_KEYS=k6=<clave>) y, para medir el efecto sobre la
// cotización en línea, en paralelo con tests/rendimiento/humo-cotizacion.js del servicio de cotización:
//   k6 run -e BASE_URL=http://localhost:8002 -e SERVICE_KEY=<clave> tests/rendimiento/humo-enriquecimiento.js
// NO sustituye la medición formal en staging (informe de la semana 7): es un humo local.
import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    sostenido: { executor: 'constant-arrival-rate', rate: 6, timeUnit: '1s', duration: '60s', preAllocatedVUs: 20 },
  },
  thresholds: { http_req_failed: ['rate==0'], http_req_duration: ['p(95)<900'] },
};

export default function () {
  const r = http.post(
    `${__ENV.BASE_URL || 'http://localhost:8002'}/v1/perfilamiento/cliente-${__VU}-${__ITER}/enriquecer`,
    null,
    { headers: { 'X-Service-Key': __ENV.SERVICE_KEY } },
  );
  check(r, { 'status 201': (x) => x.status === 201 });
}
