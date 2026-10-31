"""Clientes HTTP de los servicios vecinos (REST síncrono, ADR-0002): Identidad para la sesión y el contacto, Suscripción para el enlace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from app.notificaciones.errores import DependenciaNoDisponible


@dataclass(frozen=True)
class Contacto:
    cliente_id: str
    nombre: str
    email: str


@dataclass(frozen=True)
class EnlaceCertificado:
    ruta: str
    vence_en: datetime


class Identidad(Protocol):
    async def cliente_de_token(self, token: str) -> str | None: ...

    async def contacto(self, cliente_id: str) -> Contacto | None: ...


class Certificados(Protocol):
    async def emitir_enlace(self, poliza_id: str, idioma: str) -> EnlaceCertificado | None:
        """El enlace de descarga, o `None` si Suscripción no tiene habilitados los enlaces (el correo sale sin él)."""
        ...


class ClienteIdentidad:
    """`GET /v1/sesion/actual` con el token del cliente y `GET /v1/contactos-cliente/{id}` (interno), ambos con la clave de servicio."""

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

    async def contacto(self, cliente_id: str) -> Contacto | None:
        try:
            r = await self._http.get(f"/v1/contactos-cliente/{cliente_id}", headers={"X-Service-Key": self._clave})
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible("Identidad no responde.") from error
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise DependenciaNoDisponible(f"Identidad respondió {r.status_code}.")
        d = r.json()
        return Contacto(cliente_id=d["cliente_id"], nombre=d["nombre"], email=d["email"])


class ClienteSuscripcion:
    """`POST /v1/interno/certificados/enlaces` (HU22): un enlace firmado y con vencimiento para el correo."""

    def __init__(self, http: httpx.AsyncClient, clave: str) -> None:
        self._http = http
        self._clave = clave

    async def emitir_enlace(self, poliza_id: str, idioma: str) -> EnlaceCertificado | None:
        try:
            r = await self._http.post(
                "/v1/interno/certificados/enlaces",
                json={"poliza_id": poliza_id, "idioma": idioma},
                headers={"X-Service-Key": self._clave},
            )
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible("Suscripción no responde.") from error
        if r.status_code == 503 and r.json().get("error", {}).get("code") == "ENLACES_NO_CONFIGURADOS":
            return None
        if r.status_code != 201:
            raise DependenciaNoDisponible(f"Suscripción respondió {r.status_code}.")
        d = r.json()
        vence = datetime.fromisoformat(d["vence_en"])
        return EnlaceCertificado(ruta=d["ruta"], vence_en=vence if vence.tzinfo else vence.replace(tzinfo=UTC))
