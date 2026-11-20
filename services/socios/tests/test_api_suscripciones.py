"""`/partners/v1/suscripciones` y `/partners/v1/polizas/{id}` (HU33, criterios 1 a 7, 9 y 10)."""

import logging

import httpx
import pytest
from app.socios.errores import (
    ConflictoDeNegocio,
    ServicioNoDisponible,
    SolicitudInvalida,
)

from tests.dobles import CLIENTE_APTO, DOCUMENTO_APTO, SuscripcionesFalsas

COTIZACION = "00000000-0000-4000-8000-000000000001"
TOKEN_PAGO = "tok_sim_0f8fad5b-d9cb-469f-a165-70867728950e"
CLIENTE = {"tipo_documento": DOCUMENTO_APTO[0], "numero_documento": DOCUMENTO_APTO[1]}
SUSCRIPCION = {
    "cotizacion_id": COTIZACION,
    "cliente": CLIENTE,
    "metodo_pago": TOKEN_PAGO,
    "acepta_terminos": True,
    "periodicidad_pago": "ANUAL",
}
OTP = SuscripcionesFalsas.OTP
HASH = SuscripcionesFalsas.HASH


def _cabeceras(token, clave=None):
    h = {"Authorization": f"Bearer {token}"}
    if clave is not None:
        h["X-Idempotency-Key"] = clave
    return h


def _suscribir(cliente, token, cuerpo=SUSCRIPCION, clave="k1"):
    return cliente.post("/partners/v1/suscripciones", json=cuerpo, headers=_cabeceras(token, clave))


def _firmar(cliente, token, suscripcion_id, clave="f1", **cambios):
    cuerpo = {"documento_sha256": HASH, "documento_revisado": True, "codigo": OTP, "canal": "MOVIL", **cambios}
    return cliente.post(f"/partners/v1/suscripciones/{suscripcion_id}/firma", json=cuerpo, headers=_cabeceras(token, clave))


# --- iniciar (criterios 1, 2 y 7) ---------------------------------------------------------------------------------------


def test_inicia_la_saga_a_nombre_del_cliente_y_atribuye_la_venta_al_socio(cliente, token, suscripciones):
    r = _suscribir(cliente, token())

    assert r.status_code == 202
    cuerpo = r.json()
    assert cuerpo["estado"] == "EN_PROGRESO" and cuerpo["cotizacion_id"] == COTIZACION
    assert cuerpo["poliza_url"] == f"/partners/v1/polizas/{cuerpo['id']}"
    # Atribución: Suscripción recibe el socio autenticado, el cliente real de Identidad y la clave del socio.
    assert suscripciones.llamadas == [("iniciar", ("socio-a", CLIENTE_APTO, "k1", COTIZACION, TOKEN_PAGO, True, "ANUAL"))]


def test_la_respuesta_no_trae_datos_internos_ni_del_cliente(cliente, token):
    r = _suscribir(cliente, token())

    assert set(r.json()) == {
        "id", "cotizacion_id", "estado", "paso_actual", "producto", "moneda", "monto_asegurado", "periodicidad_pago",
        "creada_en", "poliza_url",
    }
    assert DOCUMENTO_APTO[1] not in r.text and CLIENTE_APTO not in r.text and TOKEN_PAGO not in r.text


def test_sin_cuerpo_valido_responde_422(cliente, token, suscripciones):
    t = token()
    sin_cliente = {k: v for k, v in SUSCRIPCION.items() if k != "cliente"}

    for cuerpo in (
        sin_cliente,
        {**SUSCRIPCION, "periodicidad_pago": "SEMANAL"},
        {**SUSCRIPCION, "extra": 1},
        {**SUSCRIPCION, "cliente": {**CLIENTE, "tipo_documento": "XX"}},
        {**SUSCRIPCION, "cliente": {**CLIENTE, "numero_documento": "12"}},
    ):
        r = _suscribir(cliente, t, cuerpo, clave="x")
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST"
    assert suscripciones.llamadas == []


