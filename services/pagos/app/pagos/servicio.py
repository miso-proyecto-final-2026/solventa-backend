"""Caso de uso del cobro de la prima (HU18): registrar, cobrar y conciliar sin cobrar dos veces.

El cobro ocurre en tres momentos, cada uno en su propia transacción de base de datos:

1. **Registrar** el comando: una transacción `PENDIENTE` por `idempotency_key` (restricción única). Una repetición no
   crea nada: devuelve la transacción existente y, si ya terminó, vuelve a anunciar su resultado. No se llama a la
   pasarela aquí: el consumidor de Kafka deshace la transacción si falla, pero un cobro no se deshace (ver la plantilla).
2. **Cobrar**: se toma la transacción con `FOR UPDATE SKIP LOCKED`, se anota el intento y se confirma *antes* de
   llamar; así, si el proceso cae a mitad, queda rastro. La orden lleva la misma `idempotency_key`, de modo que la
   pasarela tampoco cobra dos veces.
3. **Conciliar**: si no hubo respuesta (tiempo agotado o respuesta perdida), el siguiente intento primero consulta la
   pasarela por la clave. Si el cobro existe se conserva su resultado; si no, se vuelve a pedir con la misma clave.

El resultado se publica por la cola de salida (`eventos_salida`) en la misma transacción que lo registra.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.contexto import correlacion_id, traza_id
from app.eventos import Evento
from app.pagos import repositorio as repo
from app.pagos.config import MONEDA, TOPIC_RESULTADOS
from app.pagos.dominio import (
    ESTADOS_FINALES,
    DatosToken,
    EstadoTransaccion,
    PasarelaNoDisponible,
    PasarelaPago,
    PasarelaSinRespuesta,
    ResultadoCobro,
    ResultadoIntento,
    SolicitudCobro,
    TipoTransaccion,
    TokenDesconocido,
    parece_pan,
)
from app.pagos.errores import (
    ClaveReutilizada,
    PasarelaNoResponde,
    SolicitudInvalida,
    TokenNoReconocido,
)
from app.pagos.modelos_db import IntentoCobroDB, TransaccionDB

log = logging.getLogger("pagos")

FUENTE = "ms-pagos"
TIPO_COMANDO = "cobro_prima_solicitado"
CONFIRMADO = "cobro_prima_confirmado"
RECHAZADO = "cobro_prima_rechazado"
PERIODICIDADES = ("ANUAL", "MENSUAL")


def _ahora() -> datetime:
    return datetime.now(UTC)


def clave_por_defecto(suscripcion_id: str) -> str:
    """La clave que usa MS Suscripción; se deriva igual si un comando antiguo no la trae."""
    return f"cobro_prima:{suscripcion_id}"


@dataclass(frozen=True)
class ComandoCobro:
    idempotency_key: str
    suscripcion_id: str
    cliente_id: str
    token: str
    monto: Decimal
    moneda: str
    periodicidad_pago: str | None = None

    @classmethod
    def desde_evento(cls, payload: dict[str, Any]) -> ComandoCobro:
        """Valida `cobro_prima_solicitado`. Acepta el payload de HU15 (solo primas) y el nuevo (con `monto`)."""
        faltan = [c for c in ("suscripcion_id", "cliente_id", "metodo_pago") if not payload.get(c)]
        if faltan:
            raise SolicitudInvalida(detalles=[{"campo": c, "motivo": "es obligatorio"} for c in faltan])
        periodicidad = payload.get("periodicidad_pago") or "ANUAL"
        bruto = payload.get("monto")
        if bruto is None:
            bruto = payload.get("prima_mensual" if periodicidad == "MENSUAL" else "prima_anual")
        return cls.validado(
            idempotency_key=payload.get("idempotency_key") or clave_por_defecto(payload["suscripcion_id"]),
            suscripcion_id=str(payload["suscripcion_id"]),
            cliente_id=str(payload["cliente_id"]),
            token=str(payload["metodo_pago"]),
            monto=bruto,
            moneda=payload.get("moneda") or MONEDA,
            periodicidad_pago=periodicidad,
        )

    @classmethod
    def validado(cls, *, monto: Any, moneda: str, periodicidad_pago: str | None, **resto: str) -> ComandoCobro:
        try:
            valor = Decimal(str(monto)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise SolicitudInvalida(detalles=[{"campo": "monto", "motivo": "no es un número"}]) from None
        defectos = []
        if not valor.is_finite() or valor <= 0 or valor >= Decimal("1e13"):
            defectos.append({"campo": "monto", "motivo": "debe ser mayor que 0 y caber en NUMERIC(15,2)"})
        if moneda != MONEDA:
            defectos.append({"campo": "moneda", "motivo": f"solo se cobra en {MONEDA}"})
        if periodicidad_pago is not None and periodicidad_pago not in PERIODICIDADES:
            defectos.append({"campo": "periodicidad_pago", "motivo": "debe ser ANUAL o MENSUAL"})
        if parece_pan(resto["token"]):
            # El valor no se copia al detalle ni al log: se informa solo el campo.
            defectos.append({"campo": "metodo_pago", "motivo": "debe ser el token de la pasarela, nunca el número de tarjeta"})
        if not 1 <= len(resto["idempotency_key"]) <= 100:
            defectos.append({"campo": "idempotency_key", "motivo": "debe tener de 1 a 100 caracteres"})
        if defectos:
            raise SolicitudInvalida(detalles=defectos)
        return cls(monto=valor, moneda=moneda, periodicidad_pago=periodicidad_pago, **resto)


class ServicioCobros:
    def __init__(
        self,
        *,
        fabrica: sessionmaker[Session],
        pasarela: PasarelaPago,
        periodo_gracia_dias: int,
        timeout_pasarela_s: float = 5.0,
        reintento_base_s: float = 5.0,
        reintento_maximo_s: float = 300.0,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._fabrica = fabrica
        self._pasarela = pasarela
        self._gracia = timedelta(days=periodo_gracia_dias)
        self._timeout_s = timeout_pasarela_s
        self._base_s = reintento_base_s
        self._maximo_s = reintento_maximo_s
        self._reloj = reloj
        # Mientras una réplica espera a la pasarela, la transacción queda «prestada» y las demás no la toman. Cubre las tres
        # llamadas de un intento (describir el token, consultar y cobrar) con margen.
        self._prestamo = timedelta(seconds=timeout_pasarela_s * 3 + 5)

    # --- 1. registrar ---------------------------------------------------------------------------------------------

    async def manejar_evento(self, evento: Evento, sesion: Session) -> None:
        """Manejador del consumidor de `solventa.suscripcion.comandos` (corre en la transacción de la plantilla)."""
        if evento.event_type != TIPO_COMANDO:
            log.info("evento ignorado event_type=%s event_id=%s", evento.event_type, evento.event_id)
            return
        traza_id.set(evento.trace_id)
        correlacion_id.set(evento.correlation_id)
        self.registrar(sesion, ComandoCobro.desde_evento(evento.payload), origen="kafka")

    def registrar(self, sesion: Session, comando: ComandoCobro, *, origen: str) -> tuple[TransaccionDB, bool]:
        """Devuelve (transacción, creada). Con `creada=False` es una repetición: se cuenta y no se cobra otra vez."""
        existente = repo.por_clave(sesion, comando.idempotency_key)
        if existente is None:
            ahora = self._reloj()
            token = repo.obtener_o_crear_token(sesion, comando.token, comando.cliente_id, ahora)
            nueva = TransaccionDB(
                id=str(uuid4()),
                tipo=TipoTransaccion.COBRO_PRIMA.value,
                idempotency_key=comando.idempotency_key,
                suscripcion_id=comando.suscripcion_id,
                cliente_id=comando.cliente_id,
                monto=comando.monto,
                moneda=comando.moneda,
                periodicidad_pago=comando.periodicidad_pago,
                estado=EstadoTransaccion.PENDIENTE.value,
                token_id=token.id,
                intentos=0,
                proximo_intento_en=ahora,
                origen=origen,
                reentregas=0,
                correlacion_id=correlacion_id.get(),
                traza_id=traza_id.get(),
                created_at=ahora,
                updated_at=ahora,
            )
            if repo.insertar_transaccion(sesion, nueva):
                log.info(
                    "cobro registrado transaccion_id=%s suscripcion_id=%s origen=%s", nueva.id, nueva.suscripcion_id, origen
                )
                return nueva, True
            existente = repo.por_clave(sesion, comando.idempotency_key)  # otra réplica ganó la carrera
        return self._repeticion(sesion, existente, comando), False

    def _repeticion(self, sesion: Session, tx: TransaccionDB, comando: ComandoCobro) -> TransaccionDB:
        if (tx.suscripcion_id, Decimal(tx.monto)) != (comando.suscripcion_id, comando.monto):
            raise ClaveReutilizada
        tx = repo.contar_reentrega(sesion, tx, self._reloj())
        log.info("cobro repetido transaccion_id=%s estado=%s reentregas=%d", tx.id, tx.estado, tx.reentregas)
        if tx.estado in ESTADOS_FINALES:
            # La segunda respuesta es la de la primera: se vuelve a anunciar el mismo resultado, sin cobrar.
            self._encolar_resultado(sesion, tx, repeticion=True)
        return tx

    # --- 2. cobrar y 3. conciliar ---------------------------------------------------------------------------------

    async def procesar(self, transaccion_id: str) -> TransaccionDB | None:
        """Un intento sobre la transacción. None si no le toca o la tiene otra réplica. Seguro de repetir."""
        tomado = await asyncio.to_thread(self._tomar, transaccion_id)
        if tomado is None:
            return None
        numero, solicitud, describir = tomado
        datos_token = await self._describir(solicitud.token) if describir else None
        inicio = time.monotonic()
        resultado: ResultadoCobro | None = None
        desenlace = ResultadoIntento.SIN_RESPUESTA
        try:
            if numero > 1:
                # Un intento anterior no supo si cobró: primero se pregunta (EC-D1.2).
                resultado = await asyncio.wait_for(self._pasarela.consultar(solicitud.idempotency_key), self._timeout_s)
                if resultado is not None:
                    desenlace = ResultadoIntento.CONCILIADO
            if resultado is None:
                resultado = await asyncio.wait_for(self._pasarela.cobrar(solicitud), self._timeout_s)
                desenlace = ResultadoIntento.APROBADO if resultado.aprobado else ResultadoIntento.RECHAZADO
        except PasarelaNoDisponible as error:
            desenlace = ResultadoIntento.NO_DISPONIBLE
            log.warning("pasarela no disponible transaccion_id=%s intento=%d: %s", transaccion_id, numero, error)
        except (PasarelaSinRespuesta, TimeoutError) as error:
            log.warning("pasarela sin respuesta transaccion_id=%s intento=%d: %s", transaccion_id, numero, type(error).__name__)
        except Exception:  # un fallo desconocido puede haber cobrado: se trata como «sin respuesta»
            log.exception("fallo al cobrar transaccion_id=%s intento=%d", transaccion_id, numero)
        duracion_ms = int((time.monotonic() - inicio) * 1000)
        return await asyncio.to_thread(
            self._cerrar_intento, transaccion_id, numero, desenlace, resultado, duracion_ms, datos_token
        )

    async def _describir(self, token: str) -> DatosToken | None:
        """Marca y últimos 4 para `tokens_tarjeta`. Es informativo: si la pasarela no responde, el cobro sigue."""
        try:
            return await asyncio.wait_for(self._pasarela.describir_token(token), self._timeout_s)
        except Exception as error:  # noqa: BLE001
            log.warning("no se pudo describir el token: %s", type(error).__name__)
            return None

    def _tomar(self, transaccion_id: str) -> tuple[int, SolicitudCobro, bool] | None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            tx = repo.tomar_pendiente(s, transaccion_id, ahora)
            if tx is None:
                return None
            intento = repo.nuevo_intento(s, tx, ahora)
            tx.proximo_intento_en = ahora + self._prestamo
            tx.updated_at = ahora
            token = repo.token_de(s, tx)
            solicitud = SolicitudCobro(
                idempotency_key=tx.idempotency_key,
                token=token.token,
                monto=Decimal(tx.monto),
                moneda=tx.moneda,
                referencia=tx.suscripcion_id,
            )
            return intento.numero, solicitud, token.ultimos4 is None

    def _cerrar_intento(
        self,
        transaccion_id: str,
        numero: int,
        desenlace: ResultadoIntento,
        resultado: ResultadoCobro | None,
        duracion_ms: int,
        datos_token: DatosToken | None = None,
    ) -> TransaccionDB | None:
        ahora = self._reloj()
        with self._fabrica() as s, s.begin():
            tx = repo.obtener(s, transaccion_id, bloquear=True)
            if datos_token is not None:
                token = repo.token_de(s, tx)
                token.ultimos4, token.marca = datos_token.ultimos4, datos_token.marca
            intento = next(i for i in repo.intentos_de(s, transaccion_id) if i.numero == numero)
            _anotar(intento, desenlace, resultado, duracion_ms, ahora)
            if tx.estado in ESTADOS_FINALES:
                return tx  # ya resuelta (no debería ocurrir: la transacción estaba prestada a esta réplica)
            if resultado is None:
                espera = min(self._maximo_s, self._base_s * 2 ** (numero - 1))
                tx.proximo_intento_en = ahora + timedelta(seconds=espera)
                tx.updated_at = ahora
                log.info("cobro sin resolver transaccion_id=%s intento=%d reintento_en_s=%s", tx.id, numero, espera)
                return tx
            tx.estado = (EstadoTransaccion.APROBADA if resultado.aprobado else EstadoTransaccion.RECHAZADA).value
            tx.referencia_pasarela = resultado.referencia_pasarela
            if not resultado.aprobado:
                tx.codigo_rechazo = (resultado.codigo or "RECHAZADO")[:50]
                tx.motivo_rechazo = (resultado.motivo or "La pasarela rechazó el cobro.")[:200]
                tx.gracia_hasta = ahora + self._gracia
            tx.proximo_intento_en = None
            tx.resuelta_en = ahora
            tx.updated_at = ahora
            self._encolar_resultado(s, tx, repeticion=False)
        log.info(
            "cobro resuelto transaccion_id=%s estado=%s intento=%d desenlace=%s duracion_ms=%d",
            transaccion_id, tx.estado, numero, desenlace.value, duracion_ms,
        )
        return tx

    async def procesar_pendientes(self, limite: int = 20) -> int:
        """Una vuelta del procesador: cobros nuevos, reintentos y conciliaciones que ya vencieron."""
        ids = await asyncio.to_thread(self._pendientes, limite)
        resueltos = 0
        for i in ids:
            tx = await self.procesar(i)
            if tx is not None and tx.estado in ESTADOS_FINALES:
                resueltos += 1
        return resueltos

    def _pendientes(self, limite: int) -> list[str]:
        with self._fabrica() as s:
            return repo.pendientes_vencidas(s, self._reloj(), limite)

    async def ejecutar(self, intervalo_s: float = 0.5) -> None:
        while True:
            try:
                await self.procesar_pendientes()
                await asyncio.sleep(intervalo_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fallo en el procesador de cobros")
                await asyncio.sleep(intervalo_s * 4)

    # --- eventos de resultado -------------------------------------------------------------------------------------

    def _encolar_resultado(self, s: Session, tx: TransaccionDB, *, repeticion: bool) -> None:
        aprobado = tx.estado == EstadoTransaccion.APROBADA.value
        token = repo.token_de(s, tx)
        payload: dict[str, Any] = {
            "transaccion_id": tx.id,
            "suscripcion_id": tx.suscripcion_id,
            "cliente_id": tx.cliente_id,
            "idempotency_key": tx.idempotency_key,
            "tipo": tx.tipo,
            "estado": tx.estado,
            "monto": str(Decimal(tx.monto).quantize(Decimal("0.01"))),
            "moneda": tx.moneda,
            "periodicidad_pago": tx.periodicidad_pago,
            "referencia_pasarela": tx.referencia_pasarela,
            "ultimos4": token.ultimos4,
            "resuelta_en": repo.utc(tx.resuelta_en).isoformat(),
            "repeticion": repeticion,
        }
        if not aprobado:
            payload |= {
                "codigo": tx.codigo_rechazo,
                "motivo": tx.motivo_rechazo,
                "gracia_hasta": repo.utc(tx.gracia_hasta).isoformat(),
            }
        evento = Evento(
            event_type=CONFIRMADO if aprobado else RECHAZADO,
            source=FUENTE,
            timestamp=self._reloj(),
            correlation_id=tx.correlacion_id or tx.suscripcion_id,
            trace_id=tx.traza_id or str(uuid4()),
            payload=payload,
        )
        repo.encolar_evento(
            s, event_id=evento.event_id, topic=TOPIC_RESULTADOS, clave=tx.suscripcion_id,
            payload=evento.model_dump(mode="json"), ahora=self._reloj(),
        )

    # --- entradas REST internas -----------------------------------------------------------------------------------

    async def cobrar(self, comando: ComandoCobro) -> tuple[TransaccionDB, bool]:
        """`POST /v1/pagos/cobros`: el mismo caso de uso que el comando de Kafka, y espera un primer intento."""

        def registrar() -> tuple[TransaccionDB, bool]:
            with self._fabrica() as s, s.begin():
                return self.registrar(s, comando, origen="rest")

        tx, creada = await asyncio.to_thread(registrar)
        if creada:
            tx = await self.procesar(tx.id) or tx
        return tx, creada

    async def tokenizar(self, token: str, cliente_id: str | None) -> tuple[int, str, str]:
        """Registra un token de la pasarela (nunca el PAN, SAQ-A) con la marca y los últimos 4 que ella informa."""
        try:
            datos = await asyncio.wait_for(self._pasarela.describir_token(token), self._timeout_s)
        except TokenDesconocido:
            raise TokenNoReconocido from None
        except (PasarelaNoDisponible, PasarelaSinRespuesta, TimeoutError):
            raise PasarelaNoResponde from None

        def guardar() -> tuple[int, str, str]:
            with self._fabrica() as s, s.begin():
                registro = repo.obtener_o_crear_token(s, token, cliente_id, self._reloj())
                registro.ultimos4, registro.marca = datos.ultimos4, datos.marca
                return registro.id, datos.ultimos4, datos.marca

        return await asyncio.to_thread(guardar)

    # --- consultas ------------------------------------------------------------------------------------------------

    def consultar(self, transaccion_id: str) -> tuple[TransaccionDB, str | None, str | None] | None:
        """(transacción, últimos 4, marca) para la API; nunca el token."""
        with self._fabrica() as s:
            tx = repo.obtener(s, transaccion_id)
            if tx is None:
                return None
            token = repo.token_de(s, tx)
            return tx, token.ultimos4, token.marca


def _anotar(
    intento: IntentoCobroDB, desenlace: ResultadoIntento, resultado: ResultadoCobro | None, duracion_ms: int, ahora: datetime
) -> None:
    intento.resultado = desenlace.value
    intento.duracion_ms = duracion_ms
    intento.updated_at = ahora
    if resultado is not None:
        intento.referencia_pasarela = resultado.referencia_pasarela
        if not resultado.aprobado:
            intento.codigo = (resultado.codigo or "RECHAZADO")[:50]
            intento.motivo = (resultado.motivo or "")[:200] or None
