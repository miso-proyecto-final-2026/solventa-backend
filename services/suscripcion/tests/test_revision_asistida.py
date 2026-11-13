"""HU16: bandeja, detalle y decisión de la revisión asistida, con el servicio y por HTTP."""

import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.suscripcion import repositorio as repo
from app.suscripcion.acceso import Asesor, VerificadorJwtHs256
from app.suscripcion.api_revision import router
from app.suscripcion.clientes import Cotizaciones
from app.suscripcion.dominio import PerfilRiesgo
from app.suscripcion.errores import (
    CotizacionNoDisponible,
    CotizacionVencida,
    DependenciaNoDisponible,
    RevisionNoEncontrada,
    RevisionYaDecidida,
    SinMetodoPago,
    registrar_errores,
)
from app.suscripcion.modelos_db import (
    AuditoriaAsesorDB,
    RevisionAsistidaDB,
    SuscripcionDB,
)
from app.suscripcion.revision import CategoriaMotivo, Prioridad, UmbralesPrioridad
from app.suscripcion.revision_asistida import ServicioRevisionAsistida
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.dobles import (
    AHORA,
    CotizacionesFalsas,
    IdentidadFalsa,
    PerfilesFalsos,
    cotizacion,
)

SECRETO = "secreto-de-prueba-de-al-menos-32-bytes"
EMISOR = "solventa-identidad"
ASESOR = Asesor("ase-1", "Laura Gómez")
JUSTIFICACION = "Se revisaron los soportes de ingresos del cliente."
PEDIDO = {"token": "token-1", "clave": "k-1", "cotizacion_id": "cot-1", "metodo_pago": "tok_visa_123", "acepta_terminos": True}


class CotizacionesCaidas:
    """Cotización no responde."""

    async def obtener(self, cotizacion_id):
        raise DependenciaNoDisponible("Cotización no responde.")


def _revision(fabrica, cotizaciones: Cotizaciones | None = None, reloj=lambda: AHORA) -> ServicioRevisionAsistida:
    return ServicioRevisionAsistida(
        fabrica=fabrica,
        identidad=IdentidadFalsa(),
        cotizaciones=cotizaciones or CotizacionesFalsas(),
        umbrales=UmbralesPrioridad(24, 4),
        reloj=reloj,
    )


async def _derivar(armar, clave="k-1", cotizacion_id="cot-1", token="token-1", perfil=None, **servicios):
    """Una saga derivada por perfil no disponible, con su revisión abierta."""
    servicio = armar(perfiles=PerfilesFalsos(perfil or PerfilRiesgo.no_disponible("Perfilamiento no responde.")), **servicios)
    susc, _ = await servicio.iniciar(**{**PEDIDO, "clave": clave, "cotizacion_id": cotizacion_id, "token": token})
    await servicio.decidir(susc.id)
    return susc.id


def _saga(fabrica, susc_id) -> SuscripcionDB:
    with fabrica() as s:
        return repo.obtener(s, susc_id)


def _salidas(fabrica):
    with fabrica() as s:
        return repo.salidas_pendientes(s, limite=50)


def _auditoria(fabrica):
    with fabrica() as s:
        return list(s.execute(select(AuditoriaAsesorDB).order_by(AuditoriaAsesorDB.id)).scalars())


def _fijar(fabrica, susc_id, **campos):
    with fabrica() as s, s.begin():
        susc = s.get(SuscripcionDB, susc_id)
        for k, v in campos.items():
            setattr(susc, k, v)


@pytest.fixture
def revisiones(fabrica):
    return _revision(fabrica)


# --- iniciar guarda la vigencia ---------------------------------------------------------------------------------------

async def test_iniciar_guarda_la_vigencia_de_la_cotizacion(armar, fabrica):
    susc_id = await _derivar(armar)
    vigente = _saga(fabrica, susc_id).cotizacion_vigente_hasta
    assert repo.utc(vigente) == AHORA + timedelta(days=5)


# --- decidir: cada decisión (criterios 3, 4 y 5) -----------------------------------------------------------------------

