"""Push de pago paramétrico (HU30): `indemnizacion_pagada` deja un aviso de prioridad alta en la bandeja del titular."""

import json
from pathlib import Path

import pytest
from app.eventos import Evento
from app.notificaciones.modelos_db import BandejaPushDB, EnvioDB
from app.notificaciones.plantillas import construir_push_indemnizacion, monto_legible
from app.notificaciones.servicio import CAMPOS_PAGO
from sqlalchemy import select

from tests.dobles import CorreoFalso, PushFalso
from tests.test_servicio import _bandeja, _envios, _registrar_dispositivo

EJEMPLO = Path(__file__).parent / "contratos" / "indemnizacion_pagada.json"


def _pago(**cambios) -> Evento:
    cuerpo = json.loads(EJEMPLO.read_text(encoding="utf-8"))
    cuerpo["payload"] = {k: v for k, v in (cuerpo["payload"] | cambios).items() if v is not None}
    return Evento.model_validate(cuerpo)


async def _recibir_pago(servicio, fabrica, evento):
    with fabrica() as s, s.begin():
        await servicio.manejar_pago(evento, s)


# --- criterio 1: se notifica al pagar ---------------------------------------------------------------------------------

async def test_un_pago_deja_una_notificacion_de_prioridad_alta_solo_por_push(armar, fabrica):
    correo = CorreoFalso()
    servicio = armar(correo=correo)

    await _recibir_pago(servicio, fabrica, _pago())
    await servicio.procesar_pendientes()

    (envio,) = _envios(fabrica)
    assert (envio.canal, envio.tipo, envio.estado, envio.cliente_id) == ("PUSH", "INDEMNIZACION_PAGADA", "ENVIADO", "cli-1")
    (n,) = _bandeja(fabrica)
    assert (n.titulo, n.prioridad, n.cliente_id, n.poliza_id, n.suscripcion_id) == (
        "Indemnización pagada", "ALTA", "cli-1", "90d7708a-1f26-4e2e-9c2d-722dca46c475", None,
    )
    assert correo.enviados == []


async def test_el_pago_fallido_no_avisa_al_cliente(armar, fabrica):
    servicio = armar()
    fallido = _pago()
    fallido = fallido.model_copy(update={"event_type": "indemnizacion_fallida"})

    await _recibir_pago(servicio, fabrica, fallido)

    assert _envios(fabrica) == []


# --- criterio 2: sin duplicados ---------------------------------------------------------------------------------------

async def test_reentregar_el_mismo_evento_no_duplica(armar, fabrica):
    servicio = armar()
    evento = _pago()

    await _recibir_pago(servicio, fabrica, evento)
    await _recibir_pago(servicio, fabrica, evento)
    await servicio.procesar_pendientes()

    assert len(_envios(fabrica)) == 1 and len(_bandeja(fabrica)) == 1


async def test_una_repeticion_con_otro_sobre_del_mismo_pago_tampoco_duplica(armar, fabrica):
    servicio = armar()
    repetido = Evento(
        event_type="indemnizacion_pagada", source="ms-pagos", correlation_id="c-1", payload=_pago().payload | {"repeticion": True}
    )

    await _recibir_pago(servicio, fabrica, _pago())
    await _recibir_pago(servicio, fabrica, repetido)
    await servicio.procesar_pendientes()

    assert repetido.event_id != _pago().event_id
    assert len(_bandeja(fabrica)) == 1


async def test_dos_pagos_distintos_son_dos_notificaciones(armar, fabrica):
    servicio = armar()

    await _recibir_pago(servicio, fabrica, _pago())
    await _recibir_pago(servicio, fabrica, _pago(indemnizacion_id="otro-pago"))
    await servicio.procesar_pendientes()

    assert len(_bandeja(fabrica)) == 2


# --- criterio 3: contenido seguro -------------------------------------------------------------------------------------

