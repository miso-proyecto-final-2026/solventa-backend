"""Sobrecosto de latencia de la fachada de socios (HU32, criterio 10; HA01).

Mide N cotizaciones directas a Cotización y N a través de MS Socios, intercaladas, y reporta p50/p95 de cada camino
y la diferencia. Para aislar el costo de la fachada, conviene correrlo desde dentro del clúster (URLs internas de los
dos servicios) o en local; por CloudFront se suma la latencia de la red del cliente.

    COTIZACION_URL=http://solventa-backend SOCIOS_URL=http://solventa-socios \\
    SOCIO_CLIENT_ID=socio-demo SOCIO_CLIENT_SECRET=... \\
    python scripts/carga/medir_latencia.py --n 30

El presupuesto está «a confirmar»; se propone ≤ 30 ms de sobrecosto en p95 (ADR-0025). Imprime los números tal cual
salen: no decide si se cumple.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import uuid

import httpx

CUERPO = {"producto": "microseguro_vida", "datos": {"edad": 35, "suma_asegurada": 50_000_000}}


def percentil(valores: list[float], p: float) -> float:
    ordenados = sorted(valores)
    k = max(0, min(len(ordenados) - 1, round(p / 100 * len(ordenados) + 0.5) - 1))
    return ordenados[k]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=30)
    args = parser.parse_args()

    cotizacion = os.environ.get("COTIZACION_URL", "http://localhost:8000")
    socios = os.environ.get("SOCIOS_URL", "http://localhost:8007")
    secreto = os.environ.get("SOCIO_CLIENT_SECRET")
    if not secreto:
        print("Defina SOCIO_CLIENT_SECRET.", file=sys.stderr)
        return 2

    with httpx.Client(timeout=10) as http:
        r = http.post(
            f"{socios}/oauth/token",
            data={"grant_type": "client_credentials", "client_id": os.environ.get("SOCIO_CLIENT_ID", "socio-demo"), "client_secret": secreto},
        )
        r.raise_for_status()
        token = r.json()["access_token"]

        directas: list[float] = []
        fachada: list[float] = []
        # Solo se cuentan las respuestas 201: un 429 de la cuota es rápido y bajaría el p95 de la fachada.
        for i in range(args.n):
            inicio = time.perf_counter()
            d = http.post(f"{cotizacion}/v1/cotizaciones", json={"cliente_id": f"carga-{i}", **CUERPO})
            if d.status_code == 201:
                directas.append((time.perf_counter() - inicio) * 1000)

            for _ in range(5):
                inicio = time.perf_counter()
                s = http.post(
                    f"{socios}/partners/v1/cotizaciones",
                    json={"referencia_cliente": f"carga-{i}", **CUERPO},
                    headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": str(uuid.uuid4())},
                )
                if s.status_code != 429:
                    break
                time.sleep(int(s.headers.get("Retry-After", "1")))
            if s.status_code == 201:
                fachada.append((time.perf_counter() - inicio) * 1000)
            if d.status_code != 201 or s.status_code != 201:
                print(f"corrida {i}: directa={d.status_code} fachada={s.status_code}", file=sys.stderr)

    if not directas or not fachada:
        print("Sin respuestas 201 suficientes para comparar.", file=sys.stderr)
        return 1

    for nombre, valores in (("directa", directas), ("fachada", fachada)):
        print(f"{nombre:8} n={len(valores)} p50={statistics.median(valores):.1f} ms p95={percentil(valores, 95):.1f} ms")
    print(f"sobrecosto p95 = {percentil(fachada, 95) - percentil(directas, 95):.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