async def test_aprobar_reanuda_la_saga_y_encola_el_cobro_con_el_monto_original(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    detalle = await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)

    saga = _saga(fabrica, susc_id)
    assert (saga.estado_saga, saga.paso_actual, saga.decision) == ("APROBADA", "COBRO", "APROBADA")
    assert saga.metodo_pago_token is None
    assert detalle.puede_decidir is False
    assert detalle.revision.decision == "APROBADA"
    cobro = next(s for s in _salidas(fabrica) if s.payload["event_type"] == "cobro_prima_solicitado")
    assert cobro.topic == "solventa.suscripcion.comandos"
    assert cobro.payload["payload"]["monto"] == "175000.00"
    assert cobro.payload["payload"]["metodo_pago"] == "tok_visa_123"


async def test_ajustar_escala_primas_y_coberturas_y_cobra_la_prima_ajustada(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, "AJUSTADA", JUSTIFICACION, 30_000_000)

    saga = _saga(fabrica, susc_id)
    assert (saga.estado_saga, saga.paso_actual, saga.decision) == ("APROBADA", "COBRO", "AJUSTADA")
    assert saga.monto_asegurado == 30_000_000
    assert (int(saga.prima_anual), int(saga.prima_mensual)) == (131_250, 10_937)
    assert [c["monto"] for c in saga.coberturas] == [30_000_000, 15_000_000]
    assert saga.ajuste["monto_original"] == 40_000_000
    cobro = next(s for s in _salidas(fabrica) if s.payload["event_type"] == "cobro_prima_solicitado")
    assert cobro.payload["payload"]["monto"] == "131250.00"
    with fabrica() as s:
        (revision,) = repo.revisiones_de(s, susc_id)
    assert revision.ajustes["factor"] == 0.75


async def test_rechazar_cierra_la_saga_sin_cobro_y_libera_la_cotizacion(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, "RECHAZADA", JUSTIFICACION)

    saga = _saga(fabrica, susc_id)
    assert (saga.estado_saga, saga.paso_actual, saga.decision) == ("RECHAZADA", "DECISION", "RECHAZADA")
    assert saga.metodo_pago_token is None
    assert "Rechazada tras revisión asistida." in saga.motivos
    assert not [s for s in _salidas(fabrica) if s.payload["event_type"] == "cobro_prima_solicitado"]
    with fabrica() as s:
        assert repo.buscar_activa_por_cotizacion(s, "cot-1") is None  # el cliente puede volver a cotizar


@pytest.mark.parametrize(("decision", "monto"), [("APROBADA", None), ("AJUSTADA", 20_000_000), ("RECHAZADA", None)])
async def test_toda_decision_deja_el_evento_sin_justificacion_y_la_revision_registrada(armar, fabrica, revisiones, decision, monto):
    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, decision, JUSTIFICACION, monto)

    resuelta = next(s for s in _salidas(fabrica) if s.payload["event_type"] == "revision_asistida_resuelta")
    assert resuelta.topic == "solventa.suscripcion.revision"
    assert resuelta.payload["payload"]["decision"] == decision
    assert resuelta.payload["payload"]["asesor_id"] == "ase-1"
    assert JUSTIFICACION not in str(resuelta.payload)
    assert "metodo_pago" not in resuelta.payload["payload"]
    with fabrica() as s:
        (revision,) = repo.revisiones_de(s, susc_id)
    assert (revision.decision, revision.justificacion) == (decision, JUSTIFICACION)
    assert (revision.asesor_id, revision.asesor_nombre) == ("ase-1", "Laura Gómez")
    assert revision.decidido_en is not None


# --- una sola decisión (criterio 6) y errores ---------------------------------------------------------------------------------

async def test_la_segunda_decision_recibe_409_con_quien_y_cuando(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)
    antes = len(_salidas(fabrica))

    with pytest.raises(RevisionYaDecidida) as error:
        await revisiones.decidir(Asesor("ase-2", "Otro"), susc_id, "RECHAZADA", JUSTIFICACION)

    detalles = {d["campo"]: d["motivo"] for d in error.value.detalles}
    assert (detalles["decision"], detalles["asesor"]) == ("APROBADA", "Laura Gómez")
    assert detalles["decidido_en"].startswith("2026-10-23")
    assert len(_salidas(fabrica)) == antes  # la segunda no encola nada
    assert _saga(fabrica, susc_id).estado_saga == "APROBADA"


