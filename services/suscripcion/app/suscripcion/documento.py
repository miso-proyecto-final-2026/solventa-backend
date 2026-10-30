"""Documento de la póliza (HU20): plantilla versionada y renderizado del PDF.

`renderizar_poliza` es una función pura: recibe los datos ya armados y devuelve los bytes del PDF, sin red ni base de datos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import yaml
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.suscripcion import formato_es_co as fmt

log = logging.getLogger("suscripcion.documento")

DIRECTORIO = Path(__file__).resolve().parents[2] / "plantillas" / "poliza"
VERSION_VIGENTE = "v1"
PERIODICIDADES = {"ANUAL": "Anual", "MENSUAL": "Mensual"}


@dataclass(frozen=True)
class Plantilla:
    version: str
    productos: dict[str, dict[str, str]]
    amparos: dict[str, str]
    naturaleza_generica: str
    condiciones_generales: tuple[str, ...]

    def producto(self, clave: str) -> tuple[str, str]:
        """(nombre, naturaleza del riesgo). Un producto desconocido no falla: usa la clave y un texto genérico."""
        if clave in self.productos:
            p = self.productos[clave]
            return p["nombre"], p["naturaleza_riesgo"]
        log.warning("producto sin plantilla producto=%s version=%s", clave, self.version)
        return clave, self.naturaleza_generica

    def amparo(self, tipo: str) -> str:
        return self.amparos.get(tipo, tipo.replace("_", " ").capitalize())


def cargar_plantilla(version: str = VERSION_VIGENTE, directorio: Path = DIRECTORIO) -> Plantilla:
    datos = yaml.safe_load((directorio / f"{version}.yaml").read_text(encoding="utf-8"))
    return Plantilla(
        version=str(datos["version"]),
        productos={k: {"nombre": v["nombre"], "naturaleza_riesgo": v["naturaleza_riesgo"]} for k, v in datos["productos"].items()},
        amparos=dict(datos["amparos"]),
        naturaleza_generica=datos["generico"]["naturaleza_riesgo"],
        condiciones_generales=tuple(datos["condiciones_generales"]),
    )


@dataclass(frozen=True)
class CoberturaDocumento:
    tipo: str
    monto: int
    deducible: int


@dataclass(frozen=True)
class DatosPoliza:
    numero_poliza: str
    generada_en: datetime
    vigencia_desde: datetime
    vigencia_hasta: datetime
    cliente_nombre: str
    cliente_tipo_documento: str
    cliente_documento_enmascarado: str
    producto: str
    monto_asegurado: int
    coberturas: tuple[CoberturaDocumento, ...]
    prima_anual: int
    prima_mensual: int
    periodicidad_pago: str
    referencia_cobro: str | None


def _estilos() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle("titulo", parent=base["Title"], fontSize=18, spaceAfter=4),
        "seccion": ParagraphStyle("seccion", parent=base["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4),
        "texto": ParagraphStyle("texto", parent=base["BodyText"], fontSize=10, leading=13),
    }


def _tabla(filas: list[list[str]], anchos: list[float], estilos: dict[str, ParagraphStyle], cabecera: bool = False) -> Table:
    celdas = [[Paragraph(escape(c), estilos["texto"]) for c in fila] for fila in filas]
    tabla = Table(celdas, colWidths=anchos, hAlign="LEFT")
    comandos = [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    if cabecera:
        comandos.append(("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke))
    tabla.setStyle(TableStyle(comandos))
    return tabla


def renderizar_poliza(datos: DatosPoliza, plantilla: Plantilla) -> bytes:
    nombre_producto, naturaleza = plantilla.producto(datos.producto)
    e = _estilos()
    historia: list = [
        Paragraph("Póliza de seguro", e["titulo"]),
        Paragraph(escape(nombre_producto), e["texto"]),
        Spacer(1, 6),
        _tabla(
            [
                ["Número de póliza", datos.numero_poliza],
                ["Fecha de generación", fmt.fecha(datos.generada_en)],
                ["Vigencia", f"Desde el {fmt.fecha(datos.vigencia_desde)} hasta el {fmt.fecha(datos.vigencia_hasta)}"],
            ],
            [5 * cm, 11 * cm],
            e,
        ),
        Paragraph("Datos del cliente", e["seccion"]),
        _tabla(
            [
                ["Nombre completo", datos.cliente_nombre],
                ["Tipo de documento", datos.cliente_tipo_documento],
                ["Documento", datos.cliente_documento_enmascarado],
            ],
            [5 * cm, 11 * cm],
            e,
        ),
        Paragraph("Tipo de póliza", e["seccion"]),
        Paragraph(escape(nombre_producto), e["texto"]),
        Paragraph("Valor asegurado del bien", e["seccion"]),
        Paragraph(fmt.monto(datos.monto_asegurado), e["texto"]),
        Paragraph("Naturaleza del riesgo", e["seccion"]),
        Paragraph(escape(naturaleza), e["texto"]),
        Spacer(1, 4),
        Paragraph(
            "Amparos cubiertos: " + escape(", ".join(plantilla.amparo(c.tipo) for c in datos.coberturas) or "ninguno") + ".",
            e["texto"],
        ),
        Paragraph("Coberturas", e["seccion"]),
        _tabla(
            [["Amparo", "Monto", "Deducible"]]
            + [
                [plantilla.amparo(c.tipo), fmt.monto(c.monto), fmt.monto(c.deducible) if c.deducible else "No aplica"]
                for c in datos.coberturas
            ],
            [7 * cm, 4.5 * cm, 4.5 * cm],
            e,
            cabecera=True,
        ),
        Paragraph("Prima", e["seccion"]),
        _tabla(
            [
                ["Prima anual", fmt.monto(datos.prima_anual)],
                ["Prima mensual", fmt.monto(datos.prima_mensual)],
                ["Periodicidad de pago", PERIODICIDADES.get(datos.periodicidad_pago, datos.periodicidad_pago)],
                ["Referencia del cobro", datos.referencia_cobro or "No disponible"],
            ],
            [5 * cm, 11 * cm],
            e,
        ),
        Paragraph("Condiciones generales", e["seccion"]),
    ]
    for parrafo in plantilla.condiciones_generales:
        historia += [Paragraph(escape(parrafo), e["texto"]), Spacer(1, 4)]

    salida = BytesIO()
    SimpleDocTemplate(
        salida,
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"Póliza {datos.numero_poliza}",
        author="Solventa",
    ).build(historia)
    return salida.getvalue()
