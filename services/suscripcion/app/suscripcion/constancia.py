"""Copia firmada de la póliza (HU21): el PDF original más una página de constancia con la evidencia de la firma.

El documento original no se modifica: sus páginas se copian tal cual y la constancia se agrega al final, de modo que el
hash del original (que el cliente vio y firmó) sigue siendo verificable contra la copia.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.suscripcion import formato_es_co as fmt
from app.suscripcion.documento import _estilos, _tabla


@dataclass(frozen=True)
class EvidenciaFirma:
    numero_poliza: str
    firmante_id: str
    firmada_en: datetime
    canal: str
    ip: str | None
    dispositivo: str | None
    metodo_autenticacion: str
    proveedor: str
    referencia_proveedor: str | None
    documento_sha256: str


CANALES = {"MOVIL": "App móvil", "WEB": "Web"}


def _constancia(e: EvidenciaFirma) -> bytes:
    estilos = _estilos()
    salida = BytesIO()
    SimpleDocTemplate(
        salida, pagesize=A4, leftMargin=2.5 * cm, rightMargin=2.5 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"Constancia de firma {e.numero_poliza}", author="Solventa",
    ).build(
        [
            Paragraph("Constancia de firma electrónica", estilos["titulo"]),
            Paragraph(f"Póliza {escape(e.numero_poliza)}", estilos["texto"]),
            Spacer(1, 8),
            _tabla(
                [
                    ["Firmante (identificador)", e.firmante_id],
                    ["Fecha y hora de la firma", f"{fmt.fecha(e.firmada_en)}, {e.firmada_en.astimezone(fmt.BOGOTA):%H:%M:%S} (hora de Colombia)"],
                    ["Canal", CANALES.get(e.canal, e.canal)],
                    ["Dirección IP", e.ip or "No disponible"],
                    ["Dispositivo", e.dispositivo or "No disponible"],
                    ["Autenticación", e.metodo_autenticacion],
                    ["Proveedor de firma", e.proveedor],
                    ["Referencia del proveedor", e.referencia_proveedor or "No disponible"],
                    ["SHA-256 del documento firmado", e.documento_sha256],
                ],
                [5.5 * cm, 10.5 * cm],
                estilos,
            ),
            Spacer(1, 8),
            Paragraph(
                "El hash corresponde al documento de la póliza tal como se presentó al firmante, sin esta página. "
                "Firma generada con un proveedor simulado: no tiene validez legal certificada.",
                estilos["texto"],
            ),
        ]
    )
    return salida.getvalue()


def sellar_documento(documento: bytes, evidencia: EvidenciaFirma) -> bytes:
    """El PDF de la póliza con la constancia como última página."""
    escritor = PdfWriter()
    for pagina in PdfReader(BytesIO(documento)).pages:
        escritor.add_page(pagina)
    for pagina in PdfReader(BytesIO(_constancia(evidencia))).pages:
        escritor.add_page(pagina)
    escritor.add_metadata({"/Title": f"Póliza firmada {evidencia.numero_poliza}", "/Author": "Solventa"})
    salida = BytesIO()
    escritor.write(salida)
    return salida.getvalue()