async def test_una_saga_sin_revision_responde_404(armar, fabrica, revisiones):
    servicio = armar()
    susc, _ = await servicio.iniciar(**PEDIDO)  # sin decidir: no hay revisión
    with pytest.raises(RevisionNoEncontrada):
        await revisiones.decidir(ASESOR, susc.id, "APROBADA", JUSTIFICACION)
    with pytest.raises(RevisionNoEncontrada):
        await revisiones.decidir(ASESOR, "no-existe", "APROBADA", JUSTIFICACION)
    with pytest.raises(RevisionNoEncontrada):
        await revisiones.detalle("no-existe")


async def test_sin_metodo_de_pago_no_se_toca_la_saga(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    _fijar(fabrica, susc_id, metodo_pago_token=None)

    with pytest.raises(SinMetodoPago):
        await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)

    saga = _saga(fabrica, susc_id)
    assert (saga.estado_saga, saga.decision) == ("EN_PROGRESO", "DERIVADA")
    with fabrica() as s:
        assert repo.revisiones_de(s, susc_id)[0].decision is None


async def test_un_ajuste_invalido_no_toca_la_saga(armar, fabrica, revisiones):
    from app.suscripcion.errores import AjusteInvalido

    susc_id = await _derivar(armar)
    with pytest.raises(AjusteInvalido):
        await revisiones.decidir(ASESOR, susc_id, "AJUSTADA", JUSTIFICACION, 40_000_000)
    assert _saga(fabrica, susc_id).decision == "DERIVADA"


# --- auditoría ----------------------------------------------------------------------------------------------------------------

async def test_la_auditoria_registra_el_exito_y_tambien_el_intento_rechazado(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)
    with pytest.raises(RevisionYaDecidida):
        await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)
    with pytest.raises(RevisionNoEncontrada):
        await revisiones.decidir(ASESOR, "no-existe", "APROBADA", JUSTIFICACION)

    filas = [(a.accion, a.resultado, a.suscripcion_id, a.asesor_id) for a in _auditoria(fabrica)]
    assert filas == [
        ("DECIDIR_REVISION", "EXITO", susc_id, "ase-1"),
        ("DECIDIR_REVISION", "REVISION_YA_DECIDIDA", susc_id, "ase-1"),
        ("DECIDIR_REVISION", "REVISION_NO_ENCONTRADA", None, "ase-1"),
    ]


async def test_los_logs_no_traen_la_justificacion_ni_el_token(armar, fabrica, revisiones, caplog):
    susc_id = await _derivar(armar)
    with caplog.at_level(logging.DEBUG):
        await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)
    assert JUSTIFICACION not in caplog.text
    assert "tok_visa_123" not in caplog.text


# --- inmutabilidad (criterio 5) -----------------------------------------------------------------------------------------------

async def test_una_revision_decidida_no_se_puede_modificar_ni_borrar(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)

    with pytest.raises(RuntimeError, match="inmutable"), fabrica() as s, s.begin():
        s.execute(select(RevisionAsistidaDB)).scalars().first().justificacion = "otra cosa"
    with pytest.raises(RuntimeError, match="no se puede borrar"), fabrica() as s, s.begin():
        s.delete(s.execute(select(RevisionAsistidaDB)).scalars().first())


# --- cotización vencida --------------------------------------------------------------------------------------------------------

def _vencida(fabrica, susc_id):
    _fijar(fabrica, susc_id, cotizacion_vigente_hasta=AHORA - timedelta(minutes=1))


