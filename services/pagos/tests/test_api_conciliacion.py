"""API de conciliación (HU31): solo el rol OPERADOR, filtros, conciliar, escalar, exportar y errores distinguibles."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import jwt
import pytest
from app.pagos.acceso import VerificadorJwtHs256
from app.pagos.api_conciliacion import router
from app.pagos.conciliacion import ServicioConciliacion
from app.pagos.errores import registrar_errores
from app.pagos.modelos_db import IndemnizacionDB
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.correlacion import MiddlewareCorrelacion
from tests.dobles import PasarelaFalsa, evento_indemnizacion
from tests.test_conciliacion import _con_datos

SECRETO = "secreto-de-prueba-hu31-de-32-bytes-o-mas"
EMISOR = "solventa-identidad"
RUTA = "/v1/pagos/conciliacion/desembolsos"


def token(rol="OPERADOR", *, secreto=SECRETO, vence_en_s=1800, sub="op-1"):
    ahora = datetime.now(UTC)
    claims = {"sub": sub, "rol": rol, "nombre": "Olga Operadora", "iss": EMISOR, "iat": ahora, "exp": ahora + timedelta(seconds=vence_en_s)}
    return {"Authorization": f"Bearer {jwt.encode(claims, secreto, algorithm='HS256')}"}


@pytest.fixture
def pasarela():
    return PasarelaFalsa()


@pytest.fixture
async def cliente(armar_indemnizaciones, fabrica, reloj, pasarela):
    pagos = armar_indemnizaciones(pasarela)
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.add_middleware(MiddlewareCorrelacion)
    app.state.servicio_conciliacion = ServicioConciliacion(fabrica=fabrica, pasarela=pasarela, timeout_pasarela_s=0.2, reloj=reloj)
    app.state.verificador_gestion = VerificadorJwtHs256(SECRETO, EMISOR)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.pagos, c.fabrica = pagos, fabrica
        yield c


async def _desembolso(cliente, i=1, **cambios):
    with cliente.fabrica() as s, s.begin():
        await cliente.pagos.manejar_evento(evento_indemnizacion(_con_datos(i, **cambios)), s)
    await cliente.pagos.procesar_pendientes()
    with cliente.fabrica() as s:
        return s.execute(select(IndemnizacionDB).where(IndemnizacionDB.evento_id == f"evt-{i}")).scalar_one().id


# --- criterio 9: acceso ---------------------------------------------------------------------------------------------

RUTAS = [
    ("get", RUTA), ("get", f"{RUTA}/resumen"), ("get", f"{RUTA}/exportar"), ("get", f"{RUTA}/x"),
    ("post", f"{RUTA}/x/conciliar"), ("post", f"{RUTA}/x/escalar"),
]


@pytest.mark.parametrize("metodo, ruta", RUTAS)
async def test_sin_token_o_con_uno_invalido_o_vencido_es_401(cliente, metodo, ruta):
    cuerpo = {"json": {"nota": "x"}} if ruta.endswith("escalar") else {}
    for cabeceras in ({}, token(secreto="otro-secreto-tambien-de-32-bytes-o-mas"), token(vence_en_s=-10), {"Authorization": "Basic abc"}):
        r = await getattr(cliente, metodo)(ruta, headers=cabeceras, **cuerpo)
        assert (r.status_code, r.json()["error"]["code"]) == (401, "NO_AUTENTICADO")


@pytest.mark.parametrize("metodo, ruta", RUTAS)
async def test_un_asesor_no_entra_a_la_conciliacion(cliente, metodo, ruta):
    cuerpo = {"json": {"nota": "x"}} if ruta.endswith("escalar") else {}
    r = await getattr(cliente, metodo)(ruta, headers=token("ASESOR"), **cuerpo)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "SIN_PERMISO")


async def test_el_error_trae_trace_id(cliente):
    r = await cliente.get(f"{RUTA}/no-existe", headers=token())
    assert r.status_code == 404 and r.json()["error"]["code"] == "DESEMBOLSO_NO_ENCONTRADO" and r.json()["error"]["trace_id"]


# --- criterios 1, 3, 4 y 8: lista, detalle, filtros y resumen -----------------------------------------------------------


async def test_la_lista_trae_los_campos_y_nunca_el_cliente_entero_ni_la_cuenta(cliente):
    await _desembolso(cliente, cuenta_destino="cta_secreta_de_prueba")
    r = await cliente.get(RUTA, headers=token())
    assert r.status_code == 200 and r.json()["total"] == 1
    [d] = r.json()["items"]
    assert (d["tipo_evento"], d["ubicacion"], d["estado"], d["estado_conciliacion"], d["idempotency_key"]) == (
        "sismo", "bogota", "PAGADA", "PENDIENTE", "evt-1:pol-1",
    )
    assert d["cliente"] == "cli***01" and d["numero_poliza"] == "POL-00001" and d["validacion"]["resultado"] == "CORRESPONDE_PAGO"
    assert "cli-00000001" not in r.text and "cta_secreta" not in r.text


async def test_los_filtros_y_el_tamano_maximo_de_pagina(cliente):
    await _desembolso(cliente, 1)
    await _desembolso(cliente, 2, evento={"tipo": "inundacion", "ocurrido_en": "2026-11-11T00:00:00+00:00", "ubicacion": "cali", "magnitud": {}})
    cab = token()
    assert [d["tipo_evento"] for d in (await cliente.get(RUTA, params={"tipo_evento": "inundacion"}, headers=cab)).json()["items"]] == ["inundacion"]
    assert (await cliente.get(RUTA, params={"estado_conciliacion": "CONCILIADO"}, headers=cab)).json()["total"] == 0
    assert (await cliente.get(RUTA, params={"estado_conciliacion": "RARO"}, headers=cab)).status_code == 422
    assert (await cliente.get(RUTA, params={"tamano": 101}, headers=cab)).status_code == 422
    assert (await cliente.get(RUTA, params={"desde": "2100-01-01T00:00:00Z"}, headers=cab)).json()["total"] == 0


async def test_el_detalle_trae_los_intentos_y_el_resumen_prueba_que_no_hay_duplicados(cliente):
    id_ = await _desembolso(cliente)
    d = (await cliente.get(f"{RUTA}/{id_}", headers=token())).json()
    assert [(i["numero"], i["resultado"]) for i in d["intentos_de_pago"]] == [(1, "APROBADO")]
    r = (await cliente.get(f"{RUTA}/resumen", headers=token())).json()
    assert (r["desembolsos"], r["claves_distintas"], r["claves_con_mas_de_un_pago"], r["pagados"]) == (1, 1, 0, 1)
    assert Decimal(r["monto_pagado"]) == Decimal("5000000.00")


# --- criterios 5 y 6: conciliar y escalar --------------------------------------------------------------------------


async def test_conciliar_deja_quien_lo_hizo_y_conciliar_dos_veces_no_falla(cliente):
    id_ = await _desembolso(cliente)
    r = await cliente.post(f"{RUTA}/{id_}/conciliar", headers=token())
    assert r.status_code == 200 and (r.json()["estado_conciliacion"], r.json()["conciliado_por"]) == ("CONCILIADO", "Olga Operadora")
    assert (await cliente.post(f"{RUTA}/{id_}/conciliar", headers=token())).json()["estado_conciliacion"] == "CONCILIADO"


async def test_con_diferencia_se_escala_con_una_nota(cliente, pasarela):
    id_ = await _desembolso(cliente)
    pasarela.montos_pagados["evt-1:pol-1"] = Decimal("1.00")
    r = await cliente.post(f"{RUTA}/{id_}/conciliar", headers=token())
    assert r.json()["estado_conciliacion"] == "CON_DIFERENCIA" and r.json()["diferencia"]["monto_pasarela"] == "1.00"

    assert (await cliente.post(f"{RUTA}/{id_}/escalar", json={}, headers=token())).status_code == 422
    assert (await cliente.post(f"{RUTA}/{id_}/escalar", json={"nota": ""}, headers=token())).status_code == 422
    r = await cliente.post(f"{RUTA}/{id_}/escalar", json={"nota": "El extracto dice 1,00"}, headers=token())
    assert r.status_code == 200 and (r.json()["nota_escalamiento"], r.json()["escalada_por"]) == ("El extracto dice 1,00", "op-1")


async def test_errores_de_negocio_de_conciliar_y_escalar(cliente, pasarela):
    id_ = await _desembolso(cliente)
    r = await cliente.post(f"{RUTA}/{id_}/escalar", json={"nota": "x"}, headers=token())
    assert (r.status_code, r.json()["error"]["code"]) == (409, "DESEMBOLSO_SIN_DIFERENCIA")
    pasarela.montos_pagados.clear()
    r = await cliente.post(f"{RUTA}/{id_}/conciliar", headers=token())
    assert (r.status_code, r.json()["error"]["code"]) == (409, "EXTRACTO_SIN_PAGO")
    assert (await cliente.post(f"{RUTA}/no-existe/conciliar", headers=token())).status_code == 404


async def test_un_desembolso_fallido_no_se_concilia(cliente, pasarela):
    pasarela.guion.append("rechazar")
    id_ = await _desembolso(cliente)
    r = await cliente.post(f"{RUTA}/{id_}/conciliar", headers=token())
    assert (r.status_code, r.json()["error"]["code"]) == (409, "DESEMBOLSO_NO_CONCILIABLE")


# --- criterio 7: exportar ----------------------------------------------------------------------------------------------


async def test_exporta_csv_y_pdf_con_los_filtros(cliente):
    await _desembolso(cliente, 1)
    await _desembolso(cliente, 2, evento={"tipo": "inundacion", "ocurrido_en": "2026-11-11T00:00:00+00:00", "ubicacion": "cali", "magnitud": {}})
    r = await cliente.get(f"{RUTA}/exportar", params={"formato": "csv", "tipo_evento": "inundacion"}, headers=token())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and "evt-2" in r.text and "evt-1" not in r.text
    r = await cliente.get(f"{RUTA}/exportar", params={"formato": "pdf"}, headers=token())
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert (await cliente.get(f"{RUTA}/exportar", params={"formato": "xls"}, headers=token())).status_code == 422
