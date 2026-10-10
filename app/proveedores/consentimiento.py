"""Stub del contrato de consentimiento de MS Identidad (HU06) hasta que exista."""

from __future__ import annotations

from app.proveedores.dominio import EstadoConsentimiento


class ConsentimientoStub:
    def __init__(
        self,
        por_defecto: EstadoConsentimiento = EstadoConsentimiento.OTORGADO,
        estados: dict[str, EstadoConsentimiento] | None = None,
    ) -> None:
        self._por_defecto = por_defecto
        self._estados = estados or {}

    async def estado(self, cliente_id: str) -> EstadoConsentimiento:
        return self._estados.get(cliente_id, self._por_defecto)
