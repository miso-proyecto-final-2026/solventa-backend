"""Ensamblado de MS Socios: base, almacén efímero, cliente de Cotización, tokens, cuota e idempotencia."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.logs import ganchos_http
from app.socios.almacen import Almacen, AlmacenMemoria, AlmacenRedis
from app.socios.clientes import ClienteCotizacion, Cotizaciones
from app.socios.config import ConfigSocios
from app.socios.cuota import LimitadorCuota
from app.socios.idempotencia import Idempotencia
from app.socios.registro import RegistroSocios
from app.socios.repositorio import crear_engine, crear_tablas_locales
from app.socios.tokens import EmisorTokens


@dataclass
class Ensamblado:
    registro: RegistroSocios
    tokens: EmisorTokens
    almacen: Almacen
    limitador: LimitadorCuota
    idempotencia: Idempotencia
    cotizaciones: Cotizaciones
    http: httpx.AsyncClient | None = None

    async def detener(self) -> None:
        if self.http is not None:
            await self.http.aclose()
        await self.almacen.cerrar()


def construir_ensamblado(config: ConfigSocios) -> Ensamblado:
    engine = crear_engine(config.database_url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)
    fabrica: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)

    almacen: Almacen = AlmacenRedis.desde_url(config.redis_url) if config.redis_url else AlmacenMemoria()
    http = httpx.AsyncClient(
        base_url=config.cotizacion_url,
        timeout=config.timeout_cotizacion_s,
        event_hooks=ganchos_http(),
    )
    return Ensamblado(
        registro=RegistroSocios(fabrica, ttl_contratos_s=config.ttl_contratos_s),
        tokens=EmisorTokens(config.jwt_secreto, config.jwt_ttl_s),
        almacen=almacen,
        limitador=LimitadorCuota(almacen),
        idempotencia=Idempotencia(almacen, ttl_s=config.ttl_idempotencia_s),
        cotizaciones=ClienteCotizacion(http),
        http=http,
    )


def publicar(app_state: object, ensamblado: Ensamblado) -> None:
    """Deja cada pieza en `app.state` con el nombre que usan los routers."""
    for nombre in ("registro", "tokens", "almacen", "limitador", "idempotencia", "cotizaciones"):
        setattr(app_state, nombre, getattr(ensamblado, nombre))
