"""Validación de la condición paramétrica contra pólizas activas (HU28)."""

from datetime import UTC, datetime

import pytest
from app.eventos import (
    ConsumidorEventos,
    Evento,
    PoliticaReintentos,
    RegistroEventos,
    parametros_seguridad,
)
from app.siniestros.evaluacion import (
    GRUPO_POLIZAS,
    TOPIC_CICLO_VIDA,
    TOPIC_COMANDOS,
    Evaluador,
    RegistroPolizas,
    clave_idempotencia,
)
from app.siniestros.modelos_db import (
    EVENTOS_PROCESADOS,
    EvaluacionParametricaDB,
    PolizaParametricaDB,
)
from app.siniestros.parametrico import cargar_reglas
from sqlalchemy import select

from tests.siniestros.conftest import AHORA, evento, mensaje_de, no_dormir

REGLAS = cargar_reglas()
DESDE = datetime(2026, 1, 1, tzinfo=UTC)
HASTA = datetime(2027, 1, 1, tzinfo=UTC)


def sismo(**cambios):
    return evento(**{"datos": {"magnitud": 6.4, "zona": "bogota"}, **cambios})


def emitida(poliza_id="pol-1", producto="parametrico_clima", zona="bogota", desde=DESDE, hasta=HASTA) -> Evento:
    return Evento(
        event_type="poliza_emitida",
        source="ms-suscripcion",
        correlation_id="c-1",
        payload={
            "poliza_id": poliza_id,
            "producto": producto,
            "zona": zona,
            "vigencia_desde": desde.isoformat(),
            "fecha_vencimiento": hasta.isoformat(),
        },
    )


class Escenario:
    def __init__(self, entorno, tamano_lote=500) -> None:
        self.e = entorno
        self.evaluador = Evaluador(REGLAS, entorno.publicador, reloj=lambda: AHORA, tamano_lote=tamano_lote)
        self.registro = RegistroPolizas(REGLAS, reloj=lambda: AHORA)

    def _consumidor(self, topic, grupo, manejador) -> ConsumidorEventos:
        return ConsumidorEventos(
            brokers=["sin-kafka:9092"],
            seguridad=parametros_seguridad("PLAINTEXT"),
            topic=topic,
            grupo=grupo,
            manejador=manejador,
            fabrica_sesiones=self.e.fabrica,
            registro=RegistroEventos(EVENTOS_PROCESADOS),
            publicador=self.e.publicador,
            politica=PoliticaReintentos(intentos=2, base_s=0.01),
            dormir=no_dormir,
        )

    async def alta(self, *eventos: Evento) -> None:
        consumidor = self._consumidor(TOPIC_CICLO_VIDA, GRUPO_POLIZAS, self.registro.procesar)
        for i, ev in enumerate(eventos):
            await consumidor.procesar(mensaje_de(TOPIC_CICLO_VIDA, ev.payload["poliza_id"], ev, offset=i))

    def consumidor_evaluador(self) -> ConsumidorEventos:
        from app.siniestros.servicio import GRUPO_RECEPCION, TOPIC_EVENTOS

        return self._consumidor(TOPIC_EVENTOS, GRUPO_RECEPCION, self.evaluador.procesar)

    async def recibir_y_evaluar(self, cuerpo=None):
        assert self.e.enviar(cuerpo).status_code == 202
        await self.entregar()

    async def entregar(self) -> None:
        consumidor = self.consumidor_evaluador()
        recibidos = [p for p in self.e.publicador.publicados if p[0] == "solventa.siniestros.eventos"]
        for i, (topic, clave, publicado) in enumerate(recibidos):
            await consumidor.procesar(mensaje_de(topic, clave, publicado, offset=i))

    def comandos(self) -> list[Evento]:
        return [ev for topic, _clave, ev in self.e.publicador.publicados if topic == TOPIC_COMANDOS]

    def evaluaciones(self) -> list[EvaluacionParametricaDB]:
        with self.e.fabrica() as s:
            return list(s.scalars(select(EvaluacionParametricaDB).order_by(EvaluacionParametricaDB.poliza_id)))


@pytest.fixture
def esc(entorno):
    return Escenario(entorno)


# --- copia local de pólizas ---------------------------------------------------------------------------------------

async def test_solo_las_polizas_de_productos_parametricos_se_copian(esc):
    await esc.alta(emitida("pol-1"), emitida("pol-2", producto="microseguro_vida"))

    with esc.e.fabrica() as s:
        assert [p.poliza_id for p in s.scalars(select(PolizaParametricaDB))] == ["pol-1"]


