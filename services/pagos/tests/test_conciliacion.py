"""Registro y conciliación de desembolsos (HU31): datos copiados del comando, estados de conciliación, filtros y exportación.

Usa la pasarela falsa (el puerto): lo que dice su extracto es lo que se compara con el desembolso registrado.
"""

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.pagos.acceso import Gestor
from app.pagos.conciliacion import (
    Filtros,
    ServicioConciliacion,
    a_csv,
    a_pdf,
    enmascarar,
)
from app.pagos.errores import (
    DesembolsoNoConciliable,
    DesembolsoNoEncontrado,
    DesembolsoSinDiferencia,
    ExtractoSinPago,
    SolicitudInvalida,
)
from app.pagos.modelos_db import IndemnizacionDB
from sqlalchemy import select

from tests.dobles import PasarelaFalsa, comando_indemnizacion, evento_indemnizacion

OPERADOR = Gestor("op-1", "Olga Operadora", "OPERADOR")
OTRO = Gestor("op-2", "Omar Operador", "OPERADOR")


def _con_datos(i=1, **cambios):
    """El comando de Siniestros tal como lo publica HU31: con el evento, la póliza y la validación."""
    datos = {
        "cliente_id": f"cli-{i:08d}",
        "evento": {"tipo": "sismo", "ocurrido_en": "2026-11-10T08:30:00+00:00", "ubicacion": "bogota", "magnitud": {"magnitud": 6.4}},
        "poliza": {"producto": "parametrico_clima", "zona": "bogota", "numero": f"POL-{i:05d}", "monto_asegurado": 5_000_000},
        "validacion": {"resultado": "CORRESPONDE_PAGO", "motivo": "condicion_cumplida", "regla": "sismo", "version_reglas": "v1"},
    }
    return comando_indemnizacion(f"evt-{i}", f"pol-{i}", **{**datos, **cambios})


@pytest.fixture
def servicio_pagos(armar_indemnizaciones):
    return armar_indemnizaciones


async def _pagar(servicio, fabrica, i=1, **cambios):
    with fabrica() as s, s.begin():
        await servicio.manejar_evento(evento_indemnizacion(_con_datos(i, **cambios)), s)
    await servicio.procesar_pendientes()
    with fabrica() as s:
        return s.execute(select(IndemnizacionDB).where(IndemnizacionDB.evento_id == f"evt-{i}")).scalar_one().id


@pytest.fixture
def conciliar(fabrica, reloj):
    def _armar(pasarela):
        return ServicioConciliacion(fabrica=fabrica, pasarela=pasarela, timeout_pasarela_s=0.2, reloj=reloj)

    return _armar


# --- criterio 1: registro completo ---------------------------------------------------------------------------------


