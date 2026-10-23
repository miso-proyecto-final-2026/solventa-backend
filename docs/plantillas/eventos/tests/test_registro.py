"""Registro de eventos procesados: la clave de idempotencia se valida contra la base antes de actuar."""

from sqlalchemy import select


def _reentregas(base, consumidor, event_id):
    with base.fabrica() as s:
        return s.execute(
            select(base.tabla.c.reentregas).where(base.tabla.c.consumidor == consumidor, base.tabla.c.event_id == event_id)
        ).scalar_one_or_none()


def test_la_primera_vez_reserva_y_una_reentrega_se_detecta_y_se_cuenta(base):
    with base.fabrica() as s, s.begin():
        assert base.registro.reservar(s, "svc", "e1") is True
    with base.fabrica() as s, s.begin():
        assert base.registro.reservar(s, "svc", "e1") is False
    with base.fabrica() as s, s.begin():
        assert base.registro.reservar(s, "svc", "e1") is False

    assert _reentregas(base, "svc", "e1") == 2


def test_la_clave_es_por_consumidor(base):
    with base.fabrica() as s, s.begin():
        assert base.registro.reservar(s, "svc-a", "e1") is True
        assert base.registro.reservar(s, "svc-b", "e1") is True


def test_si_la_transaccion_falla_la_reserva_se_deshace(base):
    try:
        with base.fabrica() as s, s.begin():
            base.registro.reservar(s, "svc", "e1")
            raise RuntimeError("falló el manejador")
    except RuntimeError:
        pass

    assert _reentregas(base, "svc", "e1") is None
    with base.fabrica() as s, s.begin():
        assert base.registro.reservar(s, "svc", "e1") is True


def test_un_esquema_propio_del_servicio_se_aplica_a_la_tabla():
    from eventos.registro import tabla_eventos_procesados
    from sqlalchemy import MetaData

    tabla = tabla_eventos_procesados(MetaData(), schema="suscripcion")

    assert tabla.schema == "suscripcion" and tabla.fullname == "suscripcion.eventos_procesados"
