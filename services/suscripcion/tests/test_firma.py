"""Firma electrónica de la póliza (HU21): evidencia, hash, OTP, idempotencia, vencimiento y compensación."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from app.eventos import Evento
from app.suscripcion import repositorio as repo
from app.suscripcion.cobros import manejar_resultado_cobro
from app.suscripcion.config import TOPIC_CICLO_VIDA, TOPIC_COMANDOS
from app.suscripcion.errores import (
    AutenticacionInvalida,
    DocumentoAlterado,
    FirmaFallida,
    FirmaNoDisponible,
    FirmaNoEncontrada,
    FirmaVencida,
    SolicitudInvalida,
    SuscripcionNoEncontrada,
)
from app.suscripcion.firma import OrigenFirma, ServicioFirma, clave_copia_firmada
from app.suscripcion.modelos_db import FirmaDB, PolizaDB, SuscripcionDB
from app.suscripcion.proveedor_firma import ProveedorFirmaSimulado
from app.suscripcion.reglas import cargar_reglas
from pypdf import PdfReader
from sqlalchemy import select

from tests.dobles import IdentidadFalsa
from tests.test_polizas import (
    AlmacenMemoria,
    Reloj,
    _cobrada,
    _generador,
    _poliza,
    _texto,
)
from tests.test_servicio import _salidas

COBRO = datetime(2026, 10, 25, 12, 0, 5, tzinfo=UTC)  # lo que informa `_resultado` en test_cobros
ORIGEN = OrigenFirma(canal="MOVIL", ip="190.1.2.3", dispositivo="Pixel 8 / Android 15")
OTP = "123456"


def _servicio_firma(fabrica, almacen, reloj, **cambios):
    return ServicioFirma(
        fabrica=fabrica,
        identidad=cambios.pop("identidad", IdentidadFalsa()),
        almacen=almacen,
        proveedor=cambios.pop("proveedor", ProveedorFirmaSimulado(OTP)),
        reglas=cambios.pop("reglas", cargar_reglas()),
        reloj=reloj,
    )


async def _lista(armar, fabrica, **cambios):
    """Una saga con el cobro confirmado y el PDF generado, lista para firmar una hora después del cobro."""
    _, susc = await _cobrada(armar, fabrica)
    almacen = AlmacenMemoria()
    await _generador(fabrica, almacen).procesar_pendientes()
    reloj = Reloj(COBRO + timedelta(hours=1))
    firma = _servicio_firma(fabrica, almacen, reloj, **cambios)
    return firma, susc, almacen, reloj


async def _firmar(firma, susc, poliza=None, *, codigo=OTP, revisado=True, origen=ORIGEN, huella=None, token="token-1"):
    documento = await firma.documento_para_firmar(susc.id, token)
    return await firma.firmar(
        susc.id,
        token,
        documento_sha256=huella or documento.documento_sha256,
        documento_revisado=revisado,
        codigo=codigo,
        origen=origen,
    )


def _saga(fabrica, susc_id) -> SuscripcionDB:
    with fabrica() as s:
        return repo.obtener(s, susc_id)


def _firmas(fabrica) -> list[FirmaDB]:
    with fabrica() as s:
        return list(s.execute(select(FirmaDB)).scalars())


def _comandos(fabrica):
    return [x for x in _salidas(fabrica) if x.topic == TOPIC_COMANDOS]


# --- revisar antes de firmar (criterio 1) -----------------------------------------------------------------------------

async def test_el_documento_por_firmar_trae_el_hash_del_pdf_y_el_vencimiento(armar, fabrica):
    firma, susc, almacen, _ = await _lista(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)

    documento = await firma.documento_para_firmar(susc.id, "token-1")

    pdf = almacen.objetos[poliza.documento_s3_key][0]
    assert documento.documento_sha256 == hashlib.sha256(pdf).hexdigest() == poliza.documento_sha256
    assert (documento.numero_poliza, documento.estado, documento.metodo_autenticacion) == (poliza.numero_poliza, "PENDIENTE", "OTP")
    assert documento.vence_en == COBRO + timedelta(hours=24)
    assert await firma.contenido_documento(susc.id, "token-1") == pdf


async def test_sin_el_documento_generado_todavia_no_se_puede_firmar(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    firma = _servicio_firma(fabrica, AlmacenMemoria(), Reloj(COBRO + timedelta(hours=1)))

    from app.suscripcion.errores import PolizaNoLista

    with pytest.raises(PolizaNoLista):
        await firma.documento_para_firmar(susc.id, "token-1")


async def test_hay_que_confirmar_que_se_reviso_el_documento(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    with pytest.raises(SolicitudInvalida) as error:
        await _firmar(firma, susc, revisado=False)

    assert error.value.detalles[0]["campo"] == "documento_revisado"
    assert _firmas(fabrica) == []


# --- firmar y evidencia (criterios 2 y 3) -----------------------------------------------------------------------------

async def test_firmar_guarda_la_evidencia_y_la_saga_pasa_a_firmada_y_luego_emite(armar, fabrica, monkeypatch):
    firma, susc, *_ = await _lista(armar, fabrica)
    monkeypatch.setattr(firma, "_emitir", lambda _id: False)  # se observa el estado intermedio

    registro, creada = await _firmar(firma, susc)

    saga = _saga(fabrica, susc.id)
    assert creada and (saga.estado_saga, saga.paso_actual) == ("FIRMADA", "EMISION")
    poliza = _poliza(fabrica, susc.id)
    assert registro.documento_sha256 == poliza.documento_sha256
    assert (registro.firmante_id, registro.canal, registro.ip, registro.dispositivo) == ("cli-1", "MOVIL", "190.1.2.3", "Pixel 8 / Android 15")
    assert (registro.metodo_autenticacion, registro.proveedor) == ("OTP", "simulado")
    assert repo.utc(registro.firmada_en) == COBRO + timedelta(hours=1)
    assert registro.referencia_proveedor.startswith("simfirma_")

    monkeypatch.undo()
    assert await firma.emitir_firmadas() == 1
    assert _saga(fabrica, susc.id).estado_saga == "EMITIDA"
    assert _poliza(fabrica, susc.id).estado == "VIGENTE"


async def test_firmar_emite_la_poliza_en_la_misma_llamada_y_publica_un_solo_evento(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    await _firmar(firma, susc)

    assert (_saga(fabrica, susc.id).estado_saga, _poliza(fabrica, susc.id).estado) == ("EMITIDA", "VIGENTE")
    emitidos = [x for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA]
    assert [x.payload["event_type"] for x in emitidos] == ["poliza_emitida"]


async def test_la_evidencia_no_guarda_nombre_ni_documento_del_cliente(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    registro, _ = await _firmar(firma, susc)

    texto = " ".join(str(getattr(registro, c.name)) for c in FirmaDB.__table__.columns)
    assert "Ana" not in texto and "Pérez" not in texto and "******300" not in texto


async def test_la_evidencia_es_inmutable(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)
    await _firmar(firma, susc)

    with pytest.raises(RuntimeError, match="inmutable"), fabrica() as s, s.begin():
        fila = s.execute(select(FirmaDB)).scalar_one()
        fila.canal = "WEB"
    with pytest.raises(RuntimeError, match="inmutable"), fabrica() as s, s.begin():
        s.delete(s.execute(select(FirmaDB)).scalar_one())

    assert _firmas(fabrica)[0].canal == "MOVIL"


# --- documento firmado (criterio 4) -----------------------------------------------------------------------------------

async def test_la_copia_firmada_es_el_original_mas_la_constancia_y_queda_en_el_almacen(armar, fabrica):
    firma, susc, almacen, _ = await _lista(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)
    original = almacen.objetos[poliza.documento_s3_key][0]

    registro, _ = await _firmar(firma, susc)

    clave = clave_copia_firmada(susc.id, poliza.numero_poliza)
    copia, tipo = almacen.objetos[clave]
    assert registro.copia_firmada_s3_key == clave and tipo == "application/pdf"
    paginas_original = len(PdfReader(BytesIO(original)).pages)
    assert len(PdfReader(BytesIO(copia)).pages) == paginas_original + 1
    assert almacen.objetos[poliza.documento_s3_key][0] == original  # el original no se toca
    texto = _texto(copia)
    assert "Constancia de firma electrónica" in texto and poliza.documento_sha256 in texto.replace(" ", "")
    assert "190.1.2.3" in texto
    assert "no tiene validez legal certificada" in texto
    descargada, numero = await firma.copia_firmada(susc.id, "token-1")
    assert descargada == copia and numero == poliza.numero_poliza


async def test_sin_firma_no_hay_copia_firmada(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    with pytest.raises(FirmaNoEncontrada):
        await firma.copia_firmada(susc.id, "token-1")


# --- el documento no cambia (criterio 5) -------------------------------------------------------------------------------

async def test_un_hash_distinto_al_del_documento_se_rechaza_sin_gastar_un_intento(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    with pytest.raises(DocumentoAlterado):
        await _firmar(firma, susc, huella="0" * 64)

    assert _firmas(fabrica) == [] and _saga(fabrica, susc.id).firma_intentos_fallidos == 0


async def test_si_el_pdf_guardado_cambio_la_firma_se_rechaza(armar, fabrica):
    firma, susc, almacen, _ = await _lista(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)
    documento = await firma.documento_para_firmar(susc.id, "token-1")
    almacen.objetos[poliza.documento_s3_key] = (b"%PDF-alterado", "application/pdf")

    with pytest.raises(DocumentoAlterado):
        await firma.firmar(
            susc.id, "token-1", documento_sha256=documento.documento_sha256, documento_revisado=True, codigo=OTP, origen=ORIGEN
        )

    assert _firmas(fabrica) == []


async def test_una_poliza_anterior_a_hu21_calcula_su_hash_la_primera_vez(armar, fabrica):
    firma, susc, almacen, _ = await _lista(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)
    with fabrica() as s, s.begin():
        s.get(PolizaDB, poliza.id).documento_sha256 = None

    documento = await firma.documento_para_firmar(susc.id, "token-1")

    assert documento.documento_sha256 == hashlib.sha256(almacen.objetos[poliza.documento_s3_key][0]).hexdigest()
    assert _poliza(fabrica, susc.id).documento_sha256 == documento.documento_sha256


# --- autenticación reforzada ---------------------------------------------------------------------------------------------

async def test_un_codigo_erroneo_no_firma_y_avisa_cuantos_intentos_quedan(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    with pytest.raises(AutenticacionInvalida) as error:
        await _firmar(firma, susc, codigo="000000")

    assert "intentos restantes: 2" in error.value.detalles[0]["motivo"]
    assert _firmas(fabrica) == [] and _saga(fabrica, susc.id).firma_intentos_fallidos == 1
    assert _saga(fabrica, susc.id).compensacion_estado is None  # todavía le quedan intentos
    assert [x for x in _comandos(fabrica) if x.payload["event_type"] == "reembolso_cobro_solicitado"] == []


async def test_agotados_los_codigos_la_firma_falla_y_se_pide_el_reembolso(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)
    for _ in range(2):
        with pytest.raises(AutenticacionInvalida):
            await _firmar(firma, susc, codigo="000000")
    with pytest.raises(FirmaFallida):
        await _firmar(firma, susc, codigo="000000")

    saga = _saga(fabrica, susc.id)
    assert (saga.compensacion_estado, saga.compensacion_motivo, saga.estado_saga) == ("SOLICITADA", "FIRMA_FALLIDA", "COBRO_REALIZADO")
    (reembolso,) = [x for x in _comandos(fabrica) if x.payload["event_type"] == "reembolso_cobro_solicitado"]
    assert reembolso.payload["payload"]["idempotency_key"] == f"reembolso_prima:{susc.id}"
    # Aun con el código correcto, ya no se puede firmar.
    with pytest.raises(FirmaFallida):
        await _firmar(firma, susc)
    assert _firmas(fabrica) == []


async def test_el_codigo_no_se_escribe_en_los_logs(armar, fabrica, caplog):
    firma, susc, *_ = await _lista(armar, fabrica)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AutenticacionInvalida):
            await _firmar(firma, susc, codigo="765432")
        await _firmar(firma, susc)

    assert "765432" not in caplog.text and OTP not in caplog.text


# --- idempotencia (criterio 8) -------------------------------------------------------------------------------------------

async def test_firmar_dos_veces_devuelve_la_misma_firma_sin_otra_emision(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)
    primera, creada1 = await _firmar(firma, susc)

    segunda, creada2 = await _firmar(firma, susc, codigo="000000")  # ni siquiera pide de nuevo el código

    assert (creada1, creada2) == (True, False) and primera.id == segunda.id
    assert len(_firmas(fabrica)) == 1
    assert len([x for x in _salidas(fabrica) if x.topic == TOPIC_CICLO_VIDA]) == 1


async def test_la_replica_que_llega_tarde_recibe_la_firma_ya_registrada(armar, fabrica):
    """Con PostgreSQL el bloqueo de la fila y la restricción única hacen que dos firmas a la vez dejen una sola."""
    firma, susc, *_ = await _lista(armar, fabrica)
    poliza = _poliza(fabrica, susc.id)
    huella = poliza.documento_sha256 or (await firma.documento_para_firmar(susc.id, "token-1")).documento_sha256
    poliza = _poliza(fabrica, susc.id)
    argumentos = (susc.id, poliza, "cli-1", huella, COBRO, ORIGEN, "simfirma_x", clave_copia_firmada(susc.id, poliza.numero_poliza))

    primera, creada1 = firma._registrar(*argumentos)
    segunda, creada2 = firma._registrar(*argumentos)

    assert (creada1, creada2) == (True, False) and primera.id == segunda.id
    assert len(_firmas(fabrica)) == 1


async def test_repetir_la_firma_con_otro_hash_se_rechaza(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)
    await _firmar(firma, susc)

    with pytest.raises(DocumentoAlterado):
        await _firmar(firma, susc, huella="1" * 64)


# --- firma vencida y compensación (criterio 6) -----------------------------------------------------------------------------

async def test_firmar_despues_del_vencimiento_se_rechaza_y_pide_el_reembolso_una_vez(armar, fabrica):
    firma, susc, _, reloj = await _lista(armar, fabrica)
    documento = await firma.documento_para_firmar(susc.id, "token-1")
    reloj.ahora = COBRO + timedelta(hours=24, seconds=1)

    for _ in range(2):
        with pytest.raises(FirmaVencida):
            await firma.firmar(
                susc.id, "token-1", documento_sha256=documento.documento_sha256, documento_revisado=True, codigo=OTP, origen=ORIGEN
            )

    saga = _saga(fabrica, susc.id)
    assert (saga.compensacion_estado, saga.compensacion_motivo) == ("SOLICITADA", "FIRMA_VENCIDA")
    reembolsos = [x for x in _comandos(fabrica) if x.payload["event_type"] == "reembolso_cobro_solicitado"]
    assert len(reembolsos) == 1
    datos = reembolsos[0].payload["payload"]
    assert (datos["monto"], datos["moneda"], datos["suscripcion_id"]) == ("175000.00", "COP", susc.id)
    assert _firmas(fabrica) == []


async def test_el_barrido_compensa_las_firmas_vencidas_aunque_nadie_vuelva_a_abrir_la_app(armar, fabrica):
    firma, susc, _, reloj = await _lista(armar, fabrica)
    assert await firma.compensar_vencidas() == 0  # todavía a tiempo

    reloj.ahora = COBRO + timedelta(hours=25)

    assert await firma.compensar_vencidas() == 1
    assert await firma.compensar_vencidas() == 0
    assert _saga(fabrica, susc.id).compensacion_motivo == "FIRMA_VENCIDA"
    assert len([x for x in _comandos(fabrica) if x.payload["event_type"] == "reembolso_cobro_solicitado"]) == 1


async def test_el_vencimiento_sale_de_las_reglas(armar, fabrica):
    _, susc = await _cobrada(armar, fabrica)
    almacen = AlmacenMemoria()
    await _generador(fabrica, almacen).procesar_pendientes()
    reglas = cargar_reglas()
    assert (reglas.firma_vigencia_horas, reglas.firma_max_intentos) == (24, 3)
    assert _saga(fabrica, susc.id).firma_vence_en is not None
    assert repo.utc(_saga(fabrica, susc.id).firma_vence_en) == COBRO + timedelta(hours=24)


async def _reembolsado(fabrica, susc_id):
    evento = Evento(
        event_type="cobro_prima_reembolsado",
        source="ms-pagos",
        correlation_id=susc_id,
        payload={"suscripcion_id": susc_id, "transaccion_id": "tx-r", "estado": "APROBADA", "resuelta_en": "2026-10-26T13:00:00+00:00"},
    )
    with fabrica() as s, s.begin():
        await manejar_resultado_cobro(evento, s)


async def test_cuando_pagos_confirma_el_reembolso_la_saga_queda_compensada_y_libera_la_cotizacion(armar, fabrica):
    firma, susc, _, reloj = await _lista(armar, fabrica)
    reloj.ahora = COBRO + timedelta(hours=25)
    await firma.compensar_vencidas()

    await _reembolsado(fabrica, susc.id)

    saga = _saga(fabrica, susc.id)
    assert (saga.estado_saga, saga.compensacion_estado) == ("COMPENSADA", "COMPLETADA")
    assert _poliza(fabrica, susc.id).estado == "ANULADA"
    nueva, creada = await armar().iniciar(
        token="token-1", clave="k-nueva", cotizacion_id="cot-1", metodo_pago="tok_x", acepta_terminos=True
    )
    assert creada and nueva.id != susc.id


async def test_un_reembolso_que_nadie_pidio_no_cambia_la_saga(armar, fabrica):
    _firma, susc, *_ = await _lista(armar, fabrica)

    await _reembolsado(fabrica, susc.id)

    assert _saga(fabrica, susc.id).estado_saga == "COBRO_REALIZADO"


# --- acceso y estado ------------------------------------------------------------------------------------------------------

async def test_la_firma_de_otro_cliente_responde_como_inexistente(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    with pytest.raises(SuscripcionNoEncontrada):
        await firma.documento_para_firmar(susc.id, "token-2")
    with pytest.raises(SuscripcionNoEncontrada):
        await firma.contenido_documento(susc.id, "token-2")


async def test_sin_el_cobro_realizado_no_hay_paso_de_firma(armar, fabrica):
    servicio = armar()
    from tests.test_servicio import _iniciar

    susc, _ = await _iniciar(servicio)
    await servicio.decidir(susc.id)
    firma = _servicio_firma(fabrica, AlmacenMemoria(), Reloj(COBRO))

    from app.suscripcion.errores import PolizaNoEncontrada

    with pytest.raises((FirmaNoDisponible, PolizaNoEncontrada)):
        await firma.documento_para_firmar(susc.id, "token-1")


async def test_el_canal_debe_ser_movil_o_web(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)

    with pytest.raises(SolicitudInvalida):
        await _firmar(firma, susc, origen=OrigenFirma(canal="FAX", ip=None, dispositivo=None))


async def test_firmar_desde_la_web_y_consultar_desde_el_movil_da_la_misma_firma(armar, fabrica):
    firma, susc, *_ = await _lista(armar, fabrica)
    await _firmar(firma, susc, origen=OrigenFirma(canal="WEB", ip="10.0.0.1", dispositivo="Firefox"))

    vista = await firma.firma_de(susc.id, "token-1")

    assert (vista.canal, vista.firmante_id) == ("WEB", "cli-1")
    assert await firma.estado_saga(susc.id) == "EMITIDA"


async def test_el_proveedor_simulado_compara_el_codigo_y_da_una_referencia_estable():
    proveedor = ProveedorFirmaSimulado("246810")

    assert await proveedor.verificar_desafio("cli-1", "246810")
    assert not await proveedor.verificar_desafio("cli-1", "246811")
    assert await proveedor.registrar_firma("cli-1", "a" * 64) == await proveedor.registrar_firma("cli-1", "a" * 64)
    assert await proveedor.registrar_firma("cli-1", "a" * 64) != await proveedor.registrar_firma("cli-2", "a" * 64)
