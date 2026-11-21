"""Clientes HTTP de Identidad y Suscripción (HU33): contrato REST, traducción de errores y propagación de la traza."""

import json

import httpx
import pytest
from app.socios.clientes import ClienteIdentidad, ClienteSuscripcion
from app.socios.errores import (
    ConflictoIdempotencia,
    ErrorInterno,
    RecursoNoEncontrado,
    ReglaDeNegocio,
    ServicioNoDisponible,
    SolicitudInvalida,
)

from app.contexto import correlacion_id

CLAVE = "clave-de-servicio-de-prueba"


def _identidad(manejador) -> ClienteIdentidad:
    return ClienteIdentidad(httpx.AsyncClient(base_url="http://identidad", transport=httpx.MockTransport(manejador)), CLAVE)


def _suscripcion(manejador) -> ClienteSuscripcion:
    return ClienteSuscripcion(httpx.AsyncClient(base_url="http://suscripcion", transport=httpx.MockTransport(manejador)), CLAVE)


def _error(status, code, mensaje="Mensaje de Suscripción.", detalles=None):
    return httpx.Response(status, json={"error": {"code": code, "message": mensaje, "details": detalles or [], "trace_id": "t-1"}})


# --- Identidad ---------------------------------------------------------------------------------------------------------


async def test_identifica_con_el_documento_en_el_cuerpo_y_la_clave_de_servicio():
    vistas = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion)
        return httpx.Response(200, json={"cliente_id": "c-1", "estado_kyc": "VERIFICADO", "consentimiento_vigente": True})

    token = correlacion_id.set("corr-9")
    try:
        r = await _identidad(manejador).identificar("CC", "100200300")
    finally:
        correlacion_id.reset(token)

    assert r == {"cliente_id": "c-1", "estado_kyc": "VERIFICADO", "consentimiento_vigente": True}
    peticion = vistas[0]
    assert (peticion.method, peticion.url.path, peticion.url.query) == ("POST", "/v1/interno/clientes/identificacion", b"")
    assert json.loads(peticion.content) == {"tipo_documento": "CC", "numero_documento": "100200300"}
    assert (peticion.headers["X-Service-Key"], peticion.headers["X-Correlation-Id"]) == (CLAVE, "corr-9")
    assert "100200300" not in str(peticion.url)


async def test_un_cliente_inexistente_es_none():
    assert await _identidad(lambda _p: httpx.Response(404, json={"detail": "Cliente no encontrado"})).identificar("CC", "1") is None


async def test_un_documento_invalido_es_solicitud_invalida_sin_repetir_el_documento():
    with pytest.raises(SolicitudInvalida) as error:
        await _identidad(lambda _p: httpx.Response(422, json={"detail": "El número de documento 123 no es válido"})).identificar("CC", "123")

    assert error.value.detalles[0]["campo"] == "cliente" and "123" not in str(error.value.detalles)


@pytest.mark.parametrize("estado", [500, 503])
async def test_un_5xx_de_identidad_es_servicio_no_disponible(estado):
    with pytest.raises(ServicioNoDisponible):
        await _identidad(lambda _p: httpx.Response(estado)).identificar("CC", "100200300")


async def test_un_error_de_red_hacia_identidad_es_servicio_no_disponible():
    def caida(_p):
        raise httpx.ConnectTimeout("lento")

    with pytest.raises(ServicioNoDisponible):
        await _identidad(caida).identificar("CC", "100200300")


# --- Suscripción -------------------------------------------------------------------------------------------------------


async def test_inicia_con_la_clave_de_idempotencia_del_socio_y_la_clave_de_servicio():
    vistas = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion)
        return httpx.Response(202, json={"id": "s-1"})

    r = await _suscripcion(manejador).iniciar(
        "socio-a", "c-1", "k-1", cotizacion_id="cot-1", metodo_pago="tok_sim_1", acepta_terminos=True, periodicidad_pago="ANUAL"
    )

    assert r == {"id": "s-1"}
    peticion = vistas[0]
    assert (peticion.method, peticion.url.path) == ("POST", "/v1/interno/socios/suscripciones")
    assert (peticion.headers["Idempotency-Key"], peticion.headers["X-Service-Key"]) == ("k-1", CLAVE)
    assert json.loads(peticion.content) == {
        "socio_client_id": "socio-a",
        "cliente_id": "c-1",
        "cotizacion_id": "cot-1",
        "metodo_pago": "tok_sim_1",
        "acepta_terminos": True,
        "periodicidad_pago": "ANUAL",
    }


