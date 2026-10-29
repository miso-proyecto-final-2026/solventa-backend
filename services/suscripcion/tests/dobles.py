"""Dobles de los servicios vecinos para probar la saga sin red."""

from datetime import UTC, datetime, timedelta

from app.suscripcion.clientes import Cobertura, CotizacionDatos, ResumenCliente
from app.suscripcion.dominio import PerfilRiesgo

AHORA = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

PERFIL_ESTANDAR = PerfilRiesgo(
    disponible=True,
    con_consentimiento=True,
    degradado=False,
    completitud=1.0,
    campos={"ratio_endeudamiento": 0.30, "reportes_negativos": 0},
)


COBERTURAS = {
    "microseguro_vida": (Cobertura("muerte", 40_000_000, 0), Cobertura("incapacidad_total", 20_000_000, 0)),
    "proteccion_dispositivos": (Cobertura("dano_accidental", 3_000_000, 300_000), Cobertura("robo", 3_000_000, 600_000)),
}


def cotizacion(id="cot-1", cliente="cli-1", monto=40_000_000, vigente_hasta=None, producto="microseguro_vida") -> CotizacionDatos:
    return CotizacionDatos(
        id=id,
        cliente_id=cliente,
        producto=producto,
        moneda="COP",
        prima_mensual=14_583,
        prima_anual=175_000,
        monto_asegurado=monto,
        vigente_hasta=vigente_hasta or AHORA + timedelta(days=5),
        coberturas=COBERTURAS.get(producto, ()),
        version_reglas="2026.10.1",
    )


class IdentidadFalsa:
    def __init__(self, tokens=None, sin_resumen=False):
        self.tokens = tokens if tokens is not None else {"token-1": "cli-1", "token-2": "cli-2"}
        self.sin_resumen = sin_resumen

    async def cliente_de_token(self, token):
        return self.tokens.get(token)

    async def resumen_cliente(self, cliente_id):
        self.resumenes = getattr(self, "resumenes", 0) + 1
        if self.sin_resumen:
            return None
        return ResumenCliente(cliente_id, "Ana María", "Pérez Gómez", "CC", "******300")


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
