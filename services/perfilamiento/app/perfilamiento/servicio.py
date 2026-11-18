"""Caso de uso de Perfilamiento: consentimiento → fuentes → perfil canónico → persistencia + linaje.

Sin consentimiento vigente no se consulta ninguna fuente y el intento queda registrado con su
origen (EC-S1.2). El consentimiento se verifica en cada operación, también al leer el perfil, así
que una revocación se refleja de inmediato (criterio 9: el límite de 5 minutos queda holgado).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.contexto import correlacion_id
from app.perfilamiento.errores import (
    ClienteInvalido,
    ConsentimientoNoVigente,
    PerfilNoEncontrado,
    SumaAseguradaInvalida,
)
from app.perfilamiento.normalizacion import PerfilCanonico, normalizar
from app.perfilamiento.precio import OfertaCalculada, SumaFueraDeRango, calcular_oferta
from app.perfilamiento.reglas_precio import ReglasPrecio, cargar_reglas
from app.perfilamiento.repositorio import (
    LinajeGuardado,
    LineaLinaje,
    OfertaGuardada,
    PerfilGuardado,
    RepositorioPerfiles,
)
from app.proveedores.dominio import EstadoConsentimiento, ResultadoFuente
from app.proveedores.servicio import ServicioFuentes

log = logging.getLogger(__name__)

_CLIENTE_VALIDO = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _linea(fuente: ResultadoFuente, canonico: PerfilCanonico) -> LineaLinaje:
    return LineaLinaje(
        fuente=fuente.fuente.value,
        campos=tuple(sorted(n for n, c in canonico.campos.items() if c.fuente is fuente.fuente)),
        obtenido_en=fuente.obtenido_en,
        ttl_s=fuente.ttl_s,
        hit_cache=fuente.hit_cache,
        degradado=fuente.degradado,
        por_defecto=fuente.por_defecto,
    )


@dataclass(frozen=True)
class PerfilEnriquecido:
    guardado: PerfilGuardado

    @property
    def cuerpo(self) -> dict[str, Any]:
        return {
            "perfil_id": self.guardado.id,
            "creado_en": self.guardado.creado_en.isoformat(),
            "consentimiento_id": self.guardado.consentimiento_id,
            **self.guardado.perfil,
        }


class Enriquecedor:
    def __init__(
        self,
        fuentes: ServicioFuentes,
        repositorio: RepositorioPerfiles,
        reloj: Callable[[], datetime] | None = None,
        reglas_precio: ReglasPrecio | None = None,
    ) -> None:
        self._fuentes = fuentes
        self._repo = repositorio
        self._reglas = reglas_precio or cargar_reglas()
        self._reloj = reloj or (lambda: datetime.now(UTC))

    async def enriquecer(self, cliente_id: str, origen: str) -> PerfilEnriquecido:
        verificacion = await self._exigir_consentimiento(cliente_id, "ENRIQUECER", origen)
        crudo = await self._fuentes.consultar(cliente_id)
        canonico = normalizar(crudo)
        linaje = [_linea(f, canonico) for f in crudo.fuentes]
        guardado = await asyncio.to_thread(
            self._repo.guardar,
            cliente_id=cliente_id,
            perfil=canonico.a_dict(),
            completitud=canonico.completitud,
            degradado=canonico.degradado,
            consentimiento_id=verificacion.consentimiento_id,
            correlacion_id=correlacion_id.get(),
            creado_en=self._reloj(),
            linaje=linaje,
        )
        return PerfilEnriquecido(guardado)

    async def perfil_vigente(self, cliente_id: str, origen: str) -> PerfilEnriquecido:
        await self._exigir_consentimiento(cliente_id, "CONSULTAR_PERFIL", origen)
        guardado = await asyncio.to_thread(self._repo.ultimo, cliente_id)
        if guardado is None:
            raise PerfilNoEncontrado
        return PerfilEnriquecido(guardado)

    async def oferta(
        self, cliente_id: str, origen: str, suma_asegurada: int | None = None
    ) -> tuple[OfertaGuardada, OfertaCalculada, datetime]:
        """Oferta de vida hipotecario con el perfil vigente (HU25).

        Exige consentimiento vigente (sin él no se calcula y el intento queda registrado) y un perfil ya
        enriquecido: no se inventa un precio sin perfil. Guarda el desglose y el perfil usado, para auditoría.
        """
        verificacion = await self._exigir_consentimiento(cliente_id, "OFERTA", origen)
        perfil = await asyncio.to_thread(self._repo.ultimo, cliente_id)
        if perfil is None:
            raise PerfilNoEncontrado
        try:
            calculada = calcular_oferta(
                perfil.perfil["campos"],
                degradado=perfil.degradado,
                suma_asegurada=None if suma_asegurada is None else Decimal(suma_asegurada),
                reglas=self._reglas,
            )
        except SumaFueraDeRango as error:
            raise SumaAseguradaInvalida(str(error)) from error
        guardada = await asyncio.to_thread(
            self._repo.guardar_oferta,
            cliente_id=cliente_id,
            perfil_id=perfil.id,
            producto=calculada.producto,
            version_reglas=calculada.version_reglas,
            moneda=calculada.moneda,
            suma_asegurada=int(calculada.suma_asegurada),
            etapa_de_vida=calculada.etapa_de_vida,
            prima_anual=int(calculada.prima_anual),
            prima_mensual=int(calculada.prima_mensual),
            desglose=calculada.desglose(),
            degradado=calculada.degradado,
            consentimiento_id=verificacion.consentimiento_id,
            correlacion_id=correlacion_id.get(),
            creado_en=self._reloj(),
        )
        return guardada, calculada, perfil.creado_en

    async def linaje(self, cliente_id: str) -> LinajeGuardado:
        """Para auditoría: solo metadatos (fuentes, fechas, TTL), sin valores; no exige consentimiento."""
        _validar_cliente(cliente_id)
        guardado = await asyncio.to_thread(self._repo.ultimo, cliente_id)
        if guardado is None:
            raise PerfilNoEncontrado
        return await asyncio.to_thread(self._repo.linaje_de, guardado)

    async def _exigir_consentimiento(self, cliente_id: str, operacion: str, origen: str):
        _validar_cliente(cliente_id)
        verificacion = await self._fuentes.consentimiento.verificar(cliente_id)
        if verificacion.estado is EstadoConsentimiento.OTORGADO:
            return verificacion
        # El 100 % de los intentos bloqueados deja registro: si no se puede escribir, se
        # propaga el fallo (no se responde «bloqueado» sin evidencia).
        await asyncio.to_thread(
            self._repo.registrar_intento,
            cliente_id=cliente_id,
            operacion=operacion,
            origen=origen,
            estado_consentimiento=verificacion.estado.value,
            correlacion_id=correlacion_id.get(),
            ocurrido_en=self._reloj(),
        )
        raise ConsentimientoNoVigente(verificacion.estado)


def _validar_cliente(cliente_id: str) -> None:
    if not _CLIENTE_VALIDO.match(cliente_id):
        raise ClienteInvalido
