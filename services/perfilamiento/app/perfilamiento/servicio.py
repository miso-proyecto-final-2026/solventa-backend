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
from typing import Any

from app.contexto import correlacion_id
from app.perfilamiento.errores import (
    ClienteInvalido,
    ConsentimientoNoVigente,
    ConsultorInvalido,
    PerfilNoEncontrado,
)
from app.perfilamiento.normalizacion import PerfilCanonico, normalizar
from app.perfilamiento.repositorio import (
    LinajeGuardado,
    LineaLinaje,
    PerfilGuardado,
    RepositorioPerfiles,
)
from app.proveedores.dominio import EstadoConsentimiento, ResultadoFuente
from app.proveedores.servicio import ServicioFuentes

log = logging.getLogger(__name__)

_CLIENTE_VALIDO = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

CONSULTA_ENTREGADA = "ENTREGADO"
CONSULTA_SIN_PERFIL = "SIN_PERFIL"

# Tipo de dato de cada fuente (HU24, criterio 1). Es lo mismo que decide su TTL
# (`ConfigProveedores.ttl_de`): las señales financieras caducan antes que las socioeconómicas.
TIPO_DATO = {"open_finance": "FINANCIERO", "open_data": "SOCIOECONOMICO"}

# Solo se enriquece con consentimiento OTORGADO (`_exigir_consentimiento`), así que ese era su
# estado en el momento de la consulta de cada fuente del linaje.
ESTADO_CONSENTIMIENTO_AL_CONSULTAR = EstadoConsentimiento.OTORGADO.value


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
    ) -> None:
        self._fuentes = fuentes
        self._repo = repositorio
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

    async def linaje(
        self,
        cliente_id: str,
        origen: str,
        consultado_por: str | None = None,
        perfil_id: str | None = None,
    ) -> LinajeGuardado:
        """Para auditoría: solo metadatos (fuentes, fechas, TTL), sin valores; no exige consentimiento.

        Sin `perfil_id` es el linaje del perfil vigente (el último). Cada consulta, también la que no
        encuentra perfil, queda registrada con el servicio que llamó y la persona a cuyo nombre lo
        hizo (HU24). Si no se puede registrar, la consulta falla: no se entrega sin evidencia. Que
        no exija consentimiento es provisional: V-12 (#106) decide el comportamiento tras revocar.
        """
        _validar_cliente(cliente_id)
        if consultado_por is not None:
            _validar_consultor(consultado_por)
        if perfil_id is None:
            guardado = await asyncio.to_thread(self._repo.ultimo, cliente_id)
        else:
            guardado = await asyncio.to_thread(self._repo.por_id, cliente_id, perfil_id)
        await asyncio.to_thread(
            self._repo.registrar_consulta_linaje,
            cliente_id=cliente_id,
            perfil_id=guardado.id if guardado else None,
            origen=origen,
            consultado_por=consultado_por,
            resultado=CONSULTA_ENTREGADA if guardado else CONSULTA_SIN_PERFIL,
            consultado_en=self._reloj(),
            correlacion_id=correlacion_id.get(),
        )
        log.info(
            "linaje_consultado origen=%s consultado_por=%s resultado=%s",
            origen,
            consultado_por or "-",
            CONSULTA_ENTREGADA if guardado else CONSULTA_SIN_PERFIL,
        )
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


def _validar_consultor(consultado_por: str) -> None:
    if not _CLIENTE_VALIDO.match(consultado_por):
        raise ConsultorInvalido
