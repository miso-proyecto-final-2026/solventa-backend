"""El relevo publica lo que está en la cola de salida y deja el token fuera de la evidencia."""

from app.eventos import PublicadorMemoria
from app.suscripcion import repositorio as repo
from app.suscripcion.relay import RelayEventos

from tests.dobles import CotizacionesFalsas, cotizacion
from tests.test_servicio import PEDIDO, _iniciar


class PublicadorQueFalla(PublicadorMemoria):
    def __init__(self):
        super().__init__()
        self.falla = True

    async def publicar(self, topic, evento, clave):
        if self.falla:
            raise ConnectionError("broker caído")
        await super().publicar(topic, evento, clave)


async def _aprobada(armar):
    servicio = armar()
    susc, _ = await _iniciar(servicio)
    await servicio.decidir(susc.id)
    return susc


async def test_publica_el_evento_con_el_token_y_luego_lo_redacta(armar, fabrica):
    susc = await _aprobada(armar)
    publicador = PublicadorMemoria()

    assert await RelayEventos(fabrica, publicador).publicar_pendientes() == 1

    (topic, clave, evento) = publicador.publicados[0]
    assert (topic, clave) == ("solventa.suscripcion.comandos", susc.id)
    assert evento.payload["metodo_pago"] == PEDIDO["metodo_pago"]  # el consumidor sí lo necesita
    with fabrica() as s:
        assert repo.salidas_pendientes(s, limite=10) == []
        from app.suscripcion.modelos_db import EventoSalidaDB
        from sqlalchemy import select

        guardado = s.execute(select(EventoSalidaDB)).scalar_one()
    assert guardado.publicado_en is not None
    assert guardado.payload["payload"]["metodo_pago"] == "[redactado]"
    assert PEDIDO["metodo_pago"] not in str(guardado.payload)


async def test_si_el_broker_falla_el_evento_queda_pendiente_y_se_reintenta(armar, fabrica):
    await _aprobada(armar)
    publicador = PublicadorQueFalla()
    relay = RelayEventos(fabrica, publicador)

    assert await relay.publicar_pendientes() == 0
    with fabrica() as s:
        (pendiente,) = repo.salidas_pendientes(s, limite=10)
        assert pendiente.intentos == 1 and "broker caído" in pendiente.ultimo_error

    publicador.falla = False
    assert await relay.publicar_pendientes() == 1
    assert len(publicador.publicados) == 1


async def test_sin_pendientes_no_publica_nada(fabrica):
    assert await RelayEventos(fabrica, PublicadorMemoria()).publicar_pendientes() == 0


async def test_publica_por_lotes_y_en_orden_de_creacion(armar, fabrica):
    cotizaciones = CotizacionesFalsas(*(cotizacion(id=f"cot-{i}") for i in range(3)))
    servicio = armar(cotizaciones=cotizaciones)
    ids = []
    for i in range(3):
        susc, _ = await _iniciar(servicio, clave=f"k-{i}", cotizacion_id=f"cot-{i}")
        await servicio.decidir(susc.id)
        ids.append(susc.id)

    publicador = PublicadorMemoria()
    relay = RelayEventos(fabrica, publicador, lote=2)
    assert [await relay.publicar_pendientes() for _ in range(3)] == [2, 1, 0]
    assert [clave for _, clave, _ in publicador.publicados] == ids
