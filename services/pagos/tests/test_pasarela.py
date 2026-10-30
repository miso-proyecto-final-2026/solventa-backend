"""Adaptadores de pasarela (criterio 6, EC-M2.2): la suite de contrato, el simulador y su selección por configuración."""

import ast
from pathlib import Path

import pytest
from app.pagos.config import ConfigPagos
from app.pagos.dominio import PasarelaNoDisponible, PasarelaSinRespuesta, SolicitudCobro
from app.pagos.simulador import ModoSimulador, PasarelaSimulada, modo_desde_texto
from app.pagos.wiring import construir_pasarela

from tests.contrato_pasarela import ContratoPasarela, _solicitud
from tests.dobles import PasarelaFalsa

RAIZ = Path(__file__).parents[1] / "app" / "pagos"
MODOS = {"aprobar": ModoSimulador.APROBAR, "rechazar": ModoSimulador.RECHAZAR, "perder_respuesta": ModoSimulador.PERDER_RESPUESTA}
GUION = {"aprobar": "aprobar", "rechazar": "rechazar", "perder_respuesta": "perder"}


class TestContratoSimulador(ContratoPasarela):
    @pytest.fixture
    def preparar(self, fabrica):
        return lambda escenario: PasarelaSimulada(fabrica, modo=MODOS[escenario])


class TestContratoPasarelaFalsa(ContratoPasarela):
    """El doble de las pruebas también cumple el contrato: lo que prueban con él vale para cualquier adaptador."""

    @pytest.fixture
    def preparar(self):
        return lambda escenario: PasarelaFalsa(GUION[escenario])


# --- simulador -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "token, modo",
    [
        ("tok_sim_rechazar_1", ModoSimulador.RECHAZAR),
        ("tok_sim_perder_1", ModoSimulador.PERDER_RESPUESTA),
        ("tok_sim_tardar_1", ModoSimulador.TARDAR),
        ("tok_sim_aprobar_1", ModoSimulador.APROBAR),
        ("tok_sim_caida_1", ModoSimulador.NO_DISPONIBLE),
        ("tok_sim_ab12cd", ModoSimulador.RECHAZAR),  # token de la app (HU17): usa el modo configurado
    ],
)
def test_el_prefijo_del_token_elige_el_modo(fabrica, token, modo):
    assert PasarelaSimulada(fabrica, modo=ModoSimulador.RECHAZAR).modo_para(token) is modo


async def test_tardar_cobra_y_responde_despues_de_la_demora(fabrica):
    pasarela = PasarelaSimulada(fabrica, demora_s=0.01)
    solicitud = SolicitudCobro("k-t", "tok_sim_tardar_1", _solicitud().monto, "COP", "s")
    assert (await pasarela.cobrar(solicitud)).aprobado and pasarela.cargos() == 1


async def test_perder_respuesta_cobra_una_vez_aunque_se_repita_la_orden(fabrica):
    pasarela = PasarelaSimulada(fabrica, modo=ModoSimulador.PERDER_RESPUESTA)
    for _ in range(3):
        with pytest.raises(PasarelaSinRespuesta):
            await pasarela.cobrar(_solicitud())
    assert pasarela.cargos() == 1


async def test_no_disponible_no_recibe_la_orden_ni_cobra(fabrica):
    pasarela = PasarelaSimulada(fabrica, modo=ModoSimulador.NO_DISPONIBLE)
    with pytest.raises(PasarelaNoDisponible):
        await pasarela.cobrar(_solicitud())
    # Como no recibió la orden, no hay nada que conciliar: reintentar con la misma clave no duplica.
    assert await pasarela.consultar(_solicitud().idempotency_key) is None and pasarela.cargos() == 0


async def test_un_token_mal_formado_se_rechaza_sin_cobrar(fabrica):
    pasarela = PasarelaSimulada(fabrica)
    resultado = await pasarela.cobrar(SolicitudCobro("k", "4111111111111111", _solicitud().monto, "COP", "s"))
    assert not resultado.aprobado and resultado.codigo == "TOKEN_INVALIDO" and pasarela.cargos() == 0


def test_un_modo_desconocido_falla_con_un_mensaje_claro():
    with pytest.raises(ValueError, match="PERDER_RESPUESTA"):
        modo_desde_texto("explotar")


# --- selección por configuración -------------------------------------------------------------------------------

def test_la_pasarela_se_elige_por_configuracion(fabrica):
    pasarela = construir_pasarela(ConfigPagos.desde_entorno({"PASARELA_SIM_MODO": "rechazar"}), fabrica)
    assert isinstance(pasarela, PasarelaSimulada) and pasarela.modo_para("tok_x") is ModoSimulador.RECHAZAR


def test_un_proveedor_desconocido_no_arranca(fabrica):
    with pytest.raises(ValueError, match="PASARELA_PROVEEDOR"):
        construir_pasarela(ConfigPagos.desde_entorno({"PASARELA_PROVEEDOR": "otro"}), fabrica)


# --- arquitectura: solo el ensamblado conoce al adaptador --------------------------------------------------------

def _importa(archivo: Path) -> set[str]:
    modulos = set()
    for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            modulos.add(nodo.module)
        elif isinstance(nodo, ast.Import):
            modulos.update(a.name for a in nodo.names)
    return modulos


@pytest.mark.parametrize("archivo", ["dominio.py", "servicio.py", "api.py", "repositorio.py", "relay.py"])
def test_el_nucleo_no_importa_ningun_adaptador(archivo):
    assert "app.pagos.simulador" not in _importa(RAIZ / archivo)


def test_solo_el_ensamblado_importa_el_simulador():
    quienes = {p.name for p in RAIZ.glob("*.py") if "app.pagos.simulador" in _importa(p)}
    assert quienes == {"wiring.py"}
