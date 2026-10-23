"""HU07 — Efecto de revocar el consentimiento en Cotización (criterios 3 y 4).

Cotización consulta el consentimiento a Identidad en cada operación y antes de usar cualquier dato o
caché (ADR-0002, ADR-0009): la revocación surte efecto en la siguiente llamada, sin esperar al TTL.
"""

import httpx

from app.cotizacion.perfil import AdaptadorPerfilProveedores
from app.proveedores.consentimiento import ConsentimientoHttp
from app.proveedores.dominio import EstadoConsentimiento
from tests.proveedores.conftest import construir, redis  # noqa: F401  (fixtures reutilizadas)

BASE = "http://identidad.test"


class IdentidadSimulada:
    """Identidad con el estado del consentimiento mutable, como lo deja POST/DELETE de HU06/HU07."""

    def __init__(self, estado: str = "OTORGADO") -> None:
        self.estado = estado
        self.consultas = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.consultas += 1
        return httpx.Response(200, json={"id": "k1", "cliente_id": "c1", "estado": self.estado})


def _contar_llamadas_a_proveedores(proveedores, monkeypatch) -> list[str]:
    """Envuelve `consultar_perfil` de cada proveedor y devuelve la lista (viva) de llamadas reales."""
    vistas: list[str] = []
    for nombre, proveedor in proveedores.items():
        original = proveedor.consultar_perfil

        async def espia(*args, _original=original, _nombre=nombre, **kwargs):
            vistas.append(str(_nombre))
            return await _original(*args, **kwargs)

        monkeypatch.setattr(proveedor, "consultar_perfil", espia)
    return vistas


def _consentimiento(identidad: IdentidadSimulada) -> ConsentimientoHttp:
    http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(identidad))
    return ConsentimientoHttp(BASE, "clave", cliente=http)


async def test_tras_revocar_no_se_sirve_el_perfil_en_cache_ni_se_llama_a_proveedores(
    construir, redis, monkeypatch  # noqa: F811
):
    identidad = IdentidadSimulada("OTORGADO")
    servicio, proveedores, _ = construir(consentimiento=_consentimiento(identidad))
    antes = await servicio.obtener_perfil("c1")  # miss: consulta y cachea
    hit = await servicio.obtener_perfil("c1")
    assert not antes.por_defecto and antes.fuentes
    assert all(f.hit_cache for f in hit.fuentes), "con consentimiento el perfil sale del caché"
    llamadas = _contar_llamadas_a_proveedores(proveedores, monkeypatch)

    identidad.estado = "REVOCADO"
    despues = await servicio.obtener_perfil("c1")

    assert despues.por_defecto is True
    assert despues.fuentes == [], "el perfil cacheado no se entrega"
    assert llamadas == [], "ningún proveedor recibe la consulta"
    assert await redis.keys("*"), "el perfil cacheado no se borra: solo deja de servirse"


async def test_la_siguiente_cotizacion_es_aproximada_con_perfil_por_defecto(construir):  # noqa: F811
    identidad = IdentidadSimulada("OTORGADO")
    servicio, _, _ = construir(consentimiento=_consentimiento(identidad))
    adaptador = AdaptadorPerfilProveedores(servicio)

    con_consentimiento = await adaptador.obtener("c1")
    identidad.estado = "REVOCADO"
    sin_consentimiento = await adaptador.obtener("c1")

    assert con_consentimiento.con_consentimiento is True and con_consentimiento.senales
    assert sin_consentimiento.con_consentimiento is False
    assert not sin_consentimiento.senales
    assert sin_consentimiento.degradado is False


async def test_el_efecto_es_inmediato_porque_el_consentimiento_no_se_cachea(construir):  # noqa: F811
    identidad = IdentidadSimulada("OTORGADO")
    servicio, _, _ = construir(consentimiento=_consentimiento(identidad))
    await servicio.obtener_perfil("c1")
    consultas_antes = identidad.consultas

    identidad.estado = "REVOCADO"
    await servicio.obtener_perfil("c1")

    assert identidad.consultas == consultas_antes + 1, "cada operación consulta a Identidad"


async def test_revocar_deja_huella_de_los_intentos_sin_consentimiento(construir):  # noqa: F811
    identidad = IdentidadSimulada("REVOCADO")
    servicio, _, _ = construir(consentimiento=_consentimiento(identidad))
    huellas = []
    servicio.conectar_trazabilidad(huellas.append)

    await servicio.obtener_perfil("c1")

    assert huellas and {h.resultado.name for h in huellas} == {"SIN_CONSENTIMIENTO"}


async def test_volver_a_otorgar_restablece_el_perfil_personalizado(construir):  # noqa: F811
    identidad = IdentidadSimulada("OTORGADO")
    servicio, _, _ = construir(consentimiento=_consentimiento(identidad))
    await servicio.obtener_perfil("c1")
    identidad.estado = "REVOCADO"
    assert (await servicio.obtener_perfil("c1")).por_defecto is True

    identidad.estado = "OTORGADO"
    de_nuevo = await servicio.obtener_perfil("c1")

    assert de_nuevo.por_defecto is False and de_nuevo.fuentes


async def test_una_revocacion_no_abre_el_circuit_breaker(construir):  # noqa: F811
    identidad = IdentidadSimulada("REVOCADO")
    adaptador = _consentimiento(identidad)
    servicio, _, _ = construir(consentimiento=adaptador)

    for _ in range(20):
        await servicio.obtener_perfil("c1")

    assert adaptador.circuito_abierto is False
    assert await adaptador.estado("c1") is EstadoConsentimiento.REVOCADO


async def test_si_identidad_cae_tras_revocar_se_sigue_sin_usar_datos(construir, monkeypatch):  # noqa: F811
    """Falla cerrado: sin respuesta de Identidad el consentimiento se trata como NO_OTORGADO."""

    def caida(request):
        raise httpx.ConnectTimeout("sin respuesta")

    http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(caida))
    servicio, proveedores, _ = construir(consentimiento=ConsentimientoHttp(BASE, "clave", cliente=http))
    llamadas = _contar_llamadas_a_proveedores(proveedores, monkeypatch)

    perfil = await servicio.obtener_perfil("c1")

    assert perfil.por_defecto is True and perfil.fuentes == []
    assert llamadas == []
