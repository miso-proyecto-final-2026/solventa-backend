"""Cliente de ejemplo de un socio: pide un token, cotiza y, si se le indica un cliente, hace la venta embebida completa.

    SOCIOS_BASE_URL=https://<dominio-de-staging> \\
    SOCIO_CLIENT_ID=socio-demo SOCIO_CLIENT_SECRET=... \\
    python scripts/ejemplo_socio.py

Con `SOCIO_CLIENTE_DOCUMENTO` (un cliente ya registrado en Solventa, con KYC verificado y consentimiento vigente) sigue con
HU33: suscripción, documento, código, firma y consulta de la póliza emitida. Variables de la venta:

- SOCIO_CLIENTE_DOCUMENTO y SOCIO_CLIENTE_TIPO (`CC` por defecto).
- SOCIO_OTP: el código de firma; en el prototipo es el OTP simulado (`FIRMA_SIM_OTP`, 123456 por defecto).
- SOCIO_METODO_PAGO: el token de la pasarela; por defecto uno simulado.
- SOCIO_ESPERA_S: cuánto esperar a que el cobro se confirme (60 por defecto).

Solo necesita httpx. Imprime el estado del API, la cotización, la venta y la póliza; nunca imprime el secreto, el token, el
documento del cliente ni el código de firma.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import httpx


def _mostrar(titulo: str, r: httpx.Response) -> None:
    print(f"{titulo} -> {r.status_code}")


def _esperar_estado(http: httpx.Client, cabeceras: dict[str, str], suscripcion_id: str, estados: set[str], espera_s: float) -> dict:
    """Consulta la póliza hasta que la venta llegue a uno de los estados, o se agote la espera."""
    limite = time.monotonic() + espera_s
    while True:
        r = http.get(f"/partners/v1/polizas/{suscripcion_id}", headers=cabeceras)
        if r.status_code == 429:  # la cuota del contrato: se espera lo que pide el API y se reintenta
            time.sleep(float(r.headers.get("Retry-After", "1")))
            continue
        cuerpo = r.json()
        if r.status_code != 200 or cuerpo["estado"] in estados or time.monotonic() > limite:
            _mostrar(f"GET /partners/v1/polizas/{{id}} (estado={cuerpo.get('estado')})", r)
            return cuerpo
        time.sleep(1.0)


def vender(http: httpx.Client, token: str, cotizacion_id: str, cliente: dict[str, str]) -> int:
    """La venta embebida de HU33. Devuelve el código de salida del script."""
    cabeceras = {"Authorization": f"Bearer {token}"}
    espera_s = float(os.environ.get("SOCIO_ESPERA_S", "60"))
    otp = os.environ.get("SOCIO_OTP", "123456")
    venta = {
        "cotizacion_id": cotizacion_id,
        "cliente": cliente,
        "metodo_pago": os.environ.get("SOCIO_METODO_PAGO", f"tok_sim_{uuid.uuid4()}"),
        "acepta_terminos": True,
        "periodicidad_pago": "ANUAL",
    }
    clave = {"X-Idempotency-Key": str(uuid.uuid4())}

    # 1. Iniciar la saga y comprobar la idempotencia (misma clave: misma venta; otro contenido: 409).
    primera = http.post("/partners/v1/suscripciones", json=venta, headers={**cabeceras, **clave})
    _mostrar("POST /partners/v1/suscripciones", primera)
    if primera.status_code != 202:
        print(json.dumps(primera.json(), ensure_ascii=False, indent=2))
        return 1
    suscripcion_id = primera.json()["id"]
    repetida = http.post("/partners/v1/suscripciones", json=venta, headers={**cabeceras, **clave})
    misma = repetida.status_code == 202 and repetida.json()["id"] == suscripcion_id
    print(f"Repetición con la misma clave -> {repetida.status_code}, misma venta={misma}")
    otro = http.post("/partners/v1/suscripciones", json={**venta, "periodicidad_pago": "MENSUAL"}, headers={**cabeceras, **clave})
    print(f"Misma clave con otro contenido -> {otro.status_code} (se espera 409)")
    if not misma or otro.status_code != 409:
        return 1

    # 2. La decisión y el cobro son asíncronos: se consulta hasta que se pueda firmar.
    estado = _esperar_estado(http, cabeceras, suscripcion_id, {"COBRO_REALIZADO", "RECHAZADA", "COMPENSADA"}, espera_s)
    if estado.get("estado") != "COBRO_REALIZADO":
        print(f"La venta no llegó a firma: estado={estado.get('estado')}", file=sys.stderr)
        return 1

    # El PDF de la póliza se genera después del cobro: el socio espera a que la póliza lo informe como GENERADO.
    limite = time.monotonic() + espera_s
    while (estado.get("poliza") or {}).get("documento_estado") != "GENERADO" and time.monotonic() < limite:
        time.sleep(1.0)
        estado = http.get(f"/partners/v1/polizas/{suscripcion_id}", headers=cabeceras).json()
    print(f"Documento: {(estado.get('poliza') or {}).get('documento_estado')}")

    # 3. El cliente lee el documento y firma con su código sin salir de la app del socio.
    ruta = f"/partners/v1/suscripciones/{suscripcion_id}"
    documento = http.get(f"{ruta}/documento-firma", headers=cabeceras)
    pdf = http.get(f"{ruta}/documento-firma/pdf", headers=cabeceras)
    _mostrar("GET .../documento-firma", documento)
    _mostrar(f"GET .../documento-firma/pdf ({len(pdf.content)} bytes)", pdf)
    codigo = http.post(f"{ruta}/firma/codigo", headers=cabeceras)
    _mostrar("POST .../firma/codigo", codigo)
    firma = http.post(
        f"{ruta}/firma",
        json={
            "documento_sha256": documento.json()["documento_sha256"],
            "documento_revisado": True,
            "codigo": otp,
            "canal": "MOVIL",
            "dispositivo": "App del socio de ejemplo",
        },
        headers={**cabeceras, "X-Idempotency-Key": str(uuid.uuid4())},
    )
    _mostrar("POST .../firma", firma)
    if firma.status_code != 200:
        print(json.dumps(firma.json(), ensure_ascii=False, indent=2))
        return 1

    # 4. El socio consulta hasta ver la póliza emitida.
    final = _esperar_estado(http, cabeceras, suscripcion_id, {"EMITIDA"}, espera_s)
    poliza = final.get("poliza") or {}
    print(f"Póliza: número={poliza.get('numero_poliza')} estado={poliza.get('estado')} vence={poliza.get('fecha_vencimiento')}")
    return 0 if final.get("estado") == "EMITIDA" and poliza.get("estado") == "VIGENTE" else 1


def main() -> int:
    base = os.environ.get("SOCIOS_BASE_URL", "http://localhost:8000").rstrip("/")
    client_id = os.environ.get("SOCIO_CLIENT_ID", "socio-demo")
    secreto = os.environ.get("SOCIO_CLIENT_SECRET")
    if not secreto:
        print("Defina SOCIO_CLIENT_SECRET.", file=sys.stderr)
        return 2
    documento = os.environ.get("SOCIO_CLIENTE_DOCUMENTO")
    cliente = {"tipo_documento": os.environ.get("SOCIO_CLIENTE_TIPO", "CC"), "numero_documento": documento} if documento else None

    with httpx.Client(base_url=base, timeout=20) as http:
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
        token = r.json()["access_token"]
        cabeceras = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": str(uuid.uuid4())}

        cuerpo = {
            "producto": os.environ.get("SOCIO_PRODUCTO", "microseguro_vida"),
            "referencia_cliente": "cliente-ejemplo-001",
            "datos": {"edad": 35, "suma_asegurada": 50_000_000},
            **({"cliente": cliente} if cliente else {}),
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
        if not misma:
            return 1
        if cliente is None:
            print("Sin SOCIO_CLIENTE_DOCUMENTO la cotización es informativa: no se puede suscribir.")
            return 0
        return vender(http, token, primera.json()["id"], cliente)


if __name__ == "__main__":
    sys.exit(main())