@pytest.mark.parametrize(("decision", "monto"), [("APROBADA", None), ("AJUSTADA", 20_000_000)])
async def test_aprobar_o_ajustar_con_la_cotizacion_vencida_da_409_y_no_cambia_la_saga(armar, fabrica, revisiones, decision, monto):
    susc_id = await _derivar(armar)
    _vencida(fabrica, susc_id)
    antes = len(_salidas(fabrica))

    with pytest.raises(CotizacionVencida) as error:
        await revisiones.decidir(ASESOR, susc_id, decision, JUSTIFICACION, monto)

    assert error.value.detalles[0]["campo"] == "vigente_hasta"
    saga = _saga(fabrica, susc_id)
    assert (saga.estado_saga, saga.decision, saga.monto_asegurado) == ("EN_PROGRESO", "DERIVADA", 40_000_000)
    assert len(_salidas(fabrica)) == antes
    assert _auditoria(fabrica)[-1].resultado == "COTIZACION_VENCIDA"  # el intento queda en la auditoría


async def test_rechazar_con_la_cotizacion_vencida_si_se_permite(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    _vencida(fabrica, susc_id)
    await revisiones.decidir(ASESOR, susc_id, "RECHAZADA", JUSTIFICACION)
    assert _saga(fabrica, susc_id).estado_saga == "RECHAZADA"


async def test_el_limite_exacto_cuenta_como_vencida(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    _fijar(fabrica, susc_id, cotizacion_vigente_hasta=AHORA)
    with pytest.raises(CotizacionVencida):
        await revisiones.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)


async def test_sin_vigencia_y_sin_cotizacion_aprobar_da_503_pero_rechazar_funciona(armar, fabrica):
    susc_id = await _derivar(armar)
    _fijar(fabrica, susc_id, cotizacion_vigente_hasta=None)
    caida = _revision(fabrica, CotizacionesCaidas())

    with pytest.raises(CotizacionNoDisponible):
        await caida.decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)
    assert _saga(fabrica, susc_id).decision == "DERIVADA"
    await caida.decidir(ASESOR, susc_id, "RECHAZADA", JUSTIFICACION)
    assert _saga(fabrica, susc_id).estado_saga == "RECHAZADA"


async def test_sin_vigencia_se_consulta_a_cotizacion_y_se_guarda_al_decidir(armar, fabrica):
    susc_id = await _derivar(armar)
    _fijar(fabrica, susc_id, cotizacion_vigente_hasta=None)
    cotizaciones = CotizacionesFalsas()
    await _revision(fabrica, cotizaciones).decidir(ASESOR, susc_id, "APROBADA", JUSTIFICACION)

    assert cotizaciones.llamadas == 1
    assert repo.utc(_saga(fabrica, susc_id).cotizacion_vigente_hasta) == AHORA + timedelta(days=5)