async def test_las_consultas_llevan_el_socio_en_la_consulta_y_no_en_la_ruta():
    vistas = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion)
        if peticion.url.path.endswith("/pdf"):
            return httpx.Response(200, content=b"%PDF-1.4")
        return httpx.Response(200, json={"ok": True})

    c = _suscripcion(manejador)
    await c.detalle("s-1", "socio-a")
    await c.documento_firma("s-1", "socio-a")
    pdf = await c.documento_pdf("s-1", "socio-a")

    assert pdf == b"%PDF-1.4"
    assert [(v.url.path, v.url.params["socio_client_id"]) for v in vistas] == [
        ("/v1/interno/socios/suscripciones/s-1", "socio-a"),
        ("/v1/interno/socios/suscripciones/s-1/documento-firma", "socio-a"),
        ("/v1/interno/socios/suscripciones/s-1/documento-firma/pdf", "socio-a"),
    ]


async def test_el_codigo_y_la_firma_llevan_el_socio_en_el_cuerpo():
    vistas = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion)
        return httpx.Response(202) if peticion.url.path.endswith("/codigo") else httpx.Response(200, json={"estado_saga": "EMITIDA"})

    c = _suscripcion(manejador)
    await c.solicitar_codigo("s-1", "socio-a")
    r = await c.firmar("s-1", "socio-a", {"codigo": "123456", "canal": "WEB"})

    assert r == {"estado_saga": "EMITIDA"}
    assert json.loads(vistas[0].content) == {"socio_client_id": "socio-a"}
    assert json.loads(vistas[1].content) == {"socio_client_id": "socio-a", "codigo": "123456", "canal": "WEB"}


@pytest.mark.parametrize(
    ("respuesta", "esperado"),
    [
        (_error(404, "SUSCRIPCION_NO_ENCONTRADA"), RecursoNoEncontrado),
        (_error(409, "COTIZACION_YA_USADA"), ReglaDeNegocio),
        (_error(409, "CLAVE_IDEMPOTENCIA_REUTILIZADA"), ConflictoIdempotencia),
        (_error(422, "VALIDATION_ERROR", detalles=[{"campo": "metodo_pago", "motivo": "x"}]), SolicitudInvalida),
        (_error(400, "ERROR"), ErrorInterno),
        (_error(403, "OTP_INVALIDO"), ReglaDeNegocio),
        (_error(401, "CLAVE_DE_SERVICIO_INVALIDA"), ErrorInterno),
        (_error(503, "DEPENDENCIA_NO_DISPONIBLE"), ServicioNoDisponible),
        (httpx.Response(500, text="boom"), ServicioNoDisponible),
    ],
)
async def test_la_tabla_de_traduccion_de_errores(respuesta, esperado):
    with pytest.raises(esperado):
        await _suscripcion(lambda _p: respuesta).detalle("s-1", "socio-a")


async def test_un_conflicto_de_negocio_se_traduce_a_su_reason_con_mensaje_propio():
    with pytest.raises(ReglaDeNegocio) as error:
        await _suscripcion(lambda _p: _error(409, "COTIZACION_YA_USADA", "La cotización ya tiene una suscripción en curso.")).detalle("s", "a")

    assert error.value.reason == "COTIZACION_YA_USADA" and error.value.detalles == []
    assert error.value.status == 400
    assert error.value.mensaje != "La cotización ya tiene una suscripción en curso."


async def test_un_otp_erroneo_conserva_el_motivo_y_un_404_no_revela_nada():
    with pytest.raises(ReglaDeNegocio) as otp:
        await _suscripcion(lambda _p: _error(403, "OTP_INVALIDO")).firmar("s", "a", {})
    with pytest.raises(RecursoNoEncontrado) as ausente:
        await _suscripcion(lambda _p: _error(404, "SUSCRIPCION_NO_ENCONTRADA")).detalle("s", "a")

    assert otp.value.reason == "OTP_INVALIDO"
    assert ausente.value.detalles == []


async def test_los_detalles_de_validacion_de_suscripcion_pasan_al_socio():
    r = _error(422, "VALIDATION_ERROR", detalles=[{"campo": "metodo_pago", "motivo": "debe ser el token"}, "ruido"])

    with pytest.raises(SolicitudInvalida) as error:
        await _suscripcion(lambda _p: r).detalle("s", "a")

    assert error.value.detalles == [{"campo": "metodo_pago", "motivo": "debe ser el token"}]
