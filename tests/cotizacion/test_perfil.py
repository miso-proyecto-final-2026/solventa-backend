from app.cotizacion.perfil import AdaptadorPerfilProveedores, unificar
from app.proveedores.consentimiento import ConsentimientoStub
from app.proveedores.dominio import (
    EstadoConsentimiento,
    Fuente,
    PerfilCrudo,
    ResultadoFuente,
)
from app.proveedores.simuladores import ModoFalla
from tests.proveedores.conftest import construir, redis  # noqa: F401  (fixtures reutilizadas)


def _crudo(*fuentes: ResultadoFuente, por_defecto=False) -> PerfilCrudo:
    return PerfilCrudo("c1", list(fuentes), por_defecto=por_defecto)


OF = {"monthlyIncomeCop": 4_500_000, "debtRatio": 0.32, "accountsOpenYears": 6}
OD = {"ocupacion": "empleado", "estrato": 3, "reportes_negativos": 0}


def test_mapea_los_campos_de_ambos_proveedores_a_nombres_canonicos():
    perfil = unificar(
        _crudo(
            ResultadoFuente(Fuente.OPEN_FINANCE, OF),
            ResultadoFuente(Fuente.OPEN_DATA, OD, hit_cache=True),
        )
    )

    assert dict(perfil.senales) == {
        "ingresos_mensuales_cop": 4_500_000,
        "ratio_endeudamiento": 0.32,
        "antiguedad_cuentas_anios": 6,
        "estrato": 3,
        "reportes_negativos": 0,
    }
    assert (perfil.degradado, perfil.con_consentimiento, perfil.hit_cache) == (
        False,
        True,
        False,
    )
    assert perfil.fuentes == ("open_finance", "open_data")


def test_hit_cache_solo_si_todas_las_fuentes_vinieron_de_cache():
    perfil = unificar(
        _crudo(
            ResultadoFuente(Fuente.OPEN_FINANCE, OF, hit_cache=True),
            ResultadoFuente(Fuente.OPEN_DATA, OD, hit_cache=True),
        )
    )

    assert perfil.hit_cache is True


def test_sin_consentimiento_no_hay_senales_ni_degradado():
    perfil = unificar(_crudo(por_defecto=True))

    assert perfil.con_consentimiento is False
    assert perfil.degradado is False
    assert dict(perfil.senales) == {}


def test_perfil_degradado_no_aporta_senales_aunque_venga_de_respaldo():
    perfil = unificar(
        _crudo(
            ResultadoFuente(Fuente.OPEN_FINANCE, OF, degradado=True, hit_cache=True),
            ResultadoFuente(
                Fuente.OPEN_DATA,
                {"datos_disponibles": False},
                degradado=True,
                por_defecto=True,
            ),
        )
    )

    assert perfil.degradado is True
    assert perfil.con_consentimiento is True
    assert dict(perfil.senales) == {}


def test_ignora_valores_que_no_son_numericos():
    perfil = unificar(
        _crudo(
            ResultadoFuente(
                Fuente.OPEN_FINANCE, {"debtRatio": "alto", "monthlyIncomeCop": True}
            )
        )
    )

    assert dict(perfil.senales) == {}


async def test_con_el_servicio_real_y_proveedores_sanos(construir):  # noqa: F811
    servicio, _, _ = construir()

    perfil = await AdaptadorPerfilProveedores(servicio).obtener("c1")

    assert perfil.degradado is False
    assert perfil.senales["ratio_endeudamiento"] == 0.32
    assert perfil.senales["reportes_negativos"] == 0


async def test_segunda_consulta_viene_de_cache(construir):  # noqa: F811
    servicio, _, _ = construir()
    adaptador = AdaptadorPerfilProveedores(servicio)

    primera = await adaptador.obtener("c1")
    segunda = await adaptador.obtener("c1")

    assert (primera.hit_cache, segunda.hit_cache) == (False, True)


async def test_con_proveedor_caido_el_perfil_sale_degradado(construir):  # noqa: F811
    servicio, _, _ = construir(modo_of=ModoFalla.ERROR_5XX)

    perfil = await AdaptadorPerfilProveedores(servicio).obtener("c1")

    assert perfil.degradado is True
    assert dict(perfil.senales) == {}


async def test_sin_consentimiento_con_el_servicio_real(construir):  # noqa: F811
    servicio, _, llamadas = construir(
        consentimiento=ConsentimientoStub(EstadoConsentimiento.NO_OTORGADO)
    )

    perfil = await AdaptadorPerfilProveedores(servicio).obtener("c1")

    assert perfil.con_consentimiento is False
    assert llamadas == []  # no se consultó a ningún proveedor


async def test_una_falla_inesperada_del_servicio_degrada_en_vez_de_fallar():
    class Roto:
        async def obtener_perfil(self, _cliente_id):
            raise RuntimeError("redis caído")

    perfil = await AdaptadorPerfilProveedores(Roto()).obtener("c1")

    assert perfil.degradado is True