def test_exige_x_idempotency_key(cliente, token, suscripciones):
    r = _suscribir(cliente, token(), clave=None)

    assert r.status_code == 400 and r.json()["error"]["details"][0]["campo"] == "X-Idempotency-Key"
    assert suscripciones.llamadas == []


# --- idempotencia (criterio 3) --------------------------------------------------------------------------------------


def test_repetir_la_clave_devuelve_la_misma_venta_sin_llamar_otra_vez(cliente, token, suscripciones, identidad):
    t = token()

    primera = _suscribir(cliente, t)
    segunda = _suscribir(cliente, t)

    assert primera.status_code == segunda.status_code == 202
    assert segunda.json() == primera.json()
    assert segunda.headers["X-Idempotent-Replayed"] == "true"
    assert len(suscripciones.llamadas) == 1 and len(identidad.consultas) == 1


def test_la_misma_clave_con_otro_contenido_es_409(cliente, token, suscripciones):
    t = token()
    _suscribir(cliente, t)

    r = _suscribir(cliente, t, {**SUSCRIPCION, "periodicidad_pago": "MENSUAL"})

    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(suscripciones.llamadas) == 1


def test_la_misma_clave_en_dos_socios_no_se_cruza(cliente, token, suscripciones):
    a, b = token("socio-a"), token("socio-b")

    ra = _suscribir(cliente, a)
    rb = _suscribir(cliente, b)

    assert ra.json()["id"] != rb.json()["id"]
    assert [llamada[1][0] for llamada in suscripciones.llamadas] == ["socio-a", "socio-b"]


def test_si_la_suscripcion_falla_la_clave_se_libera_para_reintentar(cliente, token, suscripciones):
    t = token()
    suscripciones.error = ServicioNoDisponible()

    caida = _suscribir(cliente, t)
    suscripciones.error = None
    reintento = _suscribir(cliente, t)

    assert caida.status_code == 503 and caida.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert reintento.status_code == 202


# --- identidad del cliente (criterio 5) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("documento", "motivo"),
    [
        (("CC", "999999999"), "CLIENTE_NO_REGISTRADO"),
        (("CC", "200300400"), "KYC_NO_VERIFICADO"),
        (("CC", "300400500"), "CONSENTIMIENTO_NO_VIGENTE"),
    ],
)
def test_un_cliente_que_no_puede_comprar_responde_422_con_el_motivo(cliente, token, suscripciones, documento, motivo):
    cuerpo = {**SUSCRIPCION, "cliente": {"tipo_documento": documento[0], "numero_documento": documento[1]}}

    r = _suscribir(cliente, token(), cuerpo)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    assert r.json()["error"]["details"] == [{"campo": "cliente", "motivo": motivo}]
    assert suscripciones.llamadas == []


def test_si_identidad_no_responde_es_503_y_el_documento_no_se_escribe_en_los_logs(cliente, token, identidad, caplog):
    identidad.error = ServicioNoDisponible("Identidad no responde; reintente más tarde.")

    with caplog.at_level(logging.DEBUG):
        r = _suscribir(cliente, token())

    assert r.status_code == 503
    assert DOCUMENTO_APTO[1] not in caplog.text


# --- traducción de errores (criterio 9) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status", "codigo", "detalles"),
    [
        (ConflictoDeNegocio(detalles=[{"campo": "motivo", "motivo": "COTIZACION_YA_USADA"}]), 409, "IDEMPOTENCY_CONFLICT", [{"campo": "motivo", "motivo": "COTIZACION_YA_USADA"}]),
        (SolicitudInvalida(detalles=[{"campo": "metodo_pago", "motivo": "debe ser el token"}]), 422, "INVALID_REQUEST", [{"campo": "metodo_pago", "motivo": "debe ser el token"}]),
        (ServicioNoDisponible(), 503, "SERVICE_UNAVAILABLE", []),
    ],
)
def test_los_errores_de_suscripcion_llegan_al_socio_en_su_formato(cliente, token, suscripciones, error, status, codigo, detalles):
    suscripciones.error = error

    r = _suscribir(cliente, token())

    assert r.status_code == status
    cuerpo = r.json()["error"]
    assert (cuerpo["code"], cuerpo["details"]) == (codigo, detalles)
    assert cuerpo["trace_id"] and cuerpo["message"]


