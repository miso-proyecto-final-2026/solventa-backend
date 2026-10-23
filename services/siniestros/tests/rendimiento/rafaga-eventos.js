// Ráfaga k6 de HU27: proveedor simulado con tasa, duración y proporción de duplicados configurables.
// Meta del enunciado (EC-E2.1): ≥ 1.000.000 de eventos en 10 min ≈ 1.667/s. Aquí se valida un porcentaje
// en el ambiente disponible; el informe de staging debe decir qué porcentaje fue.
//   k6 run -e BASE_URL=http://localhost:8003 -e SERVICE_KEY=<clave> -e FUENTE=proveedor-sismos \
//          -e RATE=500 -e DURATION=60s -e DUP_RATIO=0.1 tests/rendimiento/rafaga-eventos.js
// Al terminar, compara «enviados distintos» con el resumen del servicio:
//   GET /v1/siniestros/eventos/resumen  →  recibidos = publicados = enviados distintos y, con Kafka, procesados = recibidos.
// NO sustituye la medición formal en staging: es un humo reproducible.
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const RATE = Number(__ENV.RATE || 200);
const DUP_RATIO = Number(__ENV.DUP_RATIO || 0.1);
const PREFIJO = __ENV.PREFIJO || `k6-${Date.now()}`;
const FUENTE = __ENV.FUENTE || 'proveedor-sismos';
const TIPOS = ['sismo', 'inundacion', 'sequia', 'vuelo_retrasado', 'vuelo_cancelado', 'tormenta'];

const nuevos = new Counter('eventos_distintos_aceptados');
const duplicados = new Counter('eventos_duplicados');
const saturados = new Counter('respuestas_503');

export const options = {
  scenarios: {
    rafaga: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: __ENV.DURATION || '30s',
      preAllocatedVUs: 100,
      maxVUs: 1000,
    },
  },
  // 503 es contrapresión legítima (el evento queda registrado y se reenvía); no debe haber otros fallos.
  thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<500'] },
};

export default function () {
  const n = __ITER + __VU * 1_000_000;
  // Con probabilidad DUP_RATIO reenvía un evento anterior de este mismo VU (mismo evento_id).
  const reenvio = __ITER > 0 && Math.random() < DUP_RATIO;
  const iter = reenvio ? Math.floor(Math.random() * __ITER) : __ITER;
  const cuerpo = {
    evento_id: `${PREFIJO}-${__VU}-${iter}`,
    tipo: TIPOS[n % TIPOS.length],
    fuente: FUENTE,
    ocurrido_en: new Date().toISOString(),
    datos: { magnitud: 5 + (n % 40) / 10, latitud: 4.71, longitud: -74.07 },
  };
  const r = http.post(`${__ENV.BASE_URL || 'http://localhost:8003'}/v1/siniestros/eventos`, JSON.stringify(cuerpo), {
    headers: { 'Content-Type': 'application/json', 'X-Service-Key': __ENV.SERVICE_KEY },
  });
  if (r.status === 202) nuevos.add(1);
  else if (r.status === 200) duplicados.add(1);
  else if (r.status === 503) saturados.add(1);
  check(r, { 'acuse 200/202/503': (x) => [200, 202, 503].includes(x.status) });
}
