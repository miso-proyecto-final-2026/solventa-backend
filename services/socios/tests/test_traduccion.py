"""Traducción de errores internos al catálogo de socios (HU34, criterio 4): tabla completa, sin filtrar mensajes ni trazas."""

import json
import logging

import httpx
import pytest
from app.socios.clientes import ClienteCotizacion, ClienteIdentidad, ClienteSuscripcion
from app.socios.errores import MOTIVOS
from app.socios.traduccion import (
    COTIZACION,
    IDENTIDAD,
    NO_ENCONTRADO_SUSCRIPCION,
    REGLAS_SUSCRIPCION,
    SUSCRIPCION,
    traducir,
)

MENSAJE_INTERNO = "Mensaje interno que el socio no debe ver"
TRAZA_INTERNA = "traza-interna-del-servicio"


def _r(status, code=None, detalles=None):
    cuerpo = {"error": {"code": code, "message": MENSAJE_INTERNO, "details": detalles or [], "trace_id": TRAZA_INTERNA}}
    return httpx.Response(status, json=cuerpo if code else {"detail": MENSAJE_INTERNO})


CAMPO = [{"campo": "datos.edad", "motivo": "fuera de rango"}]

# (servicio, respuesta del servicio) → (HTTP, code, reason)
TABLA = [
    (COTIZACION, _r(400, "VALIDATION_ERROR", CAMPO), 400, "INVALID_REQUEST", None),
    (COTIZACION, _r(422, "VALIDATION_ERROR", CAMPO), 400, "INVALID_REQUEST", None),
    (COTIZACION, _r(404, "COTIZACION_NO_ENCONTRADA"), 404, "RESOURCE_NOT_FOUND", None),
    (COTIZACION, _r(404), 500, "INTERNAL_ERROR", None),
    (COTIZACION, _r(401), 500, "INTERNAL_ERROR", None),
    (COTIZACION, _r(503, "DEPENDENCIA_NO_DISPONIBLE"), 503, "SERVICE_UNAVAILABLE", None),
    (IDENTIDAD, _r(404), 400, "INVALID_REQUEST", "CLIENTE_NO_REGISTRADO"),
    (IDENTIDAD, _r(422), 400, "INVALID_REQUEST", None),
    (IDENTIDAD, _r(401), 500, "INTERNAL_ERROR", None),
    (IDENTIDAD, _r(500), 503, "SERVICE_UNAVAILABLE", None),
    (SUSCRIPCION, _r(422, "VALIDATION_ERROR", CAMPO), 400, "INVALID_REQUEST", None),
    (SUSCRIPCION, _r(409, "CLAVE_IDEMPOTENCIA_REUTILIZADA"), 409, "IDEMPOTENCY_CONFLICT", None),
    (SUSCRIPCION, _r(401, "CLAVE_DE_SERVICIO_INVALIDA"), 500, "INTERNAL_ERROR", None),
    (SUSCRIPCION, _r(409, "UN_CONFLICTO_NUEVO"), 500, "INTERNAL_ERROR", None),
    (SUSCRIPCION, _r(503, "DEPENDENCIA_NO_DISPONIBLE"), 503, "SERVICE_UNAVAILABLE", None),
    (SUSCRIPCION, httpx.Response(502, text="<html>bad gateway</html>"), 503, "SERVICE_UNAVAILABLE", None),
    *[(SUSCRIPCION, _r(404, c) if c != "ACCESO_DENEGADO" else _r(403, c), 404, "RESOURCE_NOT_FOUND", None) for c in sorted(NO_ENCONTRADO_SUSCRIPCION)],
    *[(SUSCRIPCION, _r(403 if c == "OTP_INVALIDO" else 409, c), 400, "INVALID_REQUEST", c) for c in sorted(REGLAS_SUSCRIPCION)],
]  # fmt: skip


@pytest.mark.parametrize(("servicio", "respuesta", "status", "code", "reason"), TABLA)
def test_cada_error_interno_se_traduce_al_catalogo(servicio, respuesta, status, code, reason):
    error = traducir(servicio, respuesta)

    assert (error.status, error.code, error.reason) == (status, code, reason)
    assert MENSAJE_INTERNO not in error.mensaje and TRAZA_INTERNA not in error.mensaje
    assert MENSAJE_INTERNO not in str(error.detalles) and TRAZA_INTERNA not in str(error.detalles)


