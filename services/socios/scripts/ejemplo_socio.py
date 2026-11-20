"""Cliente de ejemplo de un socio: pide un token y cotiza de punta a punta (HU32, definición de terminado).

    SOCIOS_BASE_URL=https://<dominio-de-staging> \\
    SOCIO_CLIENT_ID=socio-demo SOCIO_CLIENT_SECRET=... \\
    python scripts/ejemplo_socio.py

Solo necesita httpx. Imprime el estado del API, la cotización y la comprobación de idempotencia; nunca imprime el
secreto ni el token.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import httpx


def main() -> int:
    base = os.environ.get("SOCIOS_BASE_URL", "http://localhost:8000").rstrip("/")
    client_id = os.environ.get("SOCIO_CLIENT_ID", "socio-demo")
    secreto = os.environ.get("SOCIO_CLIENT_SECRET")
    if not secreto:
        print("Defina SOCIO_CLIENT_SECRET.", file=sys.stderr)
        return 2

    with httpx.Client(base_url=base, timeout=10) as http:
        estado = http.get("/partners/v1/status")
        print(f"GET /partners/v1/status -> {estado.status_code} {estado.json().get('estado')}")

        r = http.post(
            "/oauth/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secreto},
        )
        print(f"POST /oauth/token -> {r.status_code} (expires_in={r.json().get('expires_in')})")
        if r.status_code != 200:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 1
        cabeceras = {"Authorization": f"Bearer {r.json()['access_token']}", "X-Idempotency-Key": str(uuid.uuid4())}

        cuerpo = {
            "producto": os.environ.get("SOCIO_PRODUCTO", "microseguro_vida"),
            "referencia_cliente": "cliente-ejemplo-001",
            "datos": {"edad": 35, "suma_asegurada": 50_000_000},
        }
        primera = http.post("/partners/v1/cotizaciones", json=cuerpo, headers=cabeceras)
        print(f"POST /partners/v1/cotizaciones -> {primera.status_code}")
        print(json.dumps(primera.json(), ensure_ascii=False, indent=2))
        if primera.status_code != 201:
            return 1

        repetida = http.post("/partners/v1/cotizaciones", json=cuerpo, headers=cabeceras)
        misma = repetida.status_code == 201 and repetida.json()["id"] == primera.json()["id"]
        print(
            f"Repetición con la misma X-Idempotency-Key -> {repetida.status_code}, "
            f"X-Idempotent-Replayed={repetida.headers.get('X-Idempotent-Replayed')}, misma cotización={misma}"
        )
        return 0 if misma else 1


if __name__ == "__main__":
    sys.exit(main())
