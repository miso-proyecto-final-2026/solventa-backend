"""El relevo publica el resultado guardado en la cola de salida, con la clave de partición `suscripcion_id`."""

from app.eventos import PublicadorMemoria
from app.pagos import repositorio as repo
from app.pagos.relay import RelayEventos

from tests.dobles import TOKEN, evento


class PublicadorQueFalla(PublicadorMemoria):
    def __init__(self):
        super().__init__()
        self.falla = True

    async def publicar(self, topic, evento, clave):
        if self.falla:
            raise ConnectionError("broker caído")
        await super().publicar(topic, evento, clave)


async def _resuelto(armar, fabrica):
    servicio = armar()
    with fabrica() as s, s.begin():
        await servicio.manejar_evento(evento(), s)
    await servicio.procesar_pendientes()


async def test_publica_el_resultado_en_el_topic_de_resultados(armar, fabrica):
    await _resuelto(armar, fabrica)
    publicador = PublicadorMemoria()

    assert await RelayEventos(fabrica, publicador).publicar_pendientes() == 1

    ((topic, clave, ev),) = publicador.publicados
    assert (topic, clave, ev.event_type) == ("solventa.pagos.resultados", "susc-1", "cobro_prima_confirmado")
    assert TOKEN not in ev.model_dump_json()
    with fabrica() as s:
        assert repo.salidas_pendientes(s, limite=10) == []


async def test_si_el_broker_falla_el_evento_queda_pendiente_y_se_reintenta(armar, fabrica):
    await _resuelto(armar, fabrica)
    publicador = PublicadorQueFalla()
    relay = RelayEventos(fabrica, publicador)

    assert await relay.publicar_pendientes() == 0
    with fabrica() as s:
        (pendiente,) = repo.salidas_pendientes(s, limite=10)
        assert pendiente.intentos == 1 and "broker caído" in pendiente.ultimo_error

    publicador.falla = False
    assert await relay.publicar_pendientes() == 1
