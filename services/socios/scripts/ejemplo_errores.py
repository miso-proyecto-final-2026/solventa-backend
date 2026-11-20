"""Recorre los errores de `/partners/v1` que un socio puede provocar por HTTP y comprueba que cada uno trae su código del catálogo (HU34).

    SOCIOS_BASE_URL=https://<dominio-de-staging> \\
    SOCIO_CLIENT_ID=socio-demo SOCIO_CLIENT_SECRET=... \\
    python scripts/ejemplo_errores.py

Variables opcionales:

- SOCIO_CLIENTE_SIN_KYC: documento de un cliente registrado sin KYC verificado (para el 400 de negocio) y SOCIO_CLIENTE_TIPO (`CC`).
- SOCIO_TOKEN_SIN_CONTRATO: token de un socio sin endpoints habilitados (para el 403).

La salida es la evidencia para el issue: HTTP, `code`, `reason` y `trace_id` de cada caso. Nunca imprime el secreto ni el token. El 500, el
503 y el circuito abierto no se provocan aquí (habría que detener un servicio para todo el equipo): los cubren `tests/test_catalogo_errores.py`
y `tests/test_circuito.py`. Solo necesita httpx. Sale con código 1 si algún caso no devuelve lo esperado.
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx

COTIZAR = {"producto": "microseguro_vida", "referencia_cliente": "ejemplo-errores", "datos": {"edad": 35, "suma_asegurada": 50000000}}
ESTRUCTURA = {"code", "message", "trace_id", "details", "reason"}


def _caso(
    titulo: str, r: httpx.Response, status: int, code: str, reason: str | None = None, cabecera: str | None = None
) -> bool:
    try:
        e = r.json()["error"]
    except (ValueError, KeyError, TypeError):
        e = {}
    ok = (
        r.status_code == status
        and e.get("code") == code
        and e.get("reason") == reason
        and {"code", "message", "trace_id"} <= set(e)
        and set(e) <= ESTRUCTURA
        and "X-Correlation-Id" in r.headers
        and (cabecera is None or cabecera in r.headers)
    )
    print(f"{'OK ' if ok else 'FALLA'} {titulo}: HTTP {r.status_code} code={e.get('code')} reason={e.get('reason')} trace_id={e.get('trace_id')}")
    return ok


def main() -> int:
    base = os.environ.get("SOCIOS_BASE_URL", "http://localhost:8007")
    client_id = os.environ.get("SOCIO_CLIENT_ID", "socio-demo")
    secreto = os.environ.get("SOCIO_CLIENT_SECRET")
    if not secreto:
        print("Falta SOCIO_CLIENT_SECRET.", file=sys.stderr)
        return 2

    resultados: list[bool] = []
    with httpx.Client(base_url=base, timeout=15.0) as http:
        t = http.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secreto})
        print(f"POST /oauth/token -> {t.status_code}")
        if t.status_code != 200:
            return 1
        auth = {"Authorization": f"Bearer {t.json()['access_token']}"}

        def cotizar(cuerpo=COTIZAR, cabeceras=None, clave: str | None = None):
            return http.post(
                "/partners/v1/cotizaciones",
                json=cuerpo,
                headers={**(auth if cabeceras is None else cabeceras), "X-Idempotency-Key": clave or str(uuid.uuid4())},
            )

        resultados.append(_caso("400 cuerpo inválido", cotizar({**COTIZAR, "producto": ""}), 400, "INVALID_REQUEST"))
        sin_clave = http.post("/partners/v1/cotizaciones", json=COTIZAR, headers=auth)
        resultados.append(_caso("400 sin X-Idempotency-Key", sin_clave, 400, "INVALID_REQUEST"))
        sin_kyc = os.environ.get("SOCIO_CLIENTE_SIN_KYC")
        if sin_kyc:
            cliente = {"tipo_documento": os.environ.get("SOCIO_CLIENTE_TIPO", "CC"), "numero_documento": sin_kyc}
            resultados.append(_caso("400 regla de negocio", cotizar({**COTIZAR, "cliente": cliente}), 400, "INVALID_REQUEST", "KYC_NO_VERIFICADO"))
        resultados.append(_caso("401 sin token", cotizar(cabeceras={}), 401, "INVALID_TOKEN", cabecera="WWW-Authenticate"))
        resultados.append(_caso("401 token mal formado", cotizar(cabeceras={"Authorization": "Bearer no-es-un-jwt"}), 401, "INVALID_TOKEN"))
        sin_contrato = os.environ.get("SOCIO_TOKEN_SIN_CONTRATO")
        if sin_contrato:
            resultados.append(
                _caso("403 endpoint no habilitado", cotizar(cabeceras={"Authorization": f"Bearer {sin_contrato}"}), 403, "ENDPOINT_NOT_AUTHORIZED")
            )
        resultados.append(_caso("404 ruta inexistente", http.get("/partners/v1/no-existe"), 404, "RESOURCE_NOT_FOUND"))
        resultados.append(
            _caso("404 póliza inexistente", http.get(f"/partners/v1/polizas/{uuid.uuid4()}", headers=auth), 404, "RESOURCE_NOT_FOUND")
        )
        resultados.append(_caso("405 método no permitido", http.delete("/partners/v1/cotizaciones"), 405, "INVALID_REQUEST", cabecera="Allow"))
        clave = str(uuid.uuid4())
        cotizar(clave=clave)
        resultados.append(
            _caso("409 clave con otro cuerpo", cotizar({**COTIZAR, "referencia_cliente": "otro"}, clave=clave), 409, "IDEMPOTENCY_CONFLICT")
        )

    print(f"\n{sum(resultados)} de {len(resultados)} casos con el resultado esperado.")
    return 0 if all(resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