async def test_el_detalle_de_una_vencida_solo_permite_rechazar(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    _vencida(fabrica, susc_id)
    d = await revisiones.detalle(susc_id)
    assert (d.cotizacion_vencida, d.puede_decidir, d.decisiones_permitidas) == (True, True, ("RECHAZADA",))


async def test_el_detalle_de_una_vigente_permite_las_tres(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    d = await revisiones.detalle(susc_id)
    assert d.decisiones_permitidas == ("APROBADA", "RECHAZADA", "AJUSTADA")
    assert d.categorias == [CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES]
    assert d.prioridad is Prioridad.BAJA


async def test_el_detalle_de_un_caso_decidido_no_permite_decidir(armar, fabrica, revisiones):
    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, "RECHAZADA", JUSTIFICACION)
    d = await revisiones.detalle(susc_id)
    assert (d.puede_decidir, d.decisiones_permitidas) == (False, ())
    assert d.revision.justificacion == JUSTIFICACION
    assert d.categorias == [CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES]  # la frase genérica no cuenta


# --- bandeja (criterio 1) ------------------------------------------------------------------------------------------------------

async def _tres_casos(armar, fabrica):
    """Tres derivados en momentos distintos: el viejo, el medio y el nuevo; el segundo es de `ase-1`."""
    cotizaciones = CotizacionesFalsas(
        cotizacion(id="c-a"), cotizacion(id="c-b"), cotizacion(id="c-c", monto=90_000_000)
    )
    ids = [
        await _derivar(armar, clave=f"k-{i}", cotizacion_id=c, cotizaciones=cotizaciones)
        for i, c in enumerate(["c-a", "c-b", "c-c"])
    ]
    for i, horas in zip(ids, (30, 5, 1), strict=True):
        with fabrica() as s, s.begin():
            s.execute(select(RevisionAsistidaDB).where(RevisionAsistidaDB.suscripcion_id == i)).scalar_one().created_at = AHORA - timedelta(hours=horas)
    _fijar(fabrica, ids[1], asesor_id="ase-1")
    return ids


async def test_la_bandeja_ordena_por_antiguedad_y_calcula_la_prioridad(armar, fabrica, revisiones):
    ids = await _tres_casos(armar, fabrica)
    b = await revisiones.listar(ASESOR)

    assert [f.suscripcion.id for f in b.filas] == ids
    assert [f.prioridad for f in b.filas] == [Prioridad.ALTA, Prioridad.MEDIA, Prioridad.BAJA]
    assert b.total == 3
    assert b.filas[0].cliente.nombre == "Ana María"


async def test_la_bandeja_filtra_por_categoria_y_por_mias(armar, fabrica, revisiones):
    ids = await _tres_casos(armar, fabrica)
    # Los tres se derivaron por perfil no disponible (datos); el monto de c-c ya no importa porque el perfil falta primero.
    assert (await revisiones.listar(ASESOR, categoria=CategoriaMotivo.DATOS_INCOMPLETOS_O_INCONSISTENTES)).total == 3
    assert (await revisiones.listar(ASESOR, categoria=CategoriaMotivo.MONTO_SOBRE_LIMITE)).total == 0
    mias = await revisiones.listar(ASESOR, solo_mias=True)
    assert [f.suscripcion.id for f in mias.filas] == [ids[1]]
    assert (await revisiones.listar(ASESOR, pagina=2, tamano=2)).filas[0].suscripcion.id == ids[2]


async def test_la_bandeja_separa_pendientes_y_decididas(armar, fabrica, revisiones):
    ids = await _tres_casos(armar, fabrica)
    await revisiones.decidir(ASESOR, ids[0], "RECHAZADA", JUSTIFICACION)

    assert (await revisiones.listar(ASESOR)).total == 2
    decididas = await revisiones.listar(ASESOR, estado="DECIDIDA")
    assert [f.suscripcion.id for f in decididas.filas] == [ids[0]]


async def test_el_filtro_de_vencidas_y_su_conteo(armar, fabrica, revisiones):
    ids = await _tres_casos(armar, fabrica)
    _vencida(fabrica, ids[0])

    todas = await revisiones.listar(ASESOR)
    assert todas.total_vencidas == 1
    assert [f.cotizacion_vencida for f in todas.filas] == [True, False, False]
    solo = await revisiones.listar(ASESOR, vencidas=True)
    assert [f.suscripcion.id for f in solo.filas] == [ids[0]]
    assert solo.total_vencidas == 1
    # Una vencida ya decidida deja de contar.
    await revisiones.decidir(ASESOR, ids[0], "RECHAZADA", JUSTIFICACION)
    assert (await revisiones.listar(ASESOR)).total_vencidas == 0


async def test_asegurar_vigencias_llena_las_nulas_y_tolera_que_cotizacion_falle(armar, fabrica):
    ids = await _tres_casos(armar, fabrica)
    for i in ids:
        _fijar(fabrica, i, cotizacion_vigente_hasta=None)

    caida = _revision(fabrica, CotizacionesCaidas())
    b = await caida.listar(ASESOR)  # no falla
    assert b.total == 3
    assert all(f.vigente_hasta is None and not f.cotizacion_vencida for f in b.filas)

    todas = CotizacionesFalsas(cotizacion(id="c-a"), cotizacion(id="c-b"), cotizacion(id="c-c"))
    await _revision(fabrica, todas).asegurar_vigencias()
    assert all(repo.utc(_saga(fabrica, i).cotizacion_vigente_hasta) == AHORA + timedelta(days=5) for i in ids)


async def test_la_bandeja_sale_sin_cliente_si_identidad_falla(armar, fabrica):
    ids = await _tres_casos(armar, fabrica)

    class IdentidadCaida(IdentidadFalsa):
        async def resumen_cliente(self, cliente_id):
            raise DependenciaNoDisponible("Identidad no responde.")

    servicio = _revision(fabrica)
    servicio._identidad = IdentidadCaida()
    b = await servicio.listar(ASESOR)
    assert len(b.filas) == len(ids)
    assert all(f.cliente is None for f in b.filas)


async def test_de_varias_replicas_decidiendo_el_mismo_caso_solo_una_gana(armar, fabrica):
    """Con SQLite en memoria no hay bloqueo de filas: el servicio igual debe dejar una sola decisión efectiva."""
    susc_id = await _derivar(armar)
    servicio = _revision(fabrica)

    resultados = []
    for decision in ("APROBADA", "RECHAZADA", "APROBADA"):
        try:
            await servicio.decidir(ASESOR, susc_id, decision, JUSTIFICACION)
            resultados.append("ok")
        except RevisionYaDecidida:
            resultados.append("409")
    assert resultados == ["ok", "409", "409"]
    cobros = [s for s in _salidas(fabrica) if s.payload["event_type"] == "cobro_prima_solicitado"]
    assert len(cobros) == 1


# --- HTTP: acceso y contrato (criterios 3, 6 y 8) ------------------------------------------------------------------------------

def _jwt(sub="ase-1", rol="ASESOR", secreto=SECRETO, nombre="Laura Gómez"):
    exp = datetime.now(UTC) + timedelta(minutes=30)
    token = jwt.encode({"sub": sub, "rol": rol, "iss": EMISOR, "exp": exp, "nombre": nombre}, secreto, "HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def http(armar, fabrica):
    app = FastAPI()
    app.include_router(router)
    registrar_errores(app)
    app.state.servicio_revision = _revision(fabrica)
    app.state.verificador_asesor = VerificadorJwtHs256(SECRETO, EMISOR)
    susc_id = await _derivar(armar)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cliente:
        yield cliente, susc_id


async def test_sin_token_o_con_el_token_opaco_del_cliente_da_401(http):
    c, susc_id = http
    assert (await c.get("/v1/suscripciones/revisiones")).status_code == 401
    r = await c.get("/v1/suscripciones/revisiones", headers={"Authorization": "Bearer token-1"})
    assert r.status_code == 401
    r = await c.post(f"/v1/suscripciones/revisiones/{susc_id}/decision", json={}, headers={"Authorization": "Bearer token-1"})
    assert r.status_code == 401


async def test_un_operador_recibe_403(http):
    c, susc_id = http
    h = _jwt(rol="OPERADOR")
    assert (await c.get("/v1/suscripciones/revisiones", headers=h)).status_code == 403
    assert (await c.get(f"/v1/suscripciones/revisiones/{susc_id}", headers=h)).status_code == 403
    r = await c.post(
        f"/v1/suscripciones/revisiones/{susc_id}/decision", json={"decision": "APROBADA", "justificacion": JUSTIFICACION}, headers=h
    )
    assert r.status_code == 403


async def test_la_bandeja_y_el_detalle_por_http(http):
    c, susc_id = http
    r = await c.get("/v1/suscripciones/revisiones", headers=_jwt())
    cuerpo = r.json()
    assert r.status_code == 200
    assert (cuerpo["total"], cuerpo["total_vencidas"], cuerpo["pagina"]) == (1, 0, 1)
    fila = cuerpo["items"][0]
    assert (fila["id"], fila["prioridad"], fila["estado"], fila["cotizacion_vencida"]) == (susc_id, "BAJA", "PENDIENTE", False)
    assert fila["categorias"] == ["DATOS_INCOMPLETOS_O_INCONSISTENTES"]
    assert fila["cliente"]["inicial_apellido"] == "P"
    assert "Pérez" not in str(fila)  # apellido completo nunca

    r = await c.get(f"/v1/suscripciones/revisiones/{susc_id}", headers=_jwt())
    d = r.json()
    assert (d["puede_decidir"], d["decisiones_permitidas"]) == (True, ["APROBADA", "RECHAZADA", "AJUSTADA"])
    assert d["cliente_id"] == "cli-1"
    assert d["cotizacion"]["coberturas"][0]["tipo"] == "muerte"
    assert d["decision_automatica"]["motivos"][0]["categoria"] == "DATOS_INCOMPLETOS_O_INCONSISTENTES"
    assert d["decision_humana"] is None


async def test_decidir_por_http_y_la_segunda_da_409_con_el_cuerpo_estandar(http):
    c, susc_id = http
    url = f"/v1/suscripciones/revisiones/{susc_id}/decision"
    r = await c.post(url, json={"decision": "AJUSTADA", "justificacion": JUSTIFICACION, "monto_asegurado": 30_000_000}, headers=_jwt())
    assert r.status_code == 200
    assert r.json()["decision_humana"]["ajuste"]["monto_asegurado"] == 30_000_000
    assert r.json()["puede_decidir"] is False

    r = await c.post(url, json={"decision": "RECHAZADA", "justificacion": JUSTIFICACION}, headers=_jwt(sub="ase-2", nombre="Otro"))
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "REVISION_YA_DECIDIDA"
    assert {d["campo"] for d in error["details"]} == {"decision", "asesor", "decidido_en"}
    assert error["trace_id"]


async def test_la_validacion_del_cuerpo_responde_422(http):
    c, susc_id = http
    url = f"/v1/suscripciones/revisiones/{susc_id}/decision"
    for cuerpo in (
        {"decision": "APROBADA", "justificacion": "corta"},
        {"decision": "AJUSTADA", "justificacion": JUSTIFICACION},
        {"decision": "APROBADA", "justificacion": JUSTIFICACION, "monto_asegurado": 5},
    ):
        assert (await c.post(url, json=cuerpo, headers=_jwt())).status_code == 422
    r = await c.post(url, json={"decision": "AJUSTADA", "justificacion": JUSTIFICACION, "monto_asegurado": 50_000_000}, headers=_jwt())
    assert (r.status_code, r.json()["error"]["code"]) == (422, "AJUSTE_INVALIDO")


async def test_un_caso_inexistente_da_404(http):
    c, _ = http
    r = await c.get("/v1/suscripciones/revisiones/no-existe", headers=_jwt())
    assert (r.status_code, r.json()["error"]["code"]) == (404, "REVISION_NO_ENCONTRADA")


async def test_filtros_de_la_bandeja_por_http(http):
    c, _ = http
    h = _jwt()
    assert (await c.get("/v1/suscripciones/revisiones?mias=true", headers=h)).json()["total"] == 0
    assert (await c.get("/v1/suscripciones/revisiones?categoria=MONTO_SOBRE_LIMITE", headers=h)).json()["total"] == 0
    assert (await c.get("/v1/suscripciones/revisiones?estado=DECIDIDA", headers=h)).json()["total"] == 0
    assert (await c.get("/v1/suscripciones/revisiones?vencidas=true", headers=h)).json()["total"] == 0
    assert (await c.get("/v1/suscripciones/revisiones?categoria=OTRA", headers=h)).status_code == 422
    assert (await c.get("/v1/suscripciones/revisiones?tamano=0", headers=h)).status_code == 422


# --- contrato del evento -----------------------------------------------------------------------------------------------------

async def test_el_evento_de_resolucion_tiene_la_forma_publicada(armar, fabrica, revisiones):
    import json
    from pathlib import Path

    susc_id = await _derivar(armar)
    await revisiones.decidir(ASESOR, susc_id, "AJUSTADA", JUSTIFICACION, 30_000_000)
    resuelta = next(s for s in _salidas(fabrica) if s.payload["event_type"] == "revision_asistida_resuelta")
    publicado = json.loads((Path(__file__).parent / "contratos" / "revision_asistida_resuelta.json").read_text(encoding="utf-8"))

    assert set(resuelta.payload) == set(publicado)
    assert set(resuelta.payload["payload"]) == set(publicado["payload"])
    assert set(resuelta.payload["payload"]["ajuste"]) == set(publicado["payload"]["ajuste"])
