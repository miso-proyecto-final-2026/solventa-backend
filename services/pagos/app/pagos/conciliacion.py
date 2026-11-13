"""Registro y conciliación de los desembolsos de indemnizaciones (HU31).

Cada indemnización (`pagos.indemnizaciones`, HU29) guarda el evento, la póliza y la validación que viajaron en el comando y el pago
con su referencia y clave de idempotencia. Esta vista la usa operaciones para:

- listar y filtrar los desembolsos por fecha, tipo de evento y estado de conciliación;
- **conciliar**: compara el desembolso con el extracto de la pasarela (hoy simulado, a confirmar). Si el monto y la referencia
  coinciden queda `CONCILIADO` con quién y cuándo; si el monto no coincide queda `CON_DIFERENCIA`; si la pasarela no tiene el pago,
  no cambia nada y se avisa;
- **escalar** un desembolso con diferencia al equipo de pagos, con una nota;
- exportar el reporte (CSV y PDF) y comprobar que no hubo pagos duplicados por la misma clave.

El `cliente_id` sale enmascarado (a confirmar). Nunca se devuelve la cuenta de destino.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.pagos.acceso import Gestor
from app.pagos.dominio import EstadoConciliacion, EstadoIndemnizacion, PasarelaPago
from app.pagos.errores import (
    DesembolsoNoConciliable,
    DesembolsoNoEncontrado,
    DesembolsoSinDiferencia,
    ExtractoSinPago,
    SolicitudInvalida,
)
from app.pagos.modelos_db import IndemnizacionDB, IntentoPagoDB

log = logging.getLogger("pagos.conciliacion")

LIMITE_EXPORTACION = 10_000
MAX_NOTA = 500


def _ahora() -> datetime:
    return datetime.now(UTC)


def _utc(valor: datetime | None) -> datetime | None:
    return valor if valor is None or valor.tzinfo else valor.replace(tzinfo=UTC)


def enmascarar(valor: str | None) -> str | None:
    """`cli-12345678` → `cli***78`: se reconoce, pero no se lee entero (HU31, criterio 9, a confirmar)."""
    if not valor:
        return None
    return "***" if len(valor) <= 5 else f"{valor[:3]}***{valor[-2:]}"


@dataclass(frozen=True)
class Filtros:
    desde: datetime | None = None
    hasta: datetime | None = None
    tipo_evento: str | None = None
    estado_conciliacion: str | None = None
    estado: str | None = None  # estado del pago


@dataclass(frozen=True)
class Desembolso:
    """Lo que ve operaciones de una indemnización. Sin cuenta de destino y con el cliente enmascarado."""

    id: str
    idempotency_key: str
    evento_id: str
    tipo_evento: str | None
    magnitud: dict[str, Any] | None
    ubicacion: str | None
    evento_ocurrido_en: datetime | None
    poliza_id: str
    producto: str | None
    cliente: str | None
    numero_poliza: str | None
    monto_asegurado: Any
    validacion: dict[str, Any] | None
    estado: str
    monto: Decimal
    moneda: str
    referencia_pasarela: str | None
    fecha_pago: datetime | None
    intentos: int
    reentregas: int
    estado_conciliacion: str
    conciliado_por: str | None
    conciliado_en: datetime | None
    diferencia: dict[str, Any] | None
    escalada_en: datetime | None
    escalada_por: str | None
    nota_escalamiento: str | None
    codigo_fallo: str | None
    motivo_fallo: str | None


@dataclass(frozen=True)
class Resumen:
    desembolsos: int
    claves_distintas: int
    claves_con_mas_de_un_pago: int
    pagados: int
    monto_pagado: Decimal
    por_estado_conciliacion: dict[str, int]


def _vista(ind: IndemnizacionDB) -> Desembolso:
    poliza = ind.datos_poliza or {}
    evento = ind.datos_evento or {}
    return Desembolso(
        id=ind.id,
        idempotency_key=ind.idempotency_key,
        evento_id=ind.evento_id,
        tipo_evento=ind.tipo_evento,
        magnitud=evento.get("magnitud"),
        ubicacion=ind.ubicacion,
        evento_ocurrido_en=_utc(ind.evento_ocurrido_en),
        poliza_id=ind.poliza_id,
        producto=ind.producto or poliza.get("producto"),
        cliente=enmascarar(ind.cliente_id),
        numero_poliza=poliza.get("numero"),
        monto_asegurado=poliza.get("monto_asegurado"),
        validacion=ind.validacion,
        estado=ind.estado,
        monto=Decimal(ind.monto),
        moneda=ind.moneda,
        referencia_pasarela=ind.referencia_pasarela,
        fecha_pago=_utc(ind.resuelta_en),
        intentos=ind.intentos,
        reentregas=ind.reentregas,
        estado_conciliacion=ind.estado_conciliacion,
        conciliado_por=ind.conciliado_por_nombre or ind.conciliado_por,
        conciliado_en=_utc(ind.conciliado_en),
        diferencia=ind.diferencia,
        escalada_en=_utc(ind.escalada_en),
        escalada_por=ind.escalada_por,
        nota_escalamiento=ind.nota_escalamiento,
        codigo_fallo=ind.codigo_fallo,
        motivo_fallo=ind.motivo_fallo,
    )


def _consulta(filtros: Filtros) -> Select[Any]:
    consulta = select(IndemnizacionDB)
    fecha = func.coalesce(IndemnizacionDB.resuelta_en, IndemnizacionDB.created_at)
    if filtros.desde is not None:
        consulta = consulta.where(fecha >= filtros.desde)
    if filtros.hasta is not None:
        consulta = consulta.where(fecha <= filtros.hasta)
    if filtros.tipo_evento:
        consulta = consulta.where(IndemnizacionDB.tipo_evento == filtros.tipo_evento)
    if filtros.estado_conciliacion:
        consulta = consulta.where(IndemnizacionDB.estado_conciliacion == filtros.estado_conciliacion)
    if filtros.estado:
        consulta = consulta.where(IndemnizacionDB.estado == filtros.estado)
    return consulta


class ServicioConciliacion:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        pasarela: PasarelaPago,
        timeout_pasarela_s: float = 5.0,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._pasarela = pasarela
        self._timeout_s = timeout_pasarela_s
        self._reloj = reloj

    def ahora(self) -> datetime:
        return self._reloj()

    # --- consultas ------------------------------------------------------------------------------------------------

    def listar(self, filtros: Filtros, *, pagina: int = 1, tamano: int = 25) -> tuple[list[Desembolso], int]:
        orden = func.coalesce(IndemnizacionDB.resuelta_en, IndemnizacionDB.created_at).desc()
        with self._fabrica() as s:
            total = s.execute(select(func.count()).select_from(_consulta(filtros).subquery())).scalar_one()
            filas = s.execute(
                _consulta(filtros).order_by(orden, IndemnizacionDB.id).offset((pagina - 1) * tamano).limit(tamano)
            ).scalars()
            return [_vista(i) for i in filas], total

    def obtener(self, desembolso_id: str) -> tuple[Desembolso, list[dict[str, Any]]]:
        """El desembolso y el rastro de sus intentos de pago (los estados por los que pasó)."""
        with self._fabrica() as s:
            ind = s.get(IndemnizacionDB, desembolso_id)
            if ind is None:
                raise DesembolsoNoEncontrado
            intentos = s.execute(
                select(IntentoPagoDB).where(IntentoPagoDB.indemnizacion_id == desembolso_id).order_by(IntentoPagoDB.numero)
            ).scalars()
            historia = [
                {
                    "numero": i.numero,
                    "resultado": i.resultado,
                    "codigo": i.codigo,
                    "motivo": i.motivo,
                    "referencia_pasarela": i.referencia_pasarela,
                    "fecha": _utc(i.created_at),
                }
                for i in intentos
            ]
            return _vista(ind), historia

    def resumen(self, filtros: Filtros) -> Resumen:
        """Criterio 8: cuántos desembolsos hay, cuántas claves distintas y si alguna clave tiene más de un pago."""
        base = _consulta(filtros).subquery()
        with self._fabrica() as s:
            desembolsos, distintas = s.execute(
                select(func.count(), func.count(func.distinct(base.c.idempotency_key))).select_from(base)
            ).one()
            pagados, monto = s.execute(
                select(func.count(), func.coalesce(func.sum(base.c.monto), 0)).where(
                    base.c.estado == EstadoIndemnizacion.PAGADA.value
                )
            ).one()
            repetidas = s.execute(
                select(func.count()).select_from(
                    select(IndemnizacionDB.idempotency_key)
                    .where(IndemnizacionDB.estado == EstadoIndemnizacion.PAGADA.value)
                    .group_by(IndemnizacionDB.idempotency_key)
                    .having(func.count() > 1)
                    .subquery()
                )
            ).scalar_one()
            por_estado = dict(
                s.execute(select(base.c.estado_conciliacion, func.count()).group_by(base.c.estado_conciliacion)).all()
            )
        return Resumen(
            desembolsos=desembolsos,
            claves_distintas=distintas,
            claves_con_mas_de_un_pago=repetidas,
            pagados=pagados,
            monto_pagado=Decimal(monto),
            por_estado_conciliacion={e.value: por_estado.get(e.value, 0) for e in EstadoConciliacion},
        )

    # --- conciliar y escalar --------------------------------------------------------------------------------------

    async def conciliar(self, desembolso_id: str, operador: Gestor) -> Desembolso:
        """Verifica el desembolso contra el extracto de la pasarela y deja el resultado y quién lo hizo."""
        clave, monto, referencia = await asyncio.to_thread(self._datos_para_conciliar, desembolso_id)
        linea = await asyncio.wait_for(self._pasarela.extracto(clave), self._timeout_s)
        if linea is None:
            raise ExtractoSinPago
        ahora = self._reloj()

        def guardar() -> Desembolso:
            with self._fabrica() as s, s.begin():
                ind = s.get(IndemnizacionDB, desembolso_id, with_for_update=True)
                coincide = linea.monto == monto and linea.moneda == ind.moneda and linea.referencia == referencia
                ind.estado_conciliacion = (
                    EstadoConciliacion.CONCILIADO if coincide else EstadoConciliacion.CON_DIFERENCIA
                ).value
                ind.conciliado_por, ind.conciliado_por_nombre, ind.conciliado_en = operador.id, operador.nombre or None, ahora
                ind.diferencia = None if coincide else {
                    "monto_registrado": str(monto),
                    "monto_pasarela": str(linea.monto),
                    "moneda_pasarela": linea.moneda,
                    "referencia_registrada": referencia,
                    "referencia_pasarela": linea.referencia,
                }
                if coincide:
                    ind.escalada_en = ind.escalada_por = ind.nota_escalamiento = None
                ind.updated_at = ahora
                log.info(
                    "desembolso conciliado desembolso_id=%s estado=%s operador_id=%s",
                    ind.id, ind.estado_conciliacion, operador.id,
                )
                return _vista(ind)

        return await asyncio.to_thread(guardar)

    def _datos_para_conciliar(self, desembolso_id: str) -> tuple[str, Decimal, str | None]:
        with self._fabrica() as s:
            ind = s.get(IndemnizacionDB, desembolso_id)
            if ind is None:
                raise DesembolsoNoEncontrado
            if ind.estado != EstadoIndemnizacion.PAGADA.value:
                raise DesembolsoNoConciliable
            return ind.idempotency_key, Decimal(ind.monto), ind.referencia_pasarela

    def escalar(self, desembolso_id: str, operador: Gestor, nota: str) -> Desembolso:
        """Pasa un desembolso con diferencia al equipo de pagos. Exige una nota; queda quién y cuándo."""
        nota = (nota or "").strip()
        if not 1 <= len(nota) <= MAX_NOTA:
            raise SolicitudInvalida(detalles=[{"campo": "nota", "motivo": f"es obligatoria, de 1 a {MAX_NOTA} caracteres"}])
        with self._fabrica() as s, s.begin():
            ind = s.get(IndemnizacionDB, desembolso_id, with_for_update=True)
            if ind is None:
                raise DesembolsoNoEncontrado
            if ind.estado_conciliacion != EstadoConciliacion.CON_DIFERENCIA.value:
                raise DesembolsoSinDiferencia
            ahora = self._reloj()
            ind.escalada_en, ind.escalada_por, ind.nota_escalamiento, ind.updated_at = ahora, operador.id, nota, ahora
            log.info("desembolso escalado desembolso_id=%s operador_id=%s", ind.id, operador.id)
            return _vista(ind)

    # --- exportar ---------------------------------------------------------------------------------------------------

    def filas_para_exportar(self, filtros: Filtros) -> list[Desembolso]:
        with self._fabrica() as s:
            filas = s.execute(
                _consulta(filtros)
                .order_by(func.coalesce(IndemnizacionDB.resuelta_en, IndemnizacionDB.created_at).desc(), IndemnizacionDB.id)
                .limit(LIMITE_EXPORTACION)
            ).scalars()
            return [_vista(i) for i in filas]


COLUMNAS = (
    ("Clave de idempotencia", lambda d: d.idempotency_key),
    ("Evento", lambda d: d.evento_id),
    ("Tipo de evento", lambda d: d.tipo_evento),
    ("Magnitud", lambda d: _texto_magnitud(d.magnitud)),
    ("Ubicación", lambda d: d.ubicacion),
    ("Fecha del evento", lambda d: _iso(d.evento_ocurrido_en)),
    ("Póliza", lambda d: d.poliza_id),
    ("Producto", lambda d: d.producto),
    ("Cliente", lambda d: d.cliente),
    ("Validación", lambda d: (d.validacion or {}).get("resultado")),
    ("Estado del pago", lambda d: d.estado),
    ("Monto", lambda d: f"{d.monto:.2f}"),
    ("Moneda", lambda d: d.moneda),
    ("Referencia", lambda d: d.referencia_pasarela),
    ("Fecha del pago", lambda d: _iso(d.fecha_pago)),
    ("Conciliación", lambda d: d.estado_conciliacion),
    ("Conciliado por", lambda d: d.conciliado_por),
    ("Conciliado en", lambda d: _iso(d.conciliado_en)),
    ("Nota de escalamiento", lambda d: d.nota_escalamiento),
)


def _iso(fecha: datetime | None) -> str | None:
    return fecha.isoformat() if fecha else None


def _texto_magnitud(magnitud: dict[str, Any] | None) -> str | None:
    return None if not magnitud else "; ".join(f"{k}={v}" for k, v in sorted(magnitud.items()))


def _seguro(valor: Any) -> str:
    """Un campo que empieza por `=`, `+`, `-` o `@` se interpretaría como fórmula al abrir el CSV en una hoja de cálculo."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto


