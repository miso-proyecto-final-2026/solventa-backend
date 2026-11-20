"""`POST /partners/v1/cotizaciones` (HU32, criterios 3 a 6, 9 y 11)."""

import logging
from datetime import UTC, datetime, timedelta

import jwt
from app.socios.errores import ServicioNoDisponible, SolicitudInvalida
from app.socios.v1.api import cliente_id_para

from tests.dobles import SECRETO_JWT_PRUEBA, AlmacenCaido

CUERPO = {"producto": "microseguro_vida", "referencia_cliente": "cli-001", "datos": {"edad": 35, "suma_asegurada": 50000000}}


def _post(cliente, token, cuerpo=CUERPO, clave="clave-1", **cabeceras):
    h = {"Authorization": f"Bearer {token}", **cabeceras}
    if clave is not None:
        h["X-Idempotency-Key"] = clave
    return cliente.post("/partners/v1/cotizaciones", json=cuerpo, headers=h)


def test_cotiza_con_un_token_valido_y_responde_solo_lo_publico(cliente, token, cotizaciones):
    r = _post(cliente, token())

    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["referencia_cliente"] == "cli-001"
    assert cuerpo["prima_mensual"] == 10000
    assert cuerpo["coberturas"] == [{"tipo": "muerte", "monto": 50000000, "deducible": 0}]
    # El desglose, la metadata y el cliente interno se quedan en Cotización.
    assert {"componentes", "metadata", "cliente_id", "asesor_id", "version_reglas"}.isdisjoint(cuerpo)
    assert cotizaciones.llamadas == [(cliente_id_para("socio-a", "cli-001"), "microseguro_vida", CUERPO["datos"])]


def test_el_cliente_final_se_deriva_del_socio_y_no_choca_entre_socios():
    assert cliente_id_para("socio-a", "cli-001") == cliente_id_para("socio-a", "cli-001")
    assert cliente_id_para("socio-a", "cli-001") != cliente_id_para("socio-b", "cli-001")
    assert len(cliente_id_para("socio-a", "x" * 64)) == 36


def test_sin_token_responde_401_con_www_authenticate(cliente):
    r = cliente.post("/partners/v1/cotizaciones", json=CUERPO, headers={"X-Idempotency-Key": "k"})

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"
    assert r.json()["error"]["trace_id"]
    assert r.headers["WWW-Authenticate"].startswith("Bearer")


def test_un_token_firmado_con_otro_secreto_es_invalido(cliente):
    falso = jwt.encode(
        {"sub": "socio-a", "iss": "solventa-socios", "aud": "solventa-partners", "iat": 0, "exp": 2**31},
        "otro-secreto-de-al-menos-32-caracteres!!",
        algorithm="HS256",
    )

    r = _post(cliente, falso)

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


def test_un_token_vencido_responde_token_expired(cliente, sembrar):
    sembrar()
    hace_dos_horas = datetime.now(UTC) - timedelta(hours=2)
    vencido = jwt.encode(
        {
            "sub": "socio-a",
            "iss": "solventa-socios",
            "aud": "solventa-partners",
            "iat": int(hace_dos_horas.timestamp()),
            "exp": int((hace_dos_horas + timedelta(hours=1)).timestamp()),
        },
        SECRETO_JWT_PRUEBA,
        algorithm="HS256",
    )

    r = _post(cliente, vencido)

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_un_endpoint_no_habilitado_en_el_contrato_responde_403(cliente, token, cotizaciones):
    r = _post(cliente, token(endpoints=()))

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ENDPOINT_NOT_AUTHORIZED"
    assert cotizaciones.llamadas == []


def test_deshabilitar_el_endpoint_surte_efecto_sin_esperar_al_token(cliente, token, sembrar):
    t = token()
    assert _post(cliente, t, clave="a").status_code == 201

    sembrar(endpoints=())

    assert _post(cliente, t, clave="b").status_code == 403


def test_al_exceder_la_cuota_responde_429_con_retry_after(cliente, token, cotizaciones):
    t = token(cuota_rps=2)

    respuestas = [_post(cliente, t, clave=f"k{i}") for i in range(4)]

    assert [r.status_code for r in respuestas] == [201, 201, 429, 429]
    excedida = respuestas[2]
    assert excedida.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert excedida.headers["Retry-After"] == "1"
    assert excedida.headers["X-RateLimit-Limit"] == "2"


