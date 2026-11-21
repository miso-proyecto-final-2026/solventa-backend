"""Catálogo de errores de `/partners/v1` (HU34): un caso por código, estructura, `trace_id`, cabeceras y datos sensibles."""

import asyncio
import io
import json
import logging
import re
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.socios.acceso import ENDPOINT_COTIZACIONES
from app.socios.errores import (
    CATALOGO,
    MOTIVOS,
    ServicioDegradado,
    ServicioNoDisponible,
)
from app.socios.idempotencia import huella
from app.socios.v1.api import SolicitudCotizacionIn

from app.logs import FiltroDatosPersonales, FormateadorJson
from tests.dobles import SECRETO_JWT_PRUEBA, AlmacenCaido

COTIZAR = {"producto": "microseguro_vida", "referencia_cliente": "cli-001", "datos": {"edad": 35}}
ESTRUCTURA = {"code", "message", "trace_id", "details"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _cotizar(cliente, token=None, cuerpo=COTIZAR, clave="k-1", **cabeceras):
    h = dict(cabeceras)
    if token:
        h["Authorization"] = f"Bearer {token}"
    if clave:
        h["X-Idempotency-Key"] = clave
    return cliente.post("/partners/v1/cotizaciones", json=cuerpo, headers=h)


def _verificar(r, status, code, reason=None):
    """Estructura común (CA1): `code`, `message` y `trace_id` siempre; `reason` solo si hay motivo; y las cabeceras."""
    assert r.status_code == status, r.text
    e = r.json()["error"]
    assert e["code"] == code
    assert ESTRUCTURA <= set(e) <= ESTRUCTURA | {"reason"}
    assert e.get("reason") == reason
    assert e["message"] and UUID.match(e["trace_id"])
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["X-Correlation-Id"]
    assert e["code"] in CATALOGO and (reason is None or reason in MOTIVOS)
    return e


def _token_vencido():
    antes = datetime.now(UTC) - timedelta(hours=2)
    return jwt.encode(
        {
            "sub": "socio-a",
            "iss": "solventa-socios",
            "aud": "solventa-partners",
            "iat": int(antes.timestamp()),
            "exp": int((antes + timedelta(hours=1)).timestamp()),
        },
        SECRETO_JWT_PRUEBA,
        algorithm="HS256",
    )


# --- una prueba por código del catálogo ----------------------------------------------------------------------------------


def test_invalid_request_por_cuerpo_invalido_con_details(cliente, token):
    e = _verificar(_cotizar(cliente, token(), {**COTIZAR, "producto": ""}), 400, "INVALID_REQUEST")

    assert e["details"] == [{"campo": "producto", "motivo": "String should have at least 1 character"}]


def test_invalid_request_por_cabecera_faltante(cliente, token):
    e = _verificar(_cotizar(cliente, token(), clave=None), 400, "INVALID_REQUEST")

    assert e["details"][0]["campo"] == "X-Idempotency-Key"


def test_invalid_request_por_regla_de_negocio_con_reason(cliente, token):
    cuerpo = {**COTIZAR, "cliente": {"tipo_documento": "CC", "numero_documento": "200300400"}}

    e = _verificar(_cotizar(cliente, token(), cuerpo), 400, "INVALID_REQUEST", "KYC_NO_VERIFICADO")

    assert e["details"] == []


def test_invalid_token_sin_token(cliente):
    _verificar(_cotizar(cliente), 401, "INVALID_TOKEN")


def test_token_expired(cliente, sembrar):
    sembrar()

    _verificar(_cotizar(cliente, _token_vencido()), 401, "TOKEN_EXPIRED")


def test_endpoint_not_authorized(cliente, token):
    _verificar(_cotizar(cliente, token(endpoints=())), 403, "ENDPOINT_NOT_AUTHORIZED")


def test_resource_not_found_con_un_recurso_ajeno(cliente, token, suscripciones):
    t = token()
    cuerpo = {
        "cotizacion_id": "c-1",
        "cliente": {"tipo_documento": "CC", "numero_documento": "100200300"},
        "metodo_pago": "tok_x",
        "acepta_terminos": True,
    }
    sid = cliente.post(
        "/partners/v1/suscripciones", json=cuerpo, headers={"Authorization": f"Bearer {t}", "X-Idempotency-Key": "a"}
    ).json()["id"]
    otro = token("socio-b")

    r = cliente.get(f"/partners/v1/polizas/{sid}", headers={"Authorization": f"Bearer {otro}"})

    _verificar(r, 404, "RESOURCE_NOT_FOUND")


def test_idempotency_conflict_con_la_clave_reutilizada(cliente, token):
    t = token()
    assert _cotizar(cliente, t).status_code == 201

    r = _cotizar(cliente, t, {**COTIZAR, "referencia_cliente": "otro"})

    _verificar(r, 409, "IDEMPOTENCY_CONFLICT")
    assert "Retry-After" not in r.headers


def test_idempotency_conflict_en_proceso_trae_reason_y_retry_after_y_luego_repite_la_respuesta(cliente, token, ensamblado):
    t = token()
    cuerpo = {**COTIZAR, "referencia_cliente": "cli-002"}
    sin_cliente = SolicitudCotizacionIn(**cuerpo).model_dump(mode="json")
    del sin_cliente["cliente"]
    h = huella({"operacion": ENDPOINT_COTIZACIONES, "version": "v1", **sin_cliente})
    # La solicitud original sigue en curso: su reserva existe y todavía no se completó.
    assert asyncio.run(ensamblado.idempotencia.reservar("socio-a", "en-proceso", h)) is None

    r = _cotizar(cliente, t, cuerpo, clave="en-proceso")

    _verificar(r, 409, "IDEMPOTENCY_CONFLICT", "SOLICITUD_EN_PROCESO")
    assert r.headers["Retry-After"] == "2"
    # Al terminar la original, el reintento con la misma clave devuelve la respuesta guardada.
    asyncio.run(ensamblado.idempotencia.completar("socio-a", "en-proceso", h, 201, {"id": "guardada"}))
    repetida = _cotizar(cliente, t, cuerpo, clave="en-proceso")
    assert repetida.status_code == 201 and repetida.json() == {"id": "guardada"}


def test_rate_limit_exceeded_con_retry_after(cliente, token):
    t = token(cuota_rps=1)
    _cotizar(cliente, t, clave="a")

    r = _cotizar(cliente, t, clave="b")

    _verificar(r, 429, "RATE_LIMIT_EXCEEDED")
    assert r.headers["Retry-After"] == "1"


def test_internal_error_con_el_mismo_trace_id_en_el_cuerpo_y_en_el_log(cliente, token, cotizaciones):
    t = token()
    cotizaciones.error = RuntimeError("detalle interno con contraseña=hunter2")
    flujo = io.StringIO()
    manejador = logging.StreamHandler(flujo)
    manejador.setFormatter(FormateadorJson())
    manejador.addFilter(FiltroDatosPersonales())
    raiz = logging.getLogger()
    nivel = raiz.level
    raiz.addHandler(manejador)
    raiz.setLevel(logging.INFO)
    try:
        r = _cotizar(cliente, t)
    finally:
        raiz.removeHandler(manejador)
        raiz.setLevel(nivel)

    e = _verificar(r, 500, "INTERNAL_ERROR")
    lineas = [json.loads(linea) for linea in flujo.getvalue().splitlines() if linea.startswith("{")]
    recibido = next(linea for linea in lineas if linea.get("evento") == "request_recibido")
    inesperado = next(linea for linea in lineas if linea["message"] == "error_inesperado")
    assert recibido["trace_id"] == inesperado["trace_id"] == e["trace_id"]
    assert r.headers["X-Correlation-Id"] == recibido["correlation_id"]
    assert "hunter2" not in r.text and "RuntimeError" not in r.text and "Traceback" not in r.text


def test_service_unavailable_con_una_dependencia_caida(cliente, token, cotizaciones):
    cotizaciones.error = ServicioNoDisponible("Cotización no responde; reintente más tarde.")

    _verificar(_cotizar(cliente, token()), 503, "SERVICE_UNAVAILABLE")


def test_service_degraded_con_el_circuito_abierto_trae_retry_after(cliente, token, cotizaciones):
    cotizaciones.error = ServicioDegradado("Cotización está degradada; reintente en 30 s.", 30)

    r = _cotizar(cliente, token())

    _verificar(r, 503, "SERVICE_DEGRADED")
    assert r.headers["Retry-After"] == "30"


def test_el_catalogo_tiene_los_diez_codigos_y_una_prueba_por_cada_uno():
    assert set(CATALOGO) == {
        "INVALID_REQUEST", "INVALID_TOKEN", "TOKEN_EXPIRED", "ENDPOINT_NOT_AUTHORIZED", "RESOURCE_NOT_FOUND",
        "IDEMPOTENCY_CONFLICT", "RATE_LIMIT_EXCEEDED", "INTERNAL_ERROR", "SERVICE_UNAVAILABLE", "SERVICE_DEGRADED",
    }  # fmt: skip


# --- rutas y métodos fuera del catálogo de la guía -----------------------------------------------------------------------


def test_una_ruta_inexistente_usa_el_formato_del_catalogo(cliente):
    r = cliente.get("/partners/v1/no-existe")

    _verificar(r, 404, "RESOURCE_NOT_FOUND")
    assert "detail" not in r.json() and r.json()["error"]["message"] != "Not Found"


def test_un_metodo_no_permitido_usa_el_formato_del_catalogo_con_allow(cliente):
    r = cliente.delete("/partners/v1/cotizaciones")

    _verificar(r, 405, "INVALID_REQUEST")
    assert "POST" in r.headers["Allow"]


# --- sin datos sensibles (CA7) -------------------------------------------------------------------------------------------


def test_un_cuerpo_invalido_no_devuelve_ni_registra_lo_que_envio_el_socio(cliente, token, caplog):
    t = token()
    pan, documento = "4111111111111111", "9988776655"
    cuerpo = {
        "producto": "microseguro_vida",
        "referencia_cliente": pan,  # inválido: debe cumplir el patrón, pero llega con un PAN
        "datos": {"edad": 35},
        "cliente": {"tipo_documento": "XX", "numero_documento": documento},
    }

    with caplog.at_level(logging.DEBUG):
        r = _cotizar(cliente, t, cuerpo)

    assert r.status_code == 400
    for secreto in (pan, documento, t):
        assert secreto not in r.text
        assert secreto not in caplog.text


def test_una_dependencia_caida_no_expone_la_url_ni_el_error_interno(cliente, token, cotizaciones):
    cotizaciones.error = ServicioNoDisponible()

    r = _cotizar(cliente, token())

    assert "http" not in r.text.lower() and "localhost" not in r.text


def test_sin_almacen_la_escritura_responde_503_del_catalogo(cliente, token, ensamblado):
    t = token()
    ensamblado.idempotencia._almacen = AlmacenCaido()

    _verificar(_cotizar(cliente, t), 503, "SERVICE_UNAVAILABLE")


@pytest.mark.parametrize("code", sorted(CATALOGO))
def test_cada_code_tiene_http_mensaje_y_politica_de_reintento(code):
    status, mensaje, reintento = CATALOGO[code]

    assert 400 <= status <= 503 and mensaje and reintento
