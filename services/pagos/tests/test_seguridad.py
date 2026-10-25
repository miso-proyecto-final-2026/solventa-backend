"""Criterio 5 (EC-S2.1): cero datos de tarjeta en claro en almacenamiento y en logs.

Recorre los flujos del servicio (aprobado, rechazado, respuesta perdida, API, intentos de enviar un PAN), captura todos
los logs y vuelca todas las tablas. Luego busca: números con forma de PAN que pasen Luhn (0 hallazgos) y el token de la
pasarela (0 en logs; en la base, solo en `tokens_tarjeta.token`, que es donde la historia lo permite).
"""

import json
import logging
import re

import pytest
from app.logs import (
    ENMASCARADO_PAN,
    ENMASCARADO_TOKEN,
    FiltroDatosTarjeta,
    FormateadorJson,
    redactar,
)
from app.pagos.api import router
from app.pagos.errores import SolicitudInvalida, registrar_errores
from app.pagos.modelos_db import BasePagos
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.contexto import correlacion_id, traza_id
from tests.dobles import PasarelaFalsa, comando, evento

# Números de prueba públicos de las marcas (no son tarjetas reales).
PANES = ["4111111111111111", "5555555555554444", "378282246310005"]
TOKENS = ["tok_sim_seguridad_a", "tok_sim_seguridad_b", "tok_sim_seguridad_c", "tok_sim_seguridad_d"]
_CANDIDATO = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
CLAVE = "clave-saga"


def _luhn(digitos: str) -> bool:
    total = 0
    for i, d in enumerate(int(c) for c in reversed(digitos)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def hallazgos_de_pan(texto: str) -> list[str]:
    encontrados = []
    for m in _CANDIDATO.finditer(texto):
        digitos = re.sub(r"\D", "", m.group())
        if 13 <= len(digitos) <= 19 and _luhn(digitos):
            encontrados.append(digitos)
    return encontrados


def _volcar_base(fabrica) -> dict[str, str]:
    """Cada columna de cada tabla, como texto: {"tabla.columna": "valores"}."""
    volcado: dict[str, str] = {}
    with fabrica() as s:
        for tabla in BasePagos.metadata.sorted_tables:
            for fila in s.execute(select(tabla)).mappings():
                for columna, valor in fila.items():
                    clave = f"{tabla.name}.{columna}"
                    volcado[clave] = volcado.get(clave, "") + " " + json.dumps(valor, default=str)
    return volcado


async def _recorrer_flujos(armar, fabrica, reloj):
    # Kafka: aprobado, rechazado y respuesta perdida con conciliación.
    for i, (guion, token) in enumerate(zip(("aprobar", "rechazar", "perder"), TOKENS, strict=False)):
        servicio = armar(PasarelaFalsa(guion))
        with fabrica() as s, s.begin():
            await servicio.manejar_evento(evento(comando(f"susc-{i}", metodo_pago=token)), s)
        await servicio.procesar_pendientes()
        reloj.avanzar(10)
        await servicio.procesar_pendientes()
    # Kafka: alguien envía un PAN como método de pago; se rechaza y no se guarda.
    for pan in PANES:
        with pytest.raises(SolicitudInvalida), fabrica() as s, s.begin():
            await armar().manejar_evento(evento(comando("susc-pan", metodo_pago=pan)), s)
    # API: cobro, consulta, tokenización y PAN rechazados.
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.state.servicio_cobros = armar()
    app.state.api_keys = {CLAVE: "suscripcion"}
    cabeceras = {"X-Service-Key": CLAVE}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/v1/pagos/cobros",
            json={"suscripcion_id": "susc-api", "cliente_id": "cli-1", "metodo_pago": TOKENS[3], "monto": "1000.00"},
            headers=cabeceras,
        )
        await c.get(f"/v1/pagos/transacciones/{r.json()['id']}", headers=cabeceras)
        await c.post("/v1/pagos/tokenizar", json={"token": TOKENS[0]}, headers=cabeceras)
        for pan in PANES:
            await c.post("/v1/pagos/tokenizar", json={"token": pan}, headers=cabeceras)
            await c.post("/v1/pagos/tokenizar", json={"token": TOKENS[0], "numero": pan}, headers=cabeceras)


async def test_cero_datos_de_tarjeta_en_logs_y_en_la_base(armar, fabrica, reloj, caplog):
    caplog.set_level(logging.DEBUG)

    await _recorrer_flujos(armar, fabrica, reloj)

    logs = "\n".join(FormateadorJson().format(r) for r in caplog.records) + caplog.text
    base = _volcar_base(fabrica)
    texto_base = " ".join(base.values())
    assert caplog.records, "la prueba debe ejercitar el registro de logs"
    assert hallazgos_de_pan(logs) == []
    assert hallazgos_de_pan(texto_base) == []
    assert [t for t in TOKENS if t in logs] == []
    columnas_con_token = {col for col, valores in base.items() if any(t in valores for t in TOKENS)}
    assert columnas_con_token == {"tokens_tarjeta.token"}


def test_el_detector_encuentra_panes_con_y_sin_separadores():
    texto = "a 4111111111111111 b 5555-5555-5555-4444 c 3782 822463 10005 d 1234567890123"
    assert hallazgos_de_pan(texto) == ["4111111111111111", "5555555555554444", "378282246310005"]


# --- segunda defensa: el filtro de logs ------------------------------------------------------------------------

def _registro(mensaje: str, *args) -> logging.LogRecord:
    return logging.LogRecord("pagos", logging.INFO, __file__, 1, mensaje, args, None)


@pytest.mark.parametrize("dato", [*PANES, "4111 1111 1111 1111", "tok_sim_x9"])
def test_el_filtro_enmascara_panes_y_tokens_aunque_alguien_los_registre(dato):
    registro = _registro("cobro con %s", dato)
    FiltroDatosTarjeta().filter(registro)
    assert dato not in registro.getMessage()
    assert ENMASCARADO_PAN in registro.getMessage() or ENMASCARADO_TOKEN in registro.getMessage()


def test_el_filtro_enmascara_la_traza_de_una_excepcion():
    try:
        raise ValueError(f"tarjeta {PANES[0]}")
    except ValueError:
        import sys

        registro = logging.LogRecord("pagos", logging.ERROR, __file__, 1, "fallo", None, sys.exc_info())
    FiltroDatosTarjeta().filter(registro)
    assert PANES[0] not in FormateadorJson().format(registro)


def test_el_log_es_json_con_trace_id_y_correlation_id():
    t1, t2 = traza_id.set("traza-1"), correlacion_id.set("corr-1")
    try:
        linea = json.loads(FormateadorJson().format(_registro("hola %s", "mundo")))
    finally:
        traza_id.reset(t1)
        correlacion_id.reset(t2)
    assert (linea["message"], linea["trace_id"], linea["correlation_id"], linea["level"]) == ("hola mundo", "traza-1", "corr-1", "INFO")


def test_redactar_no_toca_identificadores_comunes():
    texto = "transaccion_id=6f1c2a90-3b4d-4e5f-8a7b-1c2d3e4f5a6b monto=175000.00 intento=2"
    assert redactar(texto) == texto