# --- consultar y aislamiento (criterios 4, 8 y 10) -------------------------------------------------------------------------


def test_consulta_el_estado_de_la_venta_y_filtra_los_campos_internos(cliente, token, suscripciones):
    t = token()
    creada = _suscribir(cliente, t).json()

    r = cliente.get(f"/partners/v1/polizas/{creada['id']}", headers=_cabeceras(t))

    assert r.status_code == 200
    cuerpo = r.json()
    assert (cuerpo["id"], cuerpo["estado"], cuerpo["poliza"]) == (creada["id"], "EN_PROGRESO", None)
    assert cuerpo["pasos"] == ["DECISION", "COBRO", "FIRMA", "EMISION"]
    for interno in ("revision_asistida", "_socio", "_cliente_id", "cliente_id", "socio_client_id"):
        assert interno not in r.text


def test_la_venta_de_otro_socio_responde_404_igual_que_una_inexistente(cliente, token):
    a, b = token("socio-a"), token("socio-b")
    creada = _suscribir(cliente, a).json()

    ajena = cliente.get(f"/partners/v1/polizas/{creada['id']}", headers=_cabeceras(b))
    inexistente = cliente.get("/partners/v1/polizas/no-existe", headers=_cabeceras(b))

    assert ajena.status_code == inexistente.status_code == 404
    assert ajena.json()["error"]["code"] == inexistente.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert ajena.json()["error"]["details"] == inexistente.json()["error"]["details"]


def test_consultar_exige_el_endpoint_en_el_contrato(cliente, token):
    t = token(endpoints=("cotizaciones:crear", "suscripciones:crear"))

    r = cliente.get("/partners/v1/polizas/x", headers=_cabeceras(t))

    assert r.status_code == 403 and r.json()["error"]["code"] == "ENDPOINT_NOT_AUTHORIZED"


def test_crear_exige_el_endpoint_en_el_contrato(cliente, token, suscripciones):
    r = _suscribir(cliente, token(endpoints=("cotizaciones:crear",)))

    assert r.status_code == 403 and r.json()["error"]["code"] == "ENDPOINT_NOT_AUTHORIZED"
    assert suscripciones.llamadas == []


@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [
        ("post", "/partners/v1/suscripciones"),
        ("get", "/partners/v1/polizas/x"),
        ("get", "/partners/v1/suscripciones/x/documento-firma"),
        ("get", "/partners/v1/suscripciones/x/documento-firma/pdf"),
        ("post", "/partners/v1/suscripciones/x/firma/codigo"),
        ("post", "/partners/v1/suscripciones/x/firma"),
    ],
)
def test_todas_las_rutas_exigen_un_token(cliente, metodo, ruta):
    r = getattr(cliente, metodo)(ruta, headers={"X-Idempotency-Key": "k"})

    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"


# --- firma embebida y emisión (criterios 6 y 8) --------------------------------------------------------------------------


def test_el_cliente_lee_el_documento_y_el_socio_lo_descarga(cliente, token):
    t = token()
    sid = _suscribir(cliente, t).json()["id"]

    doc = cliente.get(f"/partners/v1/suscripciones/{sid}/documento-firma", headers=_cabeceras(t))
    pdf = cliente.get(f"/partners/v1/suscripciones/{sid}/documento-firma/pdf", headers=_cabeceras(t))

    assert doc.status_code == 200 and doc.json()["documento_sha256"] == HASH and doc.json()["estado"] == "PENDIENTE"
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.headers["cache-control"] == "no-store"


