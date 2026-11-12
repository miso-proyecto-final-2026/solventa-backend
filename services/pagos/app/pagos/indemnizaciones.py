"""Pago automático de la indemnización paramétrica (HU29): registrar, pagar y conciliar sin pagar dos veces.

Es el mismo patrón del cobro de la prima (`servicio.py`), con su propia tabla:

1. **Registrar** el comando `indemnizacion_solicitada` de Siniestros: una indemnización `PENDIENTE` por `idempotency_key`
   (restricción única; también por par evento–póliza). Una repetición no crea nada: si ya terminó, vuelve a anunciar el
   mismo resultado. No se llama a la pasarela aquí: el consumidor deshace la transacción si falla, pero un pago no se deshace.
2. **Pagar**: se toma con `FOR UPDATE SKIP LOCKED`, se anota el intento y se confirma *antes* de llamar. La orden lleva la
   misma `idempotency_key`, de modo que la pasarela tampoco paga dos veces.
3. **Conciliar**: si no hubo respuesta, el siguiente intento primero consulta la pasarela por la clave.

Sin respuesta se reintenta con retroceso exponencial; agotados los intentos (o ante un rechazo) queda `FALLIDA`, visible, y
se publica `indemnizacion_fallida`. El resultado sale por la cola de salida en la misma transacción que lo registra, hacia
`solventa.pagos.indemnizaciones` con clave de partición `evento_id`. Ningún evento ni log lleva la cuenta de destino.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.contexto import correlacion_id, traza_id
from app.eventos import Evento
from app.pagos import repositorio as repo
from app.pagos.config import MONEDA, TOPIC_INDEMNIZACIONES
from app.pagos.dominio import (
    ESTADOS_FINALES_INDEMNIZACION,
    EstadoIndemnizacion,
    PasarelaNoDisponible,
    PasarelaPago,
    PasarelaSinRespuesta,
    ResultadoCobro,
    ResultadoIntento,
    SolicitudPago,
    identificador_parece_pan,
)
from app.pagos.errores import ClaveReutilizada, SolicitudInvalida
from app.pagos.modelos_db import IndemnizacionDB, IntentoPagoDB

log = logging.getLogger("pagos.indemnizaciones")

FUENTE = "ms-pagos"
TIPO_COMANDO = "indemnizacion_solicitada"
PAGADA = "indemnizacion_pagada"
FALLIDA = "indemnizacion_fallida"
INTENTOS_AGOTADOS = "INTENTOS_AGOTADOS"


def _ahora() -> datetime:
    return datetime.now(UTC)


def cuenta_por_defecto(poliza_id: str) -> str:
    """PROVISIONAL (S5): ninguna historia captura la cuenta del cliente. Hasta que exista, el pago va a una cuenta
    simulada opaca derivada de la póliza (no contiene datos de nadie).

    El hash se parte con una letra cada 6 caracteres: un hexadecimal puede traer 13 o más dígitos seguidos, y eso tiene
    forma de tarjeta (`identificador_parece_pan`), así que el propio comando se rechazaría y el pago se perdería."""
    huella = hashlib.sha256(poliza_id.encode()).hexdigest()[:24]
    return "cta_sim_" + "x".join(huella[i : i + 6] for i in range(0, 24, 6))


@dataclass(frozen=True)
class ComandoIndemnizacion:
    idempotency_key: str
    evento_id: str
    poliza_id: str
    monto: Decimal
    moneda: str
    cuenta_destino: str
    cliente_id: str | None = None
    producto: str | None = None
    regla: str | None = None
    version_reglas: str | None = None

    @classmethod
    def desde_evento(cls, payload: dict[str, Any]) -> ComandoIndemnizacion:
        """Valida `indemnizacion_solicitada` (contrato de ADR-0018). `cuenta_destino` y `cliente_id` son opcionales."""
        faltan = [c for c in ("evento_id", "poliza_id") if not payload.get(c)]
        bruto = payload.get("monto_indemnizacion", payload.get("monto"))
        if bruto is None:
            faltan.append("monto_indemnizacion")
        if faltan:
            raise SolicitudInvalida(detalles=[{"campo": c, "motivo": "es obligatorio"} for c in faltan])
        evento_id, poliza_id = str(payload["evento_id"]), str(payload["poliza_id"])
        return cls.validado(
            idempotency_key=str(payload.get("idempotency_key") or f"{evento_id}:{poliza_id}"),
            evento_id=evento_id,
            poliza_id=poliza_id,
            monto=bruto,
            moneda=str(payload.get("moneda") or MONEDA),
            cuenta_destino=str(payload.get("cuenta_destino") or cuenta_por_defecto(poliza_id)),
            cliente_id=_opcional(payload.get("cliente_id")),
            producto=_opcional(payload.get("producto")),
            regla=_opcional(payload.get("regla")),
            version_reglas=_opcional(payload.get("version_reglas")),
        )

    @classmethod
    def validado(cls, *, monto: Any, **resto: Any) -> ComandoIndemnizacion:
        try:
            valor = Decimal(str(monto)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise SolicitudInvalida(detalles=[{"campo": "monto_indemnizacion", "motivo": "no es un número"}]) from None
        defectos = []
        if not valor.is_finite() or valor <= 0 or valor >= Decimal("1e13"):
            defectos.append({"campo": "monto_indemnizacion", "motivo": "debe ser mayor que 0 y caber en NUMERIC(15,2)"})
        if resto["moneda"] != MONEDA:
            defectos.append({"campo": "moneda", "motivo": f"solo se paga en {MONEDA}"})
        if identificador_parece_pan(resto["cuenta_destino"]):
            # El valor no se copia al detalle ni al log: se informa solo el campo.
            defectos.append({"campo": "cuenta_destino", "motivo": "debe ser el token de la cuenta, nunca sus datos"})
        if not 1 <= len(resto["idempotency_key"]) <= 100:
            defectos.append({"campo": "idempotency_key", "motivo": "debe tener de 1 a 100 caracteres"})
        if defectos:
            raise SolicitudInvalida(detalles=defectos)
        return cls(monto=valor, **resto)


def _opcional(valor: Any) -> str | None:
    return None if valor is None or valor == "" else str(valor)


class ServicioIndemnizaciones:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        pasarela: PasarelaPago,
        timeout_pasarela_s: float = 5.0,
        reintento_base_s: float = 5.0,
        reintento_maximo_s: float = 300.0,
        max_intentos: int = 5,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        if max_intentos < 1:
            raise ValueError("max_intentos debe ser al menos 1")
        self._fabrica = fabrica
        self._pasarela = pasarela
        self._timeout_s = timeout_pasarela_s
        self._base_s = reintento_base_s
        self._maximo_s = reintento_maximo_s
        self._max_intentos = max_intentos
        self._reloj = reloj
        # Mientras una réplica espera a la pasarela, la indemnización queda «prestada» y las demás no la toman.
        self._prestamo = timedelta(seconds=timeout_pasarela_s * 2 + 5)

    # --- 1. registrar ---------------------------------------------------------------------------------------------

    async def manejar_evento(self, evento: Evento, sesion: Session) -> None:
        """Manejador del consumidor de `solventa.siniestros.comandos` (corre en la transacción de la plantilla)."""
        if evento.event_type != TIPO_COMANDO:
            log.info("evento ignorado event_type=%s event_id=%s", evento.event_type, evento.event_id)
            return
        traza_id.set(evento.trace_id)
        correlacion_id.set(evento.correlation_id)
        self.registrar(sesion, ComandoIndemnizacion.desde_evento(evento.payload))

    def registrar(self, sesion: Session, comando: ComandoIndemnizacion) -> tuple[IndemnizacionDB, bool]:
        """Devuelve (indemnización, creada). Con `creada=False` es una repetición: se cuenta y no se paga otra vez."""
        existente = self._existente(sesion, comando)
        if existente is None:
            ahora = self._reloj()
            nueva = IndemnizacionDB(
                id=str(uuid4()),
                idempotency_key=comando.idempotency_key,
                evento_id=comando.evento_id,
                poliza_id=comando.poliza_id,
                cliente_id=comando.cliente_id,
                producto=comando.producto,
                regla=comando.regla,
                version_reglas=comando.version_reglas,
                monto=comando.monto,
                moneda=comando.moneda,
                cuenta_destino=comando.cuenta_destino,
                estado=EstadoIndemnizacion.PENDIENTE.value,
                intentos=0,
                proximo_intento_en=ahora,
                reentregas=0,
                correlacion_id=correlacion_id.get(),
                traza_id=traza_id.get(),
                created_at=ahora,
                updated_at=ahora,
            )
            if _insertar(sesion, nueva):
                log.info(
                    "indemnizacion registrada indemnizacion_id=%s evento_id=%s poliza_id=%s",
                    nueva.id, nueva.evento_id, nueva.poliza_id,
                )
                return nueva, True
            existente = self._existente(sesion, comando)  # otra réplica ganó la carrera
        return self._repeticion(sesion, existente, comando), False

    def _existente(self, sesion: Session, comando: ComandoIndemnizacion) -> IndemnizacionDB | None:
        return sesion.execute(
            select(IndemnizacionDB).where(
                (IndemnizacionDB.idempotency_key == comando.idempotency_key)
                | ((IndemnizacionDB.evento_id == comando.evento_id) & (IndemnizacionDB.poliza_id == comando.poliza_id))
            )
        ).scalars().first()

    def _repeticion(self, sesion: Session, ind: IndemnizacionDB, comando: ComandoIndemnizacion) -> IndemnizacionDB:
        if (ind.evento_id, ind.poliza_id, Decimal(ind.monto)) != (comando.evento_id, comando.poliza_id, comando.monto):
            raise ClaveReutilizada
        ind = _contar_reentrega(sesion, ind, self._reloj())
        log.info("indemnizacion repetida indemnizacion_id=%s estado=%s reentregas=%d", ind.id, ind.estado, ind.reentregas)
        if ind.estado in ESTADOS_FINALES_INDEMNIZACION:
            # La segunda respuesta es la de la primera: se vuelve a anunciar el mismo resultado, sin pagar.
            self._encolar_resultado(sesion, ind, repeticion=True)
        return ind

    # --- 2. pagar y 3. conciliar ----------------------------------------------------------------------------------

    async def procesar(self, indemnizacion_id: str) -> IndemnizacionDB | None:
        """Un intento sobre la indemnización. None si no le toca o la tiene otra réplica. Seguro de repetir."""
        tomado = await asyncio.to_thread(self._tomar, indemnizacion_id)
        if tomado is None:
            return None
        numero, solicitud = tomado
        inicio = time.monotonic()
        resultado: ResultadoCobro | None = None
        desenlace = ResultadoIntento.SIN_RESPUESTA
        try:
            if numero > 1:
                # Un intento anterior no supo si pagó: primero se pregunta (EC-D1.2).
                resultado = await asyncio.wait_for(self._pasarela.consultar(solicitud.idempotency_key), self._timeout_s)
                if resultado is not None:
                    desenlace = ResultadoIntento.CONCILIADO
            if resultado is None:
                resultado = await asyncio.wait_for(self._pasarela.pagar(solicitud), self._timeout_s)
                desenlace = ResultadoIntento.APROBADO if resultado.aprobado else ResultadoIntento.RECHAZADO
        except PasarelaNoDisponible as error:
            desenlace = ResultadoIntento.NO_DISPONIBLE
            log.warning("pasarela no disponible indemnizacion_id=%s intento=%d: %s", indemnizacion_id, numero, error)
        except (PasarelaSinRespuesta, TimeoutError) as error:
            log.warning("pasarela sin respuesta indemnizacion_id=%s intento=%d: %s", indemnizacion_id, numero, type(error).__name__)
        except Exception:  # un fallo desconocido puede haber pagado: se trata como «sin respuesta»
            log.exception("fallo al pagar indemnizacion_id=%s intento=%d", indemnizacion_id, numero)
        duracion_ms = int((time.monotonic() - inicio) * 1000)
        return await asyncio.to_thread(self._cerrar_intento, indemnizacion_id, numero, desenlace, resultado, duracion_ms)

    def _tomar(self, indemnizacion_id: str) -> tuple[int, SolicitudPago] | None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            ind = s.execute(
                select(IndemnizacionDB)
                .where(
                    IndemnizacionDB.id == indemnizacion_id,
                    IndemnizacionDB.estado == EstadoIndemnizacion.PENDIENTE.value,
                    IndemnizacionDB.proximo_intento_en <= ahora,
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if ind is None:
                return None
            ind.intentos += 1
            s.add(
                IntentoPagoDB(
                    indemnizacion_id=ind.id,
                    numero=ind.intentos,
                    resultado=ResultadoIntento.EN_CURSO.value,
                    created_at=ahora,
                    updated_at=ahora,
                )
            )
            ind.proximo_intento_en = ahora + self._prestamo
            ind.updated_at = ahora
            solicitud = SolicitudPago(
                idempotency_key=ind.idempotency_key,
                cuenta_destino=ind.cuenta_destino,
                monto=Decimal(ind.monto),
                moneda=ind.moneda,
                referencia=ind.evento_id,
            )
            return ind.intentos, solicitud

    def _cerrar_intento(
        self,
        indemnizacion_id: str,
        numero: int,
        desenlace: ResultadoIntento,
        resultado: ResultadoCobro | None,
        duracion_ms: int,
    ) -> IndemnizacionDB | None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            ind = s.get(IndemnizacionDB, indemnizacion_id, with_for_update=True)
            intento = s.execute(
                select(IntentoPagoDB).where(
                    IntentoPagoDB.indemnizacion_id == indemnizacion_id, IntentoPagoDB.numero == numero
                )
            ).scalar_one()
            intento.resultado = desenlace.value
            intento.duracion_ms = duracion_ms
            intento.updated_at = ahora
            if resultado is not None:
                intento.referencia_pasarela = resultado.referencia_pasarela
                if not resultado.aprobado:
                    intento.codigo = (resultado.codigo or "RECHAZADO")[:50]
                    intento.motivo = (resultado.motivo or "")[:200] or None
            if ind.estado in ESTADOS_FINALES_INDEMNIZACION:
                return ind  # ya resuelta (no debería ocurrir: estaba prestada a esta réplica)
            if resultado is None and numero < self._max_intentos:
                espera = min(self._maximo_s, self._base_s * 2 ** (numero - 1))
                ind.proximo_intento_en = ahora + timedelta(seconds=espera)
                ind.updated_at = ahora
                log.info("pago sin resolver indemnizacion_id=%s intento=%d reintento_en_s=%s", ind.id, numero, espera)
                return ind
            if resultado is not None and resultado.aprobado:
                ind.estado = EstadoIndemnizacion.PAGADA.value
                ind.referencia_pasarela = resultado.referencia_pasarela
            else:
                ind.estado = EstadoIndemnizacion.FALLIDA.value
                if resultado is None:
                    ind.codigo_fallo = INTENTOS_AGOTADOS
                    ind.motivo_fallo = f"La pasarela no respondió tras {numero} intentos."
                else:
                    ind.referencia_pasarela = resultado.referencia_pasarela
                    ind.codigo_fallo = (resultado.codigo or "RECHAZADO")[:50]
                    ind.motivo_fallo = (resultado.motivo or "La pasarela rechazó el pago.")[:200]
            ind.proximo_intento_en = None
            ind.resuelta_en = ahora
            ind.updated_at = ahora
            self._encolar_resultado(s, ind, repeticion=False)
        log.info(
            "indemnizacion resuelta indemnizacion_id=%s estado=%s intento=%d desenlace=%s duracion_ms=%d",
            indemnizacion_id, ind.estado, numero, desenlace.value, duracion_ms,
        )
        return ind

    async def procesar_pendientes(self, limite: int = 20) -> int:
        """Una vuelta del procesador: pagos nuevos, reintentos y conciliaciones que ya vencieron."""
        ids = await asyncio.to_thread(self._pendientes, limite)
        resueltas = 0
        for i in ids:
            ind = await self.procesar(i)
            if ind is not None and ind.estado in ESTADOS_FINALES_INDEMNIZACION:
                resueltas += 1
        return resueltas

    def _pendientes(self, limite: int) -> list[str]:
        with self._fabrica() as s:
            return list(
                s.execute(
                    select(IndemnizacionDB.id)
                    .where(
                        IndemnizacionDB.estado == EstadoIndemnizacion.PENDIENTE.value,
                        IndemnizacionDB.proximo_intento_en <= self._reloj(),
                    )
                    .order_by(IndemnizacionDB.proximo_intento_en)
                    .limit(limite)
                ).scalars()
            )

    async def ejecutar(self, intervalo_s: float = 0.5) -> None:
        while True:
            try:
                await self.procesar_pendientes()
                await asyncio.sleep(intervalo_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fallo en el procesador de indemnizaciones")
                await asyncio.sleep(intervalo_s * 4)

    # --- evento de resultado --------------------------------------------------------------------------------------

    def _encolar_resultado(self, s: Session, ind: IndemnizacionDB, *, repeticion: bool) -> None:
        pagada = ind.estado == EstadoIndemnizacion.PAGADA.value
        payload: dict[str, Any] = {
            "indemnizacion_id": ind.id,
            "idempotency_key": ind.idempotency_key,
            "evento_id": ind.evento_id,
            "poliza_id": ind.poliza_id,
            "cliente_id": ind.cliente_id,
            "producto": ind.producto,
            "estado": ind.estado,
            "monto": str(Decimal(ind.monto).quantize(Decimal("0.01"))),
            "moneda": ind.moneda,
            "referencia_pasarela": ind.referencia_pasarela,
            "resuelta_en": repo.utc(ind.resuelta_en).isoformat(),
            "repeticion": repeticion,
        }
        if not pagada:
            payload |= {"codigo": ind.codigo_fallo, "motivo": ind.motivo_fallo, "intentos": ind.intentos}
        evento = Evento(
            event_type=PAGADA if pagada else FALLIDA,
            source=FUENTE,
            timestamp=self._reloj(),
            correlation_id=ind.correlacion_id or ind.evento_id,
            trace_id=ind.traza_id or str(uuid4()),
            payload=payload,
        )
        repo.encolar_evento(
            s, event_id=evento.event_id, topic=TOPIC_INDEMNIZACIONES, clave=ind.evento_id,
            payload=evento.model_dump(mode="json"), ahora=self._reloj(),
        )

    # --- consultas ------------------------------------------------------------------------------------------------

    def consultar(self, indemnizacion_id: str) -> tuple[IndemnizacionDB, list[IntentoPagoDB]] | None:
        """La indemnización y los estados por los que pasó (criterio 9). Nunca la cuenta de destino."""
        with self._fabrica() as s:
            ind = s.get(IndemnizacionDB, indemnizacion_id)
            if ind is None:
                return None
            intentos = list(
                s.execute(
                    select(IntentoPagoDB).where(IntentoPagoDB.indemnizacion_id == indemnizacion_id).order_by(IntentoPagoDB.numero)
                ).scalars()
            )
            return ind, intentos


def _insertar(sesion: Session, ind: IndemnizacionDB) -> bool:
    """False si otra réplica ya creó el pago con la misma clave o el mismo par evento–póliza."""
    try:
        with sesion.begin_nested():
            sesion.add(ind)
        return True
    except IntegrityError:
        return False


def _contar_reentrega(sesion: Session, ind: IndemnizacionDB, ahora: datetime) -> IndemnizacionDB:
    sesion.execute(
        update(IndemnizacionDB)
        .where(IndemnizacionDB.id == ind.id)
        .values(reentregas=IndemnizacionDB.reentregas + 1, updated_at=ahora)
    )
    sesion.refresh(ind)
    return ind
