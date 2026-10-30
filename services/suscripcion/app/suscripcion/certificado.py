"""Certificado de cobertura de una póliza emitida (HU22): PDF, descarga del titular y enlace firmado para el correo.

- El PDF repite los datos de la póliza ya emitida (no recalcula nada) y sale en el idioma del cliente (es-CO o en-US).
  Se genera la primera vez que se pide y se guarda en `certificados/` del bucket; las siguientes veces se entrega la
  copia guardada, así que pedirlo dos veces no crea nada nuevo.
- El titular lo descarga con su sesión. Para el correo, el servicio de Notificaciones pide un enlace: un token firmado
  con HMAC (sin sesión, pero con vencimiento) que lleva el id de la póliza y el idioma. No es un enlace prefirmado de
  S3 a propósito: aquellos caducan con las credenciales temporales del rol y podrían morir antes del plazo.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy.orm import Session, sessionmaker

from app.suscripcion import formato_es_co as fmt
from app.suscripcion import repositorio as repo
from app.suscripcion.almacen import AlmacenDocumentos, DocumentoNoEncontrado
from app.suscripcion.clientes import Identidad
from app.suscripcion.documento import _estilos, _tabla
from app.suscripcion.errores import (
    CertificadoNoDisponible,
    EnlaceInvalido,
    EnlacesNoConfigurados,
    EnlaceVencido,
    NoAutenticado,
    PolizaNoEncontrada,
    SuscripcionNoEncontrada,
)
from app.suscripcion.modelos_db import PolizaDB, SuscripcionDB
from app.suscripcion.polizas import VIGENTE

log = logging.getLogger("suscripcion.certificado")

DIRECTORIO = Path(__file__).resolve().parents[2] / "plantillas" / "certificado"
VERSION_VIGENTE = "v1"
IDIOMAS = ("es-CO", "en-US")
IDIOMA_POR_DEFECTO = "es-CO"
MESES_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def normalizar_idioma(valor: str | None) -> str:
    """El idioma de `Accept-Language` (`en`, `en-US,en;q=0.9`, `es-419`…) llevado a uno de los dos que hay. Sin dato: es-CO."""
    if valor:
        primero = valor.split(",")[0].strip().lower()
        if primero.startswith("en"):
            return "en-US"
    return IDIOMA_POR_DEFECTO


def cargar_textos(version: str = VERSION_VIGENTE, directorio: Path = DIRECTORIO) -> dict[str, dict[str, Any]]:
    datos = yaml.safe_load((directorio / f"{version}.yaml").read_text(encoding="utf-8"))
    return dict(datos["idiomas"])


def fecha(valor: datetime, idioma: str) -> str:
    """`26 de octubre de 2026` o `October 26, 2026`, en hora de Colombia."""
    if idioma == "en-US":
        local = valor.astimezone(fmt.BOGOTA) if valor.tzinfo else valor
        return f"{MESES_EN[local.month - 1]} {local.day}, {local.year}"
    return fmt.fecha(valor)


def monto(valor: float, moneda: str, idioma: str) -> str:
    """COP sin decimales: `$ 1.234.567` o `COP 1,234,567`."""
    if idioma == "en-US":
        return f"{moneda} {round(valor):,}"
    return fmt.monto(valor)


@dataclass(frozen=True)
class CoberturaCertificado:
    tipo: str
    monto: int
    deducible: int


@dataclass(frozen=True)
class DatosCertificado:
    numero_poliza: str
    producto: str
    moneda: str
    asegurado: str
    tipo_documento: str
    documento_enmascarado: str
    vigencia_desde: datetime
    vigencia_hasta: datetime
    fecha_emision: datetime
    prima_anual: int
    prima_mensual: int
    periodicidad_pago: str
    coberturas: tuple[CoberturaCertificado, ...]


def renderizar_certificado(datos: DatosCertificado, textos: dict[str, Any], idioma: str) -> bytes:
    """El PDF del certificado. Función pura: sin red ni base de datos."""
    t = textos[idioma]
    e = _estilos()
    nombre_producto = t["productos"].get(datos.producto, datos.producto)
    prima = (
        t["prima_mensual"].format(valor=monto(datos.prima_mensual, datos.moneda, idioma))
        if datos.periodicidad_pago == "MENSUAL"
        else t["prima_anual"].format(valor=monto(datos.prima_anual, datos.moneda, idioma))
    )
    historia: list = [
        Paragraph(escape(t["titulo"]), e["titulo"]),
        Paragraph(f"<b>{escape(t['estado_activa'])}</b>", e["texto"]),
        Spacer(1, 8),
        _tabla(
            [
                [t["numero_poliza"], datos.numero_poliza],
                [t["asegurado"], datos.asegurado],
                [t["documento"], f"{datos.tipo_documento} {datos.documento_enmascarado}"],
                [t["producto"], nombre_producto],
                [
                    t["vigencia"],
                    t["vigencia_valor"].format(desde=fecha(datos.vigencia_desde, idioma), hasta=fecha(datos.vigencia_hasta, idioma)),
                ],
                [t["emision"], fecha(datos.fecha_emision, idioma)],
                [t["prima"], prima],
            ],
            [5 * cm, 11 * cm],
            e,
        ),
        Paragraph(escape(t["coberturas"]), e["seccion"]),
        _tabla(
            [[t["amparo"], t["monto"], t["deducible"]]]
            + [
                [
                    t["amparos"].get(c.tipo, c.tipo.replace("_", " ").capitalize()),
                    monto(c.monto, datos.moneda, idioma),
                    monto(c.deducible, datos.moneda, idioma) if c.deducible else t["sin_deducible"],
                ]
                for c in datos.coberturas
            ],
            [7 * cm, 4.5 * cm, 4.5 * cm],
            e,
            cabecera=True,
        ),
        Spacer(1, 12),
        Paragraph(escape(t["nota"]), e["texto"]),
        Spacer(1, 6),
        Paragraph(f'<font color="{colors.grey.hexval()}">{escape(t["generado"])}</font>', e["texto"]),
    ]
    salida = BytesIO()
    SimpleDocTemplate(
        salida,
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"{t['titulo']} {datos.numero_poliza}",
        author="Solventa",
    ).build(historia)
    return salida.getvalue()


# --- enlaces firmados ------------------------------------------------------------------------------------------------

def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _desb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


class FirmadorEnlaces:
    """Token `payload.firma`: el payload lleva la póliza, el idioma y el vencimiento; la firma es HMAC-SHA256 con una clave del servicio.

    No guarda nada en la base: verificar es recalcular la firma. Un token alterado o con otra clave es inválido; uno
    vencido se distingue para decirle al cliente que pida otro.
    """

    def __init__(self, clave: str, vigencia: timedelta) -> None:
        if len(clave) < 16:
            raise ValueError("La clave de los enlaces debe tener al menos 16 caracteres.")
        self._clave = clave.encode()
        self.vigencia = vigencia

    def _firma(self, payload: bytes) -> str:
        return _b64(hmac.new(self._clave, payload, hashlib.sha256).digest())

    def emitir(self, poliza_id: str, idioma: str, ahora: datetime) -> tuple[str, datetime]:
        vence = ahora + self.vigencia
        payload = json.dumps({"p": poliza_id, "i": idioma, "e": int(vence.timestamp())}, separators=(",", ":")).encode()
        return f"{_b64(payload)}.{self._firma(payload)}", vence

    def verificar(self, token: str, ahora: datetime) -> tuple[str, str]:
        """(póliza, idioma) del token, o `EnlaceInvalido` / `EnlaceVencido`."""
        try:
            cuerpo, firma = token.split(".")
            payload = _desb64(cuerpo)
        except ValueError:
            raise EnlaceInvalido from None
        if not hmac.compare_digest(firma, self._firma(payload)):
            raise EnlaceInvalido
        try:
            datos = json.loads(payload)
            poliza_id, idioma, vence = str(datos["p"]), str(datos["i"]), int(datos["e"])
        except (ValueError, KeyError, TypeError):
            raise EnlaceInvalido from None
        if idioma not in IDIOMAS:
            raise EnlaceInvalido
        if ahora.timestamp() >= vence:
            raise EnlaceVencido
        return poliza_id, idioma


def clave_certificado(suscripcion_id: str, numero_poliza: str, idioma: str) -> str:
    """Sin datos personales: `certificados/<suscripcion>/<número>-<idioma>.pdf`."""
    return f"certificados/{suscripcion_id}/{numero_poliza}-{idioma}.pdf"


def _ahora() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Certificado:
    contenido: bytes
    numero_poliza: str


class ServicioCertificado:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        identidad: Identidad,
        almacen: AlmacenDocumentos,
        firmador: FirmadorEnlaces | None,
        textos: dict[str, dict[str, Any]] | None = None,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._identidad = identidad
        self._almacen = almacen
        self._firmador = firmador
        self._textos = textos or cargar_textos()
        self._reloj = reloj

    # --- descarga del titular ---------------------------------------------------------------------------------------

    async def descargar_titular(self, suscripcion_id: str, token: str | None, idioma: str) -> Certificado:
        """El certificado de la póliza de esa suscripción. La de otro cliente responde como inexistente."""
        if not token:
            raise NoAutenticado
        cliente_id = await self._identidad.cliente_de_token(token)
        if cliente_id is None:
            raise NoAutenticado

        def leer() -> tuple[PolizaDB, SuscripcionDB] | None:
            with self._fabrica() as s:
                susc = repo.obtener(s, suscripcion_id)
                poliza = repo.poliza_de_suscripcion(s, suscripcion_id)
                return (poliza, susc) if susc is not None and poliza is not None else None

        encontrada = await asyncio.to_thread(leer)
        if encontrada is None or encontrada[1].cliente_id != cliente_id:
            raise SuscripcionNoEncontrada
        return await self._certificado(*encontrada, idioma)

    # --- enlace para el correo --------------------------------------------------------------------------------------

    async def emitir_enlace(self, poliza_id: str, idioma: str) -> tuple[str, datetime]:
        """(token, vencimiento). Lo pide el servicio de Notificaciones con su clave de servicio."""
        if self._firmador is None:
            raise EnlacesNoConfigurados

        def existe() -> bool:
            with self._fabrica() as s:
                poliza = s.get(PolizaDB, poliza_id)
                return poliza is not None and poliza.estado == VIGENTE

        if not await asyncio.to_thread(existe):
            raise PolizaNoEncontrada
        return self._firmador.emitir(poliza_id, idioma, self._reloj())

    async def descargar_por_enlace(self, token: str) -> Certificado:
        if self._firmador is None:
            raise EnlacesNoConfigurados
        poliza_id, idioma = self._firmador.verificar(token, self._reloj())

        def leer() -> tuple[PolizaDB, SuscripcionDB] | None:
            with self._fabrica() as s:
                poliza = s.get(PolizaDB, poliza_id)
                susc = s.get(SuscripcionDB, poliza.suscripcion_id) if poliza is not None else None
                return (poliza, susc) if poliza is not None and susc is not None else None

        encontrada = await asyncio.to_thread(leer)
        if encontrada is None:
            raise EnlaceInvalido  # un enlace bien firmado a una póliza que ya no existe se trata como inválido
        return await self._certificado(*encontrada, idioma)

    # --- generación ---------------------------------------------------------------------------------------------------

    async def _certificado(self, poliza: PolizaDB, susc: SuscripcionDB, idioma: str) -> Certificado:
        if poliza.estado != VIGENTE:
            raise CertificadoNoDisponible
        clave = clave_certificado(susc.id, poliza.numero_poliza, idioma)
        try:
            return Certificado(await asyncio.to_thread(self._almacen.leer, clave), poliza.numero_poliza)
        except DocumentoNoEncontrado:
            pass
        cliente = await self._identidad.resumen_cliente(poliza.cliente_id)
        if cliente is None:
            raise CertificadoNoDisponible
        datos = DatosCertificado(
            numero_poliza=poliza.numero_poliza,
            producto=susc.producto,
            moneda=susc.moneda,
            asegurado=f"{cliente.nombre} {cliente.apellido}".strip(),
            tipo_documento=cliente.tipo_documento,
            documento_enmascarado=cliente.documento_enmascarado,
            vigencia_desde=repo.utc(poliza.vigencia_desde),
            vigencia_hasta=repo.utc(poliza.fecha_vencimiento),
            fecha_emision=repo.utc(poliza.fecha_emision) or repo.utc(poliza.updated_at),
            prima_anual=int(susc.prima_anual),
            prima_mensual=int(susc.prima_mensual),
            periodicidad_pago=susc.periodicidad_pago,
            coberturas=tuple(
                CoberturaCertificado(c["tipo"], int(c["monto"]), int(c.get("deducible", 0))) for c in (susc.coberturas or [])
            ),
        )
        contenido = await asyncio.to_thread(renderizar_certificado, datos, self._textos, idioma)
        await asyncio.to_thread(self._almacen.guardar, clave, contenido, "application/pdf")
        log.info("certificado generado poliza_id=%s idioma=%s bytes=%s", poliza.id, idioma, len(contenido))
        return Certificado(contenido, poliza.numero_poliza)
