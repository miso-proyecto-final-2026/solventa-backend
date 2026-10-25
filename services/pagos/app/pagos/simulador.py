"""Pasarela de pago SIMULADA (adaptador 1). Permite probar el cobro sin un tercero y sin alcance PCI.

Modos (variable `PASARELA_SIM_MODO`, o por token con el prefijo `tok_sim_<modo>_…` para probar en staging sin
redesplegar):

- `APROBAR`: cobra y responde aprobado.
- `RECHAZAR`: no cobra y responde rechazado con código y motivo.
- `TARDAR`: cobra, pero tarda `PASARELA_SIM_DEMORA_S` en responder (con el tiempo de espera del adaptador, la respuesta
  no llega a tiempo y el cobro queda para conciliar).
- `PERDER_RESPUESTA`: cobra y la respuesta se pierde (EC-D1.2). Consultar la clave sí devuelve el cobro.

Es idempotente por `idempotency_key`, como una pasarela real: repetir la orden devuelve el primer resultado sin cobrar
otra vez. El libro (`simulador_cargos`) está en la base para que cualquier réplica pueda conciliar.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.pagos.dominio import (
    DatosToken,
    PasarelaSinRespuesta,
    ResultadoCobro,
    SolicitudCobro,
    TokenDesconocido,
)
from app.pagos.modelos_db import CargoSimuladoDB


class ModoSimulador(StrEnum):
    APROBAR = "APROBAR"
    RECHAZAR = "RECHAZAR"
    TARDAR = "TARDAR"
    PERDER_RESPUESTA = "PERDER_RESPUESTA"


# Prefijo del token → modo. El token por defecto de la app (HU17) es `tok_sim_<aleatorio>` y usa el modo configurado.
_PREFIJOS = {
    "tok_sim_aprobar_": ModoSimulador.APROBAR,
    "tok_sim_rechazar_": ModoSimulador.RECHAZAR,
    "tok_sim_tardar_": ModoSimulador.TARDAR,
    "tok_sim_perder_": ModoSimulador.PERDER_RESPUESTA,
}
_TOKEN_VALIDO = re.compile(r"^tok_[A-Za-z0-9_-]{1,190}$")
_MARCAS = ("VISA", "MASTERCARD", "AMEX")
RECHAZO = ("FONDOS_INSUFICIENTES", "La tarjeta no tiene fondos suficientes.")


def _ahora() -> datetime:
    return datetime.now(UTC)


def modo_desde_texto(valor: str) -> ModoSimulador:
    try:
        return ModoSimulador(valor.strip().upper())
    except ValueError as error:
        validos = ", ".join(m.value for m in ModoSimulador)
        raise ValueError(f"PASARELA_SIM_MODO={valor!r} no es válido; use uno de: {validos}") from error


class PasarelaSimulada:
    def __init__(
        self,
        fabrica: sessionmaker[Session],
        *,
        modo: ModoSimulador = ModoSimulador.APROBAR,
        demora_s: float = 10.0,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._modo = modo
        self._demora_s = demora_s
        self._reloj = reloj

    def modo_para(self, token: str) -> ModoSimulador:
        for prefijo, modo in _PREFIJOS.items():
            if token.startswith(prefijo):
                return modo
        return self._modo

    async def cobrar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        if not _TOKEN_VALIDO.match(solicitud.token):
            return ResultadoCobro(aprobado=False, codigo="TOKEN_INVALIDO", motivo="La pasarela no reconoce el token.")
        modo = self.modo_para(solicitud.token)
        resultado = await asyncio.to_thread(self._registrar, solicitud, modo)
        if modo is ModoSimulador.PERDER_RESPUESTA:
            raise PasarelaSinRespuesta("simulador: la respuesta se perdió (el cobro sí se hizo)")
        if modo is ModoSimulador.TARDAR:
            await asyncio.sleep(self._demora_s)
        return resultado

    async def consultar(self, idempotency_key: str) -> ResultadoCobro | None:
        def leer() -> ResultadoCobro | None:
            with self._fabrica() as s:
                cargo = s.get(CargoSimuladoDB, idempotency_key)
                return None if cargo is None else _resultado(cargo)

        return await asyncio.to_thread(leer)

    async def describir_token(self, token: str) -> DatosToken:
        """Simulado: el token no contiene la tarjeta, así que la marca y los últimos 4 se derivan del token."""
        if not _TOKEN_VALIDO.match(token):
            raise TokenDesconocido
        huella = int(hashlib.sha256(token.encode()).hexdigest(), 16)
        return DatosToken(ultimos4=f"{huella % 10_000:04d}", marca=_MARCAS[huella % len(_MARCAS)])

    def cargos(self) -> int:
        """Cobros hechos de verdad (aprobados) por el simulador: la evidencia de «cero cobros duplicados»."""
        with self._fabrica() as s:
            return s.execute(select(func.count()).select_from(CargoSimuladoDB).where(CargoSimuladoDB.aprobado)).scalar_one()

    def _registrar(self, solicitud: SolicitudCobro, modo: ModoSimulador) -> ResultadoCobro:
        aprobado = modo is not ModoSimulador.RECHAZAR
        codigo, motivo = (None, None) if aprobado else RECHAZO
        nuevo = CargoSimuladoDB(
            idempotency_key=solicitud.idempotency_key,
            referencia="sim_" + hashlib.sha256(solicitud.idempotency_key.encode()).hexdigest()[:20],
            monto=solicitud.monto,
            moneda=solicitud.moneda,
            aprobado=aprobado,
            codigo=codigo,
            motivo=motivo,
            solicitudes=1,
            created_at=self._reloj(),
        )
        try:
            with self._fabrica() as s, s.begin():
                s.add(nuevo)
            return _resultado(nuevo)
        except IntegrityError:
            pass
        # La clave ya se usó: se devuelve el primer resultado sin cobrar otra vez.
        with self._fabrica() as s, s.begin():
            cargo = s.get(CargoSimuladoDB, solicitud.idempotency_key, with_for_update=True)
            cargo.solicitudes += 1
            return _resultado(cargo)


def _resultado(cargo: CargoSimuladoDB) -> ResultadoCobro:
    if cargo.aprobado:
        return ResultadoCobro(aprobado=True, referencia_pasarela=cargo.referencia)
    return ResultadoCobro(aprobado=False, referencia_pasarela=cargo.referencia, codigo=cargo.codigo, motivo=cargo.motivo)
