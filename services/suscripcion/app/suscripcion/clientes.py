"""Clientes HTTP de los servicios vecinos. Suscripción los conoce solo por su contrato REST (ADR-0001)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from app.suscripcion.dominio import PerfilRiesgo
from app.suscripcion.errores import DependenciaNoDisponible


@dataclass(frozen=True)
class Cobertura:
    tipo: str
    monto: int
    deducible: int


@dataclass(frozen=True)
class ResumenCliente:
    """Lo que el documento de la póliza necesita del cliente. El documento llega ya enmascarado desde Identidad."""

    cliente_id: str
    nombre: str
    apellido: str
    tipo_documento: str
    documento_enmascarado: str


@dataclass(frozen=True)
class CotizacionDatos:
    id: str
    cliente_id: str
    producto: str
    moneda: str
    prima_mensual: int
    prima_anual: int
    monto_asegurado: int  # la cobertura principal (la de mayor monto): las demás son parte de ella, no se suman
    vigente_hasta: datetime
    coberturas: tuple[Cobertura, ...] = ()
    version_reglas: str | None = None
    asesor_id: str | None = None  # quién atendió al cliente (HU39); nulo si la cotización no es asistida
    sin_cuenta: bool = False


class Identidad(Protocol):
    async def cliente_de_token(self, token: str) -> str | None: ...

    async def resumen_cliente(self, cliente_id: str) -> ResumenCliente | None: ...


class Cotizaciones(Protocol):
    async def obtener(self, cotizacion_id: str) -> CotizacionDatos | None: ...


class Perfiles(Protocol):
    async def perfil_de(self, cliente_id: str) -> PerfilRiesgo: ...


class ClienteIdentidad:
    """`GET /v1/sesion/actual` con la clave de servicio y el token del cliente."""

    def __init__(self, http: httpx.AsyncClient, clave: str) -> None:
        self._http = http
        self._clave = clave

    async def cliente_de_token(self, token: str) -> str | None:
        try:
            r = await self._http.get(
                "/v1/sesion/actual", headers={"X-Service-Key": self._clave, "Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible("Identidad no responde.") from error
        if r.status_code == 401:
            return None
        if r.status_code != 200:
            raise DependenciaNoDisponible(f"Identidad respondió {r.status_code}.")
        return str(r.json()["cliente_id"])

    async def resumen_cliente(self, cliente_id: str) -> ResumenCliente | None:
        """`GET /v1/resumenes-cliente/{id}` (interno, HU20): nombre y documento enmascarado para el PDF de la póliza."""
        try:
            r = await self._http.get(f"/v1/resumenes-cliente/{cliente_id}", headers={"X-Service-Key": self._clave})
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible("Identidad no responde.") from error
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise DependenciaNoDisponible(f"Identidad respondió {r.status_code}.")
        d = r.json()
        return ResumenCliente(
            cliente_id=d["cliente_id"],
            nombre=d["nombre"],
            apellido=d["apellido"],
            tipo_documento=d["tipo_documento"],
            documento_enmascarado=d["documento_enmascarado"],
        )


class ClienteCotizacion:
    """`GET /v1/cotizaciones/{id}`."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def obtener(self, cotizacion_id: str) -> CotizacionDatos | None:
        try:
            r = await self._http.get(f"/v1/cotizaciones/{cotizacion_id}")
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible("Cotización no responde.") from error
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise DependenciaNoDisponible(f"Cotización respondió {r.status_code}.")
        c = r.json()
        vigente = datetime.fromisoformat(c["vigente_hasta"])
        return CotizacionDatos(
            id=c["id"],
            cliente_id=c["cliente_id"],
            producto=c["producto"],
            moneda=c["moneda"],
            prima_mensual=int(c["prima_mensual"]),
            prima_anual=int(c["prima_anual"]),
            monto_asegurado=max((int(x["monto"]) for x in c.get("coberturas", [])), default=0),
            vigente_hasta=vigente if vigente.tzinfo else vigente.replace(tzinfo=UTC),
            coberturas=tuple(
                Cobertura(tipo=str(x["tipo"]), monto=int(x["monto"]), deducible=int(x.get("deducible", 0)))
                for x in c.get("coberturas", [])
            ),
            version_reglas=c.get("version_reglas"),
            asesor_id=c.get("asesor_id"),
            sin_cuenta=bool(c.get("sin_cuenta", False)),
        )


class ClientePerfilamiento:
    """`GET /v1/perfilamiento/{cliente_id}/perfil`. Un fallo o la falta de perfil no detiene la compra: se deriva a una persona."""

    def __init__(self, http: httpx.AsyncClient, clave: str) -> None:
        self._http = http
        self._clave = clave

    async def perfil_de(self, cliente_id: str) -> PerfilRiesgo:
        try:
            r = await self._http.get(f"/v1/perfilamiento/{cliente_id}/perfil", headers={"X-Service-Key": self._clave})
        except httpx.HTTPError:
            return PerfilRiesgo.no_disponible("Perfilamiento no responde.")
        if r.status_code == 403:
            return PerfilRiesgo.sin_consentimiento()
        if r.status_code == 404:
            return PerfilRiesgo.no_disponible("El cliente no tiene perfil enriquecido.")
        if r.status_code != 200:
            return PerfilRiesgo.no_disponible(f"Perfilamiento respondió {r.status_code}.")
        p = r.json()
        return PerfilRiesgo(
            disponible=True,
            con_consentimiento=bool(p.get("con_consentimiento", True)),
            degradado=bool(p.get("degradado", False)),
            completitud=float(p.get("completitud", 0.0)),
            campos={k: v["valor"] for k, v in p.get("campos", {}).items()},
        )