async def test_la_version_publica_no_lleva_monto_ni_poliza_y_el_detalle_si_lleva_el_monto(armar, fabrica):
    servicio = armar()
    await _recibir_pago(servicio, fabrica, _pago())
    await servicio.procesar_pendientes()

    (n,) = _bandeja(fabrica)

    publico = f"{n.titulo_publico} {n.texto_publico}"
    assert "1.500.000" not in publico and "90d7708a" not in publico and "Indemnización" not in publico
    assert "$ 1.500.000 COP" in n.texto


@pytest.mark.parametrize(
    ("monto", "moneda", "idioma", "esperado"),
    [
        ("1500000.00", "COP", "es-CO", "$ 1.500.000 COP"),
        ("1500000.00", "COP", "en-US", "COP 1,500,000"),
        ("1234.50", "COP", "es-CO", "$ 1.234,50 COP"),
        ("abc", "COP", "es-CO", None),
        (None, "COP", "es-CO", None),
    ],
)
def test_el_monto_se_escribe_segun_el_idioma(monto, moneda, idioma, esperado):
    assert monto_legible(monto, moneda, idioma) == esperado


# --- criterio 7: idioma -----------------------------------------------------------------------------------------------

async def test_el_idioma_del_dispositivo_manda(armar, fabrica):
    servicio = armar()
    _registrar_dispositivo(fabrica, "en-US")

    await _recibir_pago(servicio, fabrica, _pago())
    await servicio.procesar_pendientes()

    (n,) = _bandeja(fabrica)
    assert (n.titulo, n.idioma) == ("Indemnity paid", "en-US") and "COP 1,500,000" in n.texto


def test_sin_monto_el_texto_no_lo_inventa():
    push = construir_push_indemnizacion(
        envio_id="e1", cliente_id="c1", poliza_id="p1", monto=None, moneda=None, idioma="es-CO"
    )

    assert push.texto == "Te pagamos tu indemnización. Toca para ver tu póliza."


# --- criterio 8: un fallo del envío no se pierde ----------------------------------------------------------------------

async def test_un_fallo_del_push_se_reintenta_y_la_notificacion_llega(armar, fabrica, reloj):
    servicio = armar(push=PushFalso(fallos=1))

    await _recibir_pago(servicio, fabrica, _pago())
    await servicio.procesar_pendientes()
    (envio,) = _envios(fabrica)
    assert (envio.estado, envio.intentos, envio.ultimo_error) == ("PENDIENTE", 1, "PushNoDisponible")

    reloj.avanzar(60)
    await servicio.procesar_pendientes()
    assert _envios(fabrica)[0].estado == "ENVIADO"


# --- criterio 9: solo el titular --------------------------------------------------------------------------------------

async def test_solo_el_cliente_del_evento_recibe_el_aviso(armar, fabrica):
    servicio = armar()

    await _recibir_pago(servicio, fabrica, _pago(cliente_id="cli-2"))
    await servicio.procesar_pendientes()

    with fabrica() as s:
        filas = list(s.execute(select(BandejaPushDB)).scalars())
    assert [f.cliente_id for f in filas] == ["cli-2"]


# --- contrato ---------------------------------------------------------------------------------------------------------

def test_el_ejemplo_trae_los_campos_que_el_consumidor_necesita():
    assert set(CAMPOS_PAGO) <= set(_pago().payload) and _pago().event_type == "indemnizacion_pagada"


@pytest.mark.parametrize("falta", CAMPOS_PAGO)
async def test_un_pago_mal_formado_se_rechaza_para_que_vaya_a_la_cola_de_fallidos(armar, fabrica, falta):
    with pytest.raises(ValueError, match=falta):
        await _recibir_pago(armar(), fabrica, _pago(**{falta: None}))

    assert _envios(fabrica) == []


def test_el_envio_no_guarda_datos_de_cuenta(armar, fabrica):
    # El payload de Pagos no lleva cuenta, pero lo que se guarda es una lista cerrada de campos.
    import asyncio

    servicio = armar()
    asyncio.run(_recibir_pago(servicio, fabrica, _pago(cuenta_destino="cta_sim_x")))

    with fabrica() as s:
        (envio,) = list(s.execute(select(EnvioDB)).scalars())
    assert "cuenta_destino" not in envio.datos