def test_la_cuota_es_por_socio(cliente, token):
    a = token("socio-a", cuota_rps=1)
    b = token("socio-b", cuota_rps=1)

    assert _post(cliente, a, clave="a1").status_code == 201
    assert _post(cliente, b, clave="b1").status_code == 201


def test_las_escrituras_exigen_x_idempotency_key(cliente, token, cotizaciones):
    r = _post(cliente, token(), clave=None)

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    assert r.json()["error"]["details"][0]["campo"] == "X-Idempotency-Key"
    assert cotizaciones.llamadas == []


def test_una_clave_de_idempotencia_con_caracteres_no_validos_se_rechaza(cliente, token):
    r = _post(cliente, token(), clave="clave con espacios")

    assert r.status_code == 400


def test_repetir_la_clave_devuelve_el_resultado_original_sin_recalcular(cliente, token, cotizaciones):
    t = token()

    primera = _post(cliente, t, clave="repetida")
    segunda = _post(cliente, t, clave="repetida")

    assert primera.status_code == segunda.status_code == 201
    assert segunda.json() == primera.json()
    assert segunda.headers["X-Idempotent-Replayed"] == "true"
    assert len(cotizaciones.llamadas) == 1


def test_la_misma_clave_con_otro_cuerpo_es_un_conflicto(cliente, token, cotizaciones):
    t = token()
    _post(cliente, t, clave="k")

    r = _post(cliente, t, cuerpo={**CUERPO, "referencia_cliente": "otro"}, clave="k")

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(cotizaciones.llamadas) == 1


def test_la_misma_clave_en_dos_socios_no_se_cruza(cliente, token, cotizaciones):
    a = token("socio-a")
    b = token("socio-b")

    ra = _post(cliente, a, clave="compartida")
    rb = _post(cliente, b, clave="compartida")

    assert ra.json()["id"] != rb.json()["id"]
    assert len(cotizaciones.llamadas) == 2


def test_si_cotizacion_falla_la_clave_se_libera_para_reintentar(cliente, token, cotizaciones):
    t = token()
    cotizaciones.error = ServicioNoDisponible()

    caida = _post(cliente, t, clave="reintento")
    cotizaciones.error = None
    reintento = _post(cliente, t, clave="reintento")

    assert caida.status_code == 503
    assert caida.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert reintento.status_code == 201
    assert len(cotizaciones.llamadas) == 2


def test_los_errores_de_validacion_de_cotizacion_llegan_al_socio(cliente, token, cotizaciones):
    cotizaciones.error = SolicitudInvalida(detalles=[{"campo": "datos.edad", "motivo": "fuera de rango"}])

    r = _post(cliente, token())

    assert r.status_code == 422
    assert r.json()["error"]["details"] == [{"campo": "datos.edad", "motivo": "fuera de rango"}]


def test_campos_desconocidos_o_referencias_invalidas_responden_422(cliente, token, cotizaciones):
    t = token()

    extra = _post(cliente, t, cuerpo={**CUERPO, "documento": "123"}, clave="a")
    referencia = _post(cliente, t, cuerpo={**CUERPO, "referencia_cliente": "con espacio"}, clave="b")

    assert extra.status_code == referencia.status_code == 422
    assert extra.json()["error"]["code"] == "INVALID_REQUEST"
    assert cotizaciones.llamadas == []


def test_sin_almacen_la_cuota_deja_pasar_pero_la_idempotencia_falla_cerrada(cliente, token, ensamblado, cotizaciones):
    from app.socios.cuota import LimitadorCuota
    from app.socios.idempotencia import Idempotencia

    from app.main import app

    t = token()
    caido = AlmacenCaido()
    app.state.limitador = LimitadorCuota(caido)
    app.state.idempotencia = Idempotencia(caido)

    r = _post(cliente, t)

    assert r.status_code == 503
    assert cotizaciones.llamadas == []


def test_si_no_se_puede_guardar_el_resultado_igual_se_entrega(cliente, token, almacen, monkeypatch):
    from app.socios.almacen import ErrorAlmacen

    t = token()

    async def falla(*_a, **_k):
        raise ErrorAlmacen("TimeoutError")

    monkeypatch.setattr(almacen, "escribir", falla)

    assert _post(cliente, t).status_code == 201


def test_el_token_no_queda_en_los_logs(cliente, token, caplog):
    t = token()
    caplog.set_level(logging.DEBUG)

    _post(cliente, t)

    texto = "\n".join(r.getMessage() + str(getattr(r, "campos", "")) for r in caplog.records)
    assert "cotizacion_socio" in texto
    assert t not in texto