async def test_una_poliza_cancelada_deja_de_cubrir_desde_la_cancelacion(esc):
    await esc.alta(emitida("pol-1"))
    cancelacion = Evento(
        event_type="poliza_cancelada",
        source="ms-suscripcion",
        correlation_id="c-1",
        payload={"poliza_id": "pol-1", "fecha_cancelacion": "2026-10-23T13:00:00+00:00"},
    )
    await esc.alta(cancelacion)

    await esc.recibir_y_evaluar(sismo())  # ocurrió a las 14:05, después de la cancelación

    assert esc.comandos() == [] and esc.evaluaciones()[0].motivo == "poliza_cancelada"


async def test_un_evento_anterior_a_la_cancelacion_sigue_cubierto(esc):
    await esc.alta(emitida("pol-1"))
    await esc.alta(
        Evento(
            event_type="poliza_cancelada",
            source="ms-suscripcion",
            correlation_id="c-1",
            payload={"poliza_id": "pol-1", "fecha_cancelacion": "2026-10-23T15:00:00+00:00"},
        )
    )

    await esc.recibir_y_evaluar(sismo())

    assert len(esc.comandos()) == 1


# --- criterios 1 a 4 ------------------------------------------------------------------------------------------------

async def test_condicion_cumplida_emite_el_comando_de_indemnizacion_con_clave_derivada(esc):
    await esc.alta(emitida("pol-1"))

    await esc.recibir_y_evaluar(sismo())

    [comando] = esc.comandos()
    [evaluacion] = esc.evaluaciones()
    assert evaluacion.resultado == "CORRESPONDE_PAGO" and evaluacion.version_reglas == "v1"
    assert comando.event_type == "indemnizacion_solicitada" and comando.event_id == evaluacion.comando_id
    assert comando.payload["idempotency_key"] == clave_idempotencia(evaluacion.evento_uuid, "pol-1")
    assert comando.payload["monto_indemnizacion"] == 5_000_000 and comando.payload["version_reglas"] == "v1"
    [(_, clave, _)] = [p for p in esc.e.publicador.publicados if p[0] == TOPIC_COMANDOS]
    assert clave == evaluacion.evento_uuid  # el contrato de infra particiona los comandos por evento


async def test_condicion_no_cumplida_registra_el_motivo_y_no_emite_comando(esc):
    await esc.alta(emitida("pol-1"))

    await esc.recibir_y_evaluar(sismo(datos={"magnitud": 5.2, "zona": "bogota"}))

    [evaluacion] = esc.evaluaciones()
    assert esc.comandos() == []
    assert (evaluacion.resultado, evaluacion.motivo) == ("NO_CORRESPONDE", "condicion_no_cumplida:magnitud")
    assert evaluacion.valores["magnitud"]["observado"] == 5.2 and evaluacion.comando_id is None


@pytest.mark.parametrize(
    "desde, hasta, motivo",
    [
        (datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC), "poliza_vencida"),
        (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 12, 1, tzinfo=UTC), "poliza_aun_no_vigente"),
    ],
)
async def test_una_poliza_vencida_o_sin_vigencia_en_la_fecha_del_evento_no_dispara_pago(esc, desde, hasta, motivo):
    await esc.alta(emitida("pol-1", desde=desde, hasta=hasta))

    await esc.recibir_y_evaluar(sismo())

    assert esc.comandos() == [] and esc.evaluaciones()[0].motivo == motivo


async def test_solo_se_evaluan_las_polizas_del_producto_y_tipo_del_evento(esc):
    await esc.alta(emitida("pol-clima"), emitida("pol-vuelo", producto="parametrico_vuelo", zona=None))

    await esc.recibir_y_evaluar(sismo())

    assert [e.poliza_id for e in esc.evaluaciones()] == ["pol-clima"]


async def test_un_evento_con_poliza_id_solo_evalua_esa_poliza(esc):
    await esc.alta(emitida("pol-1"), emitida("pol-2"))

    await esc.recibir_y_evaluar(sismo(poliza_id="pol-2"))

    assert [e.poliza_id for e in esc.evaluaciones()] == ["pol-2"] and len(esc.comandos()) == 1


async def test_un_evento_para_una_poliza_desconocida_se_registra_sin_pagar(esc):
    await esc.recibir_y_evaluar(sismo(poliza_id="pol-x"))

    assert esc.comandos() == [] and esc.evaluaciones()[0].motivo == "poliza_no_aplicable"