def a_csv(filas: list[Desembolso]) -> bytes:
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow([nombre for nombre, _ in COLUMNAS])
    for fila in filas:
        escritor.writerow([_seguro(extraer(fila)) for _, extraer in COLUMNAS])
    return ("﻿" + salida.getvalue()).encode("utf-8")  # con BOM para que Excel lea las tildes


def a_pdf(filas: list[Desembolso], resumen: Resumen, generado_en: datetime) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    elegidas = ("Evento", "Tipo de evento", "Póliza", "Cliente", "Estado del pago", "Monto", "Referencia", "Conciliación", "Conciliado por")
    indices = [i for i, (nombre, _) in enumerate(COLUMNAS) if nombre in elegidas]
    estilos = getSampleStyleSheet()
    pequeno = estilos["BodyText"].clone("pequeno", fontSize=7, leading=8)
    tabla = [[COLUMNAS[i][0] for i in indices]] + [
        [Paragraph(_seguro_pdf(COLUMNAS[i][1](fila)), pequeno) for i in indices] for fila in filas
    ]
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    cuerpo = [
        Paragraph("Conciliación de desembolsos", estilos["Title"]),
        Paragraph(
            f"Generado: {generado_en.isoformat(timespec='seconds')} · Desembolsos: {resumen.desembolsos} · "
            f"Claves distintas: {resumen.claves_distintas} · Claves con más de un pago: {resumen.claves_con_mas_de_un_pago} · "
            f"Monto pagado: {resumen.monto_pagado:.2f}",
            estilos["BodyText"],
        ),
        Spacer(1, 8),
    ]
    if filas:
        t = Table(tabla, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        cuerpo.append(t)
    else:
        cuerpo.append(Paragraph("No hay desembolsos con esos filtros.", estilos["BodyText"]))
    doc.build(cuerpo)
    return buffer.getvalue()


def _seguro_pdf(valor: Any) -> str:
    from xml.sax.saxutils import escape

    return escape("" if valor is None else str(valor))
