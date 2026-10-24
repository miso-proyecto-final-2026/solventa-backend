"""Dobles de los servicios vecinos para probar la saga sin red."""

from datetime import UTC, datetime, timedelta

from app.suscripcion.clientes import CotizacionDatos
from app.suscripcion.dominio import PerfilRiesgo

AHORA = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

PERFIL_ESTANDAR = PerfilRiesgo(
    disponible=True,
    con_consentimiento=True,
    degradado=False,
    completitud=1.0,
    campos={"ratio_endeudamiento": 0.30, "reportes_negativos": 0},
)


def cotizacion(id="cot-1", cliente="cli-1", monto=40_000_000, vigente_hasta=None) -> CotizacionDatos:
    return CotizacionDatos(
        id=id,
        cliente_id=cliente,
        producto="microseguro_vida",
        moneda="COP",
        prima_mensual=14_583,
        prima_anual=175_000,
        monto_asegurado=monto,
        vigente_hasta=vigente_hasta or AHORA + timedelta(days=5),
    )


class IdentidadFalsa:
    def __init__(self, tokens=None):
        self.tokens = tokens if tokens is not None else {"token-1": "cli-1"}

    async def cliente_de_token(self, token):
        return self.tokens.get(token)


class CotizacionesFalsas:
    def __init__(self, *cotizaciones):
        self.por_id = {c.id: c for c in (cotizaciones or (cotizacion(),))}
        self.llamadas = 0

    async def obtener(self, cotizacion_id):
        self.llamadas += 1
        return self.por_id.get(cotizacion_id)


class PerfilesFalsos:
    def __init__(self, perfil=PERFIL_ESTANDAR):
        self.perfil = perfil
        self.llamadas = 0

    async def perfil_de(self, cliente_id):
        self.llamadas += 1
        return self.perfil