async def test_un_tipo_de_evento_sin_reglas_no_evalua_nada(esc):
    esc.evaluador = Evaluador(
        REGLAS.__class__("v1", {"parametrico_vuelo": REGLAS.por_producto["parametrico_vuelo"]}),
        esc.e.publicador,
        reloj=lambda: AHORA,
    )
    await esc.alta(emitida("pol-1"))

    await esc.recibir_y_evaluar(sismo())

    assert esc.evaluaciones() == [] and esc.comandos() == []


# --- criterio 5: un solo pago por evento y póliza -------------------------------------------------------------

async def test_la_reentrega_del_bus_no_emite_un_segundo_comando_y_queda_registrada(esc):
    await esc.alta(emitida("pol-1"))
    assert esc.e.enviar(sismo()).status_code == 202
    consumidor = esc.consumidor_evaluador()
    [(topic, clave, publicado)] = esc.e.publicador.publicados

    await consumidor.procesar(mensaje_de(topic, clave, publicado, offset=0))
    await consumidor.procesar(mensaje_de(topic, clave, publicado, offset=1))  # Kafka reentrega

    assert len(esc.comandos()) == 1 and len(esc.evaluaciones()) == 1
    assert esc.e.resumen()["reentregas"] == 1


async def test_aunque_el_mismo_par_llegue_por_otro_camino_no_se_paga_dos_veces(esc):
    """Defensa en profundidad: la restricción única del par evento–póliza, sin depender de la reserva del consumidor."""
    await esc.alta(emitida("pol-1"))
    assert esc.e.enviar(sismo()).status_code == 202
    [(_, _, publicado)] = esc.e.publicador.publicados

    with esc.e.fabrica() as s, s.begin():
        await esc.evaluador.procesar(publicado, s)
    with esc.e.fabrica() as s, s.begin():
        await esc.evaluador.procesar(publicado, s)

    assert len(esc.comandos()) == 1 and len(esc.evaluaciones()) == 1


# --- criterio 6: muchas pólizas por evento --------------------------------------------------------------------

async def test_un_evento_que_afecta_a_muchas_polizas_se_procesa_por_lotes_sin_perder_ninguna(entorno):
    esc = Escenario(entorno, tamano_lote=100)
    with entorno.fabrica() as s, s.begin():
        s.add_all(
            PolizaParametricaDB(
                poliza_id=f"pol-{i:05d}",
                producto="parametrico_clima",
                zona="bogota" if i % 4 else "cali",  # una de cada cuatro está fuera de la zona
                vigencia_desde=DESDE,
                fecha_vencimiento=HASTA,
                actualizada_en=AHORA,
            )
            for i in range(1050)
        )

    await esc.recibir_y_evaluar(sismo())

    assert len(esc.evaluaciones()) == 1050
    assert len(esc.comandos()) == 1050 - 263  # 263 pólizas de Cali (i múltiplo de 4)
    claves = {c.payload["idempotency_key"] for c in esc.comandos()}
    assert len(claves) == len(esc.comandos())


# --- resiliencia ------------------------------------------------------------------------------------------------

async def test_si_el_bus_no_acepta_el_comando_se_deshace_todo_y_el_reintento_lo_completa(esc):
    await esc.alta(emitida("pol-1"))
    assert esc.e.enviar(sismo()).status_code == 202
    esc.e.publicador.caido = True  # Kafka cae después de recibir el evento

    await esc.entregar()  # agota los reintentos con el bus caído

    assert esc.evaluaciones() == [] and esc.e.resumen()["procesados"] == 0  # nada quedó a medias
    assert [c[0] for c in esc.e.publicador.crudos] == ["solventa.siniestros.eventos.dlq"]

    esc.e.publicador.caido = False  # el bus vuelve y el mensaje se reentrega
    await esc.entregar()

    assert len(esc.comandos()) == 1 and len(esc.evaluaciones()) == 1


async def test_un_evento_ilegible_para_el_evaluador_va_a_la_cola_de_fallidos(esc):
    malo = Evento(event_type="evento_parametrico_recibido", source="ms-siniestros", correlation_id="c", payload={})
    consumidor = esc.consumidor_evaluador()

    await consumidor.procesar(mensaje_de("solventa.siniestros.eventos", "k", malo))

    assert esc.e.publicador.crudos[0][0] == "solventa.siniestros.eventos.dlq"
