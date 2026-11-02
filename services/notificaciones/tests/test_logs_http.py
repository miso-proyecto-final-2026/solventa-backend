"""Cada request saliente deja un log con host, ruta y estado, sin la query ni las cabeceras."""

import logging

import httpx

from app.logs import ganchos_http


async def test_registra_host_ruta_estado_y_duracion_sin_query_ni_cabeceras(caplog):
    transporte = httpx.MockTransport(lambda peticion: httpx.Response(404))
    async with httpx.AsyncClient(transport=transporte, event_hooks=ganchos_http()) as cliente:
        with caplog.at_level(logging.INFO, logger="solventa.http"):
            await cliente.get("http://vecino/v1/x?token=secreto", headers={"X-Service-Key": "clave-privada"})
    (campos,) = [r.campos for r in caplog.records if getattr(r, "campos", {}).get("evento") == "request_saliente"]
    assert (campos["metodo"], campos["host"], campos["ruta"], campos["estado"]) == ("GET", "vecino", "/v1/x", 404)
    assert campos["duracion_ms"] >= 0
    assert "secreto" not in caplog.text and "clave-privada" not in caplog.text