def test_la_venta_completa_hasta_la_poliza_emitida(cliente, token, suscripciones):
    t = token()
    sid = _suscribir(cliente, t).json()["id"]
    assert cliente.post(f"/partners/v1/suscripciones/{sid}/firma/codigo", headers=_cabeceras(t)).status_code == 202

    firma = _firmar(cliente, t, sid, canal="WEB", dispositivo="App del socio", ip_cliente="190.9.8.7")
    consulta = cliente.get(f"/partners/v1/polizas/{sid}", headers=_cabeceras(t)).json()

    assert firma.status_code == 200
    assert (firma.json()["estado"], firma.json()["canal"], firma.json()["documento_sha256"]) == ("EMITIDA", "WEB", HASH)
    assert "modalidad" not in firma.json() and "poliza_id" not in firma.json()
    assert (consulta["estado"], consulta["firma"]["estado"]) == ("EMITIDA", "FIRMADA")
    assert consulta["poliza"]["numero_poliza"] == "POL-2026-000001" and consulta["poliza"]["estado"] == "VIGENTE"
    # El socio reenvía lo que sabe del cliente; el OTP viaja a Suscripción y de ahí no sale.
    enviado = next(c for c in suscripciones.llamadas if c[0] == "firmar")[1][2]
    assert (enviado["dispositivo"], enviado["ip"], enviado["canal"]) == ("App del socio", "190.9.8.7", "WEB")


def test_repetir_la_clave_de_firma_devuelve_la_misma_firma(cliente, token, suscripciones):
    t = token()
    sid = _suscribir(cliente, t).json()["id"]

    primera = _firmar(cliente, t, sid)
    segunda = _firmar(cliente, t, sid)

    assert segunda.status_code == 200 and segunda.json() == primera.json()
    assert segunda.headers["X-Idempotent-Replayed"] == "true"
    assert [c[0] for c in suscripciones.llamadas].count("firmar") == 1


def test_la_firma_con_la_misma_clave_y_otro_codigo_es_409(cliente, token):
    t = token()
    sid = _suscribir(cliente, t).json()["id"]
    _firmar(cliente, t, sid)

    r = _firmar(cliente, t, sid, codigo="654321")

    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_un_codigo_erroneo_es_422_y_deja_reintentar_con_la_misma_clave(cliente, token):
    t = token()
    sid = _suscribir(cliente, t).json()["id"]

    mala = _firmar(cliente, t, sid, codigo="000000")
    buena = _firmar(cliente, t, sid, codigo=OTP)

    assert mala.status_code == 422 and mala.json()["error"]["details"] == [{"campo": "motivo", "motivo": "OTP_INVALIDO"}]
    assert buena.status_code == 200


def test_el_codigo_del_cliente_no_se_escribe_en_los_logs(cliente, token, caplog):
    t = token()
    sid = _suscribir(cliente, t).json()["id"]

    with caplog.at_level(logging.DEBUG):
        _firmar(cliente, t, sid, codigo="987654")

    assert "987654" not in caplog.text


def test_el_hash_de_la_firma_debe_ser_un_sha256(cliente, token, suscripciones):
    t = token()
    sid = _suscribir(cliente, t).json()["id"]

    r = _firmar(cliente, t, sid, documento_sha256="no-es-un-hash")

    assert r.status_code == 422 and not [c for c in suscripciones.llamadas if c[0] == "firmar"]


