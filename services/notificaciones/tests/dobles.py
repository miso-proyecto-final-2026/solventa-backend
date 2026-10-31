"""Dobles para probar Notificaciones sin red: un reloj controlable y los vecinos y canales en memoria."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.eventos import Evento
from app.notificaciones.clientes import Contacto, EnlaceCertificado
from app.notificaciones.dominio import Correo, ErrorEnvio, Push
from app.notificaciones.errores import DependenciaNoDisponible

AHORA = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
CORREO_CLIENTE = "ana.perez@example.com"


class Reloj:
    def __init__(self, inicio: datetime = AHORA) -> None:
        self.ahora = inicio

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += timedelta(seconds=segundos)


class IdentidadFalsa:
    def __init__(self, contactos=None, tokens=None, falla=False):
        self.tokens = tokens if tokens is not None else {"token-1": "cli-1", "token-2": "cli-2"}
        self.contactos = contactos if contactos is not None else {"cli-1": Contacto("cli-1", "Ana", CORREO_CLIENTE)}
        self.falla = falla
        self.consultas = 0

    async def cliente_de_token(self, token):
        return self.tokens.get(token)

    async def contacto(self, cliente_id):
        self.consultas += 1
        if self.falla:
            raise DependenciaNoDisponible("Identidad no responde.")
        return self.contactos.get(cliente_id)


class CertificadosFalsos:
    """`modo`: "ok" entrega un enlace, "sin_enlaces" responde que no están habilitados y "caido" falla."""

    def __init__(self, modo: str = "ok", vence=None):
        self.modo = modo
        self.vence = vence or AHORA + timedelta(hours=24)
        self.llamadas: list[tuple[str, str]] = []

    async def emitir_enlace(self, poliza_id, idioma):
        self.llamadas.append((poliza_id, idioma))
        if self.modo == "caido":
            raise DependenciaNoDisponible("Suscripción no responde.")
        if self.modo == "sin_enlaces":
            return None
        return EnlaceCertificado("/v1/certificados/token-de-prueba", self.vence)


class CorreoFalso:
    def __init__(self, fallos: int = 0, codigo: str = "MessageRejected"):
        self.enviados: list[Correo] = []
        self.fallos = fallos
        self.codigo = codigo
        self.intentos = 0

    async def enviar(self, correo: Correo) -> str:
        self.intentos += 1
        if self.fallos > 0:
            self.fallos -= 1
            raise ErrorEnvio(self.codigo)
        self.enviados.append(correo)
        return f"msg-{len(self.enviados)}"


class PushFalso:
    def __init__(self, fallos: int = 0):
        self.entregados: list[Push] = []
        self.fallos = fallos

    async def entregar(self, push: Push) -> str:
        if self.fallos > 0:
            self.fallos -= 1
            raise ErrorEnvio("PushNoDisponible")
        self.entregados.append(push)
        return f"push-{len(self.entregados)}"


def payload(poliza_id: str = "pol-1", cliente_id: str = "cli-1", **cambios) -> dict:
    """El payload de `poliza_emitida` tal como lo publica MS Suscripción (HU20)."""
    datos = {
        "poliza_id": poliza_id,
        "suscripcion_id": "susc-1",
        "cliente_id": cliente_id,
        "numero_poliza": "36GBQKV2CWMM3EBB8Q6Q",
        "producto": "microseguro_vida",
        "vigencia_desde": "2026-10-30T20:00:00+00:00",
        "fecha_vencimiento": "2027-10-30T20:00:00+00:00",
        "fecha_emision": "2026-10-30T20:43:21+00:00",
    }
    datos.update(cambios)
    return {k: v for k, v in datos.items() if v is not None}


def poliza_emitida(**cambios) -> Evento:
    cuerpo = payload(**cambios)
    return Evento(event_type="poliza_emitida", source="ms-suscripcion", correlation_id="susc-1", payload=cuerpo)