async def test_cada_pago_guarda_el_evento_la_poliza_la_validacion_y_el_pago(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    d, historia = conciliar(pasarela).obtener(id_)

    assert (d.tipo_evento, d.ubicacion, d.magnitud) == ("sismo", "bogota", {"magnitud": 6.4})
    assert d.evento_ocurrido_en == datetime(2026, 11, 10, 8, 30, tzinfo=UTC)
    assert (d.poliza_id, d.producto, d.numero_poliza, d.monto_asegurado) == ("pol-1", "parametrico_clima", "POL-00001", 5_000_000)
    assert d.validacion["resultado"] == "CORRESPONDE_PAGO" and d.validacion["version_reglas"] == "v1"
    assert (d.estado, d.monto, d.moneda, d.referencia_pasarela) == ("PAGADA", Decimal("5000000.00"), "COP", "pg-1")
    assert d.idempotency_key == "evt-1:pol-1" and d.fecha_pago is not None
    assert [(h["numero"], h["resultado"]) for h in historia] == [(1, "APROBADO")]


async def test_un_comando_anterior_a_hu31_se_paga_igual_y_queda_sin_esos_datos(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    servicio = servicio_pagos(pasarela)
    with fabrica() as s, s.begin():
        await servicio.manejar_evento(evento_indemnizacion(), s)
    await servicio.procesar_pendientes()
    [d], total = conciliar(pasarela).listar(Filtros())
    assert total == 1 and d.estado == "PAGADA" and d.tipo_evento is None and d.magnitud is None and d.validacion is None


# --- criterio 2 y 5: estado de conciliación ------------------------------------------------------------------------


async def test_un_desembolso_nuevo_esta_pendiente_de_conciliar(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    assert conciliar(pasarela).obtener(id_)[0].estado_conciliacion == "PENDIENTE"


async def test_conciliar_con_el_extracto_correcto_deja_quien_y_cuando(servicio_pagos, fabrica, conciliar, reloj):
    pasarela = PasarelaFalsa()
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    d = await conciliar(pasarela).conciliar(id_, OPERADOR)
    assert d.estado_conciliacion == "CONCILIADO" and d.diferencia is None
    assert (d.conciliado_por, d.conciliado_en) == ("Olga Operadora", reloj())


async def test_si_el_monto_del_extracto_no_coincide_queda_con_diferencia_y_la_detalla(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    pasarela.montos_pagados["evt-1:pol-1"] = Decimal("4999000.00")  # el extracto dice otra cosa
    d = await conciliar(pasarela).conciliar(id_, OPERADOR)
    assert d.estado_conciliacion == "CON_DIFERENCIA"
    assert d.diferencia["monto_registrado"] == "5000000.00" and d.diferencia["monto_pasarela"] == "4999000.00"


async def test_si_la_pasarela_no_tiene_el_pago_no_cambia_nada(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    pasarela.montos_pagados.clear()
    with pytest.raises(ExtractoSinPago):
        await conciliar(pasarela).conciliar(id_, OPERADOR)
    assert conciliar(pasarela).obtener(id_)[0].estado_conciliacion == "PENDIENTE"


async def test_solo_se_concilian_los_desembolsos_pagados(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa("rechazar")
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    with pytest.raises(DesembolsoNoConciliable):
        await conciliar(pasarela).conciliar(id_, OPERADOR)
    with pytest.raises(DesembolsoNoEncontrado):
        await conciliar(pasarela).conciliar("no-existe", OPERADOR)


async def test_una_diferencia_corregida_se_puede_volver_a_conciliar(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    pasarela.montos_pagados["evt-1:pol-1"] = Decimal("1.00")
    assert (await conciliar(pasarela).conciliar(id_, OPERADOR)).estado_conciliacion == "CON_DIFERENCIA"
    pasarela.montos_pagados["evt-1:pol-1"] = Decimal("5000000.00")
    d = await conciliar(pasarela).conciliar(id_, OTRO)
    assert (d.estado_conciliacion, d.conciliado_por, d.diferencia) == ("CONCILIADO", "Omar Operador", None)


# --- criterio 6: escalar -----------------------------------------------------------------------------------------


async def test_escalar_una_diferencia_exige_nota_y_deja_quien_y_cuando(servicio_pagos, fabrica, conciliar, reloj):
    pasarela = PasarelaFalsa()
    servicio = conciliar(pasarela)
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    pasarela.montos_pagados["evt-1:pol-1"] = Decimal("1.00")
    await servicio.conciliar(id_, OPERADOR)

    for nota in ("", "   ", "x" * 501):
        with pytest.raises(SolicitudInvalida):
            servicio.escalar(id_, OPERADOR, nota)
    d = servicio.escalar(id_, OTRO, "  El extracto dice 1,00 COP  ")
    assert (d.nota_escalamiento, d.escalada_por, d.escalada_en) == ("El extracto dice 1,00 COP", "op-2", reloj())
    assert d.estado_conciliacion == "CON_DIFERENCIA"


async def test_solo_se_escala_lo_que_tiene_diferencia(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    servicio = conciliar(pasarela)
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    with pytest.raises(DesembolsoSinDiferencia):
        servicio.escalar(id_, OPERADOR, "nota")  # pendiente
    await servicio.conciliar(id_, OPERADOR)
    with pytest.raises(DesembolsoSinDiferencia):
        servicio.escalar(id_, OPERADOR, "nota")  # conciliado
    with pytest.raises(DesembolsoNoEncontrado):
        servicio.escalar("no-existe", OPERADOR, "nota")


async def test_conciliar_de_nuevo_sin_diferencia_borra_el_escalamiento(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    servicio = conciliar(pasarela)
    id_ = await _pagar(servicio_pagos(pasarela), fabrica)
    pasarela.montos_pagados["evt-1:pol-1"] = Decimal("1.00")
    await servicio.conciliar(id_, OPERADOR)
    servicio.escalar(id_, OPERADOR, "revisar")
    pasarela.montos_pagados["evt-1:pol-1"] = Decimal("5000000.00")
    d = await servicio.conciliar(id_, OPERADOR)
    assert (d.estado_conciliacion, d.nota_escalamiento, d.escalada_en) == ("CONCILIADO", None, None)


# --- criterio 4: filtros -----------------------------------------------------------------------------------------


async def _tres(servicio_pagos, fabrica, conciliar, reloj):
    pasarela = PasarelaFalsa()
    pagos = servicio_pagos(pasarela)
    await _pagar(pagos, fabrica, 1)
    reloj.avanzar(86_400)
    await _pagar(pagos, fabrica, 2, evento={"tipo": "inundacion", "ocurrido_en": "2026-11-11T00:00:00+00:00", "ubicacion": "cali", "magnitud": {"nivel_cm": 160}})
    reloj.avanzar(86_400)
    await _pagar(pagos, fabrica, 3)
    servicio = conciliar(pasarela)
    with fabrica() as s:
        ids = {i.evento_id: i.id for i in s.execute(select(IndemnizacionDB)).scalars()}
    await servicio.conciliar(ids["evt-1"], OPERADOR)
    return servicio


async def test_los_filtros_por_tipo_de_evento_estado_y_rango_de_fechas(servicio_pagos, fabrica, conciliar, reloj):
    servicio = await _tres(servicio_pagos, fabrica, conciliar, reloj)
    ids = lambda **f: sorted(d.evento_id for d in servicio.listar(Filtros(**f))[0])
    assert ids() == ["evt-1", "evt-2", "evt-3"]
    assert ids(tipo_evento="inundacion") == ["evt-2"]
    assert ids(estado_conciliacion="CONCILIADO") == ["evt-1"]
    assert ids(estado_conciliacion="PENDIENTE") == ["evt-2", "evt-3"]
    assert ids(estado="PAGADA", tipo_evento="sismo", estado_conciliacion="PENDIENTE") == ["evt-3"]
    hoy = reloj()
    assert ids(desde=hoy - timedelta(days=1, hours=1), hasta=hoy - timedelta(hours=23)) == ["evt-2"]
    assert ids(desde=hoy + timedelta(days=1)) == []


async def test_la_pagina_devuelve_el_total_y_los_mas_recientes_primero(servicio_pagos, fabrica, conciliar, reloj):
    servicio = await _tres(servicio_pagos, fabrica, conciliar, reloj)
    primera, total = servicio.listar(Filtros(), pagina=1, tamano=2)
    segunda, _ = servicio.listar(Filtros(), pagina=2, tamano=2)
    assert total == 3 and [d.evento_id for d in primera] == ["evt-3", "evt-2"] and [d.evento_id for d in segunda] == ["evt-1"]


# --- criterio 8: sin duplicados visibles ----------------------------------------------------------------------------


async def test_el_resumen_comprueba_que_ninguna_clave_tiene_mas_de_un_pago(servicio_pagos, fabrica, conciliar, reloj):
    servicio = await _tres(servicio_pagos, fabrica, conciliar, reloj)
    pagos = servicio_pagos(PasarelaFalsa())
    with fabrica() as s, s.begin():
        await pagos.manejar_evento(evento_indemnizacion(_con_datos(1)), s)  # la misma clave otra vez
    r = servicio.resumen(Filtros())
    assert (r.desembolsos, r.claves_distintas, r.claves_con_mas_de_un_pago, r.pagados) == (3, 3, 0, 3)
    assert r.monto_pagado == Decimal("15000000.00")
    assert r.por_estado_conciliacion == {"PENDIENTE": 2, "CONCILIADO": 1, "CON_DIFERENCIA": 0}
    assert [d.reentregas for d in servicio.listar(Filtros(tipo_evento="sismo"))[0] if d.evento_id == "evt-1"] == [1]


# --- criterio 9: datos del cliente enmascarados -------------------------------------------------------------------


def test_el_cliente_se_enmascara_y_un_dato_corto_se_oculta_entero():
    assert enmascarar("cli-12345678") == "cli***78" and enmascarar("abc") == "***" and enmascarar(None) is None


async def test_la_vista_nunca_muestra_el_cliente_entero_ni_la_cuenta_de_destino(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    await _pagar(servicio_pagos(pasarela), fabrica, cuenta_destino="cta_secreta_de_prueba")
    [d], _ = conciliar(pasarela).listar(Filtros())
    assert d.cliente == "cli***01" and "cli-00000001" not in repr(d) and "cta_secreta" not in repr(d)


# --- criterio 7: exportar ------------------------------------------------------------------------------------------


async def test_el_csv_tiene_una_fila_por_desembolso_con_los_campos_y_sin_datos_sensibles(servicio_pagos, fabrica, conciliar, reloj):
    servicio = await _tres(servicio_pagos, fabrica, conciliar, reloj)
    datos = a_csv(servicio.filas_para_exportar(Filtros(tipo_evento="sismo")))
    assert datos.startswith("﻿".encode())
    filas = list(csv.DictReader(io.StringIO(datos.decode("utf-8-sig"))))
    assert sorted(f["Evento"] for f in filas) == ["evt-1", "evt-3"]
    f = next(f for f in filas if f["Evento"] == "evt-1")
    assert (f["Tipo de evento"], f["Monto"], f["Conciliación"], f["Conciliado por"]) == ("sismo", "5000000.00", "CONCILIADO", "Olga Operadora")
    assert f["Cliente"] == "cli***01" and "cta_" not in datos.decode()


async def test_el_csv_neutraliza_las_formulas_para_las_hojas_de_calculo(servicio_pagos, fabrica, conciliar):
    pasarela = PasarelaFalsa()
    await _pagar(servicio_pagos(pasarela), fabrica, evento={"tipo": "sismo", "ubicacion": "=HYPERLINK(\"http://x\")", "magnitud": {}})
    texto = a_csv(conciliar(pasarela).filas_para_exportar(Filtros())).decode("utf-8-sig")
    fila = next(iter(csv.DictReader(io.StringIO(texto))))
    assert fila["Ubicación"].startswith("'=")


async def test_el_pdf_se_genera_con_y_sin_filas(servicio_pagos, fabrica, conciliar, reloj):
    servicio = await _tres(servicio_pagos, fabrica, conciliar, reloj)
    con = a_pdf(servicio.filas_para_exportar(Filtros()), servicio.resumen(Filtros()), reloj())
    sin = a_pdf([], servicio.resumen(Filtros(tipo_evento="nada")), reloj())
    assert con.startswith(b"%PDF") and sin.startswith(b"%PDF") and len(con) > len(sin)