def test_los_details_de_campo_pasan_pero_sin_otros_datos_del_servicio():
    error = traducir(COTIZACION, _r(422, "VALIDATION_ERROR", CAMPO + ["ruido"]))

    assert error.detalles == CAMPO


def test_el_log_enlaza_el_trace_id_de_origen_sin_exponerlo(caplog):
    with caplog.at_level(logging.WARNING, logger="app.socios.traduccion"):
        traducir(SUSCRIPCION, _r(409, "COTIZACION_YA_USADA"))

    campos = caplog.records[0].campos
    assert campos["evento"] == "error_traducido"
    assert campos["trace_id_origen"] == TRAZA_INTERNA
    assert (campos["servicio"], campos["status_origen"], campos["code_origen"]) == ("suscripcion", 409, "COTIZACION_YA_USADA")
    assert MENSAJE_INTERNO not in caplog.text


def test_un_trace_id_o_code_con_formato_raro_no_llega_al_log(caplog):
    r = httpx.Response(409, json={"error": {"code": "<script>", "trace_id": "x" * 500, "message": "m"}})

    with caplog.at_level(logging.WARNING, logger="app.socios.traduccion"):
        traducir(SUSCRIPCION, r)

    assert caplog.records[0].campos["code_origen"] == "" and caplog.records[0].campos["trace_id_origen"] == ""


def test_todo_reason_que_la_traduccion_produce_esta_en_la_lista_cerrada():
    assert REGLAS_SUSCRIPCION <= set(MOTIVOS)


def test_los_codes_de_suscripcion_alcanzables_desde_las_rutas_de_socios_tienen_traduccion_explicita():
    """La lista se revisa junto con `services/suscripcion/app/suscripcion/errores.py` y `api_socios.py`/`firma_socio.py`.

    Los servicios no se pueden importar en el mismo intérprete: la lista vive aquí. Si Suscripción agrega un `code`
    alcanzable desde Socios, esta prueba obliga a decidir su traducción.
    """
    alcanzables = {
        "SUSCRIPCION_NO_ENCONTRADA", "COTIZACION_NO_ENCONTRADA", "POLIZA_NO_ENCONTRADA", "FIRMA_NO_ENCONTRADA",
        "ACCESO_DENEGADO", "VALIDATION_ERROR", "CLAVE_IDEMPOTENCIA_REUTILIZADA", "COTIZACION_YA_USADA",
        "COTIZACION_VENCIDA", "COTIZACION_SIN_CUENTA", "SIN_METODO_PAGO", "POLIZA_NO_LISTA", "FIRMA_NO_DISPONIBLE",
        "FIRMA_VENCIDA", "FIRMA_FALLIDA", "DOCUMENTO_ALTERADO", "OTP_INVALIDO", "CERTIFICADO_NO_DISPONIBLE",
        "DEPENDENCIA_NO_DISPONIBLE",
    }  # fmt: skip
    traducidos = NO_ENCONTRADO_SUSCRIPCION | REGLAS_SUSCRIPCION | {
        "VALIDATION_ERROR", "CLAVE_IDEMPOTENCIA_REUTILIZADA", "DEPENDENCIA_NO_DISPONIBLE",
    }  # fmt: skip

    assert alcanzables == traducidos


# --- de extremo a extremo por los clientes HTTP ------------------------------------------------------------------------


async def test_los_clientes_usan_la_traduccion_central():
    def cliente(clase, base, *args):
        def con(respuesta):
            return clase(httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(lambda _p: respuesta)), *args)

        return con

    with pytest.raises(Exception) as cot:
        await cliente(ClienteCotizacion, "http://c")(_r(422, "VALIDATION_ERROR", CAMPO)).crear("c", "p", {})
    with pytest.raises(Exception) as ide:
        await cliente(ClienteIdentidad, "http://i", "k")(_r(422)).identificar("CC", "1")
    with pytest.raises(Exception) as sus:
        await cliente(ClienteSuscripcion, "http://s", "k")(_r(409, "FIRMA_VENCIDA")).detalle("s", "a")

    assert cot.value.code == ide.value.code == sus.value.code == "INVALID_REQUEST"
    assert sus.value.reason == "FIRMA_VENCIDA"
    assert json.dumps(cot.value.detalles) == json.dumps(CAMPO)
