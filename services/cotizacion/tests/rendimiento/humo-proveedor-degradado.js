// Humo k6 de HU09: 0 % de fallos con proveedor degradado.
// Ejecutar con el servicio levantado y OPEN_FINANCE_SIM_MODO=error_5xx:
//   k6 run -e BASE_URL=http://localhost:8000 -e SERVICE_KEY=<clave de PERFILES_API_KEYS> \
//     tests/rendimiento/humo-proveedor-degradado.js
import http from 'k6/http';
import { check } from 'k6';

export const options = {
  vus: 20,
  duration: '30s',
  thresholds: { http_req_failed: ['rate==0'], http_req_duration: ['p(95)<900'] },
};

export default function () {
  const r = http.get(`${__ENV.BASE_URL || 'http://localhost:8000'}/perfiles/cliente-${__VU}-${__ITER}`, {
    headers: { 'X-Service-Key': __ENV.SERVICE_KEY },
  });
  check(r, { 'status 200': (x) => x.status === 200 });
}