@pytest.mark.parametrize(
    ("metodo", "cola", "cuerpo"),
    [
        ("get", "/documento-firma", None),
        ("get", "/documento-firma/pdf", None),
        ("post", "/firma/codigo", None),
        ("post", "/firma", {"documento_sha256": HASH, "documento_revisado": True, "codigo": OTP, "canal": "WEB"}),
    ],
)
def test_la_firma_de_la_venta_de_otro_socio_responde_404(cliente, token, metodo, cola, cuerpo):
    a, b = token("socio-a"), token("socio-b")
    sid = _suscribir(cliente, a).json()["id"]
    cabeceras = _cabeceras(b, "f1" if metodo == "post" else None)

    r = getattr(cliente, metodo)(f"/partners/v1/suscripciones/{sid}{cola}", headers=cabeceras, **({"json": cuerpo} if cuerpo else {}))

    assert r.status_code == 404 and r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_firmar_exige_su_propio_endpoint_en_el_contrato(cliente, token):
    t = token(endpoints=("cotizaciones:crear", "suscripciones:crear", "polizas:consultar"))
    sid = _suscribir(cliente, t).json()["id"]

    assert _firmar(cliente, t, sid).status_code == 403
    assert cliente.get(f"/partners/v1/suscripciones/{sid}/documento-firma", headers=_cabeceras(t)).status_code == 403


# --- cotización con cliente (criterio 5) ---------------------------------------------------------------------------------


def _cotizar(cliente, token, cuerpo, clave="c1"):
    return cliente.post("/partners/v1/cotizaciones", json=cuerpo, headers=_cabeceras(token, clave))


COTIZAR = {"producto": "microseguro_vida", "referencia_cliente": "cli-001", "datos": {"edad": 35}}


def test_cotizar_con_cliente_usa_el_cliente_real_para_poder_suscribirlo(cliente, token, cotizaciones, identidad):
    r = _cotizar(cliente, token(), {**COTIZAR, "cliente": CLIENTE})

    assert r.status_code == 201
    assert cotizaciones.llamadas == [(CLIENTE_APTO, "microseguro_vida", {"edad": 35})]
    assert DOCUMENTO_APTO[1] not in r.text and CLIENTE_APTO not in r.text


def test_cotizar_sin_cliente_sigue_como_en_hu32(cliente, token, cotizaciones, identidad):
    from app.socios.v1.api import cliente_id_para

    r = _cotizar(cliente, token(), COTIZAR)

    assert r.status_code == 201
    assert cotizaciones.llamadas[0][0] == cliente_id_para("socio-a", "cli-001")
    assert identidad.consultas == []


def test_cotizar_con_un_cliente_sin_kyc_responde_422_y_libera_la_clave(cliente, token, cotizaciones, identidad):
    t = token()
    sin_kyc = {**COTIZAR, "cliente": {"tipo_documento": "CC", "numero_documento": "200300400"}}

    r = _cotizar(cliente, t, sin_kyc)

    assert r.status_code == 422 and r.json()["error"]["details"] == [{"campo": "cliente", "motivo": "KYC_NO_VERIFICADO"}]
    assert cotizaciones.llamadas == []
    assert _cotizar(cliente, t, {**COTIZAR, "cliente": CLIENTE}).status_code == 201


def test_la_huella_de_hu32_no_cambia_para_una_cotizacion_sin_cliente(cliente, token):
    t = token()
    primera = _cotizar(cliente, t, COTIZAR, clave="mismo")

    segunda = _cotizar(cliente, t, COTIZAR, clave="mismo")

    assert segunda.status_code == 201 and segunda.headers["X-Idempotent-Replayed"] == "true" and segunda.json() == primera.json()


# --- ensamblado y errores de red ----------------------------------------------------------------------------------------


async def test_una_caida_de_red_hacia_suscripcion_es_503():
    from app.socios.clientes import ClienteSuscripcion

    def caida(_peticion):
        raise httpx.ConnectError("sin red")

    http = httpx.AsyncClient(base_url="http://suscripcion", transport=httpx.MockTransport(caida))

    with pytest.raises(ServicioNoDisponible):
        await ClienteSuscripcion(http, "clave").detalle("x", "socio-a")
