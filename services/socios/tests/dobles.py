"""Dobles de prueba de MS Socios: Cotización falsa y un almacén que siempre falla."""

from __future__ import annotations

from typing import Any

from app.socios.almacen import ErrorAlmacen
from app.socios.errores import ErrorSocios, RecursoNoEncontrado, ReglaDeNegocio

# Solo para pruebas: nunca es un secreto real.
SECRETO_PRUEBA = "secreto-de-prueba-socio-0123456789"
SECRETO_JWT_PRUEBA = "jwt-de-prueba-socios-0123456789-abcdefghij"


def cotizacion_de(cliente_id: str, producto: str, numero: int = 1) -> dict[str, Any]:
    """Lo que responde `POST /v1/cotizaciones` de Cotización (CotizacionOut), con todos sus campos internos."""
    return {
        "id": f"00000000-0000-4000-8000-{numero:012d}",
        "cliente_id": cliente_id,
        "producto": producto,
        "estado": "EMITIDA",
        "moneda": "COP",
        "prima_anual": 120000,
        "prima_mensual": 10000,
        "coberturas": [{"tipo": "muerte", "monto": 50000000, "deducible": 0}],
        "vigente_hasta": "2026-12-17T00:00:00Z",
        "version_reglas": "2026.10",
        "degradado": False,
        "aproximada": False,
        "aviso": None,
        "componentes": [{"concepto": "prima_base", "monto": 120000}],
        "metadata": {"hit_cache": True, "latencia_interna_ms": 12.5},
        "asesor_id": None,
        "sin_cuenta": False,
    }


class CotizacionesFalsas:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, str, dict[str, Any]]] = []
        self.error: ErrorSocios | None = None
        self.arriba = True

    async def crear(self, cliente_id: str, producto: str, datos: dict[str, Any]) -> dict[str, Any]:
        self.llamadas.append((cliente_id, producto, datos))
        if self.error is not None:
            raise self.error
        return cotizacion_de(cliente_id, producto, len(self.llamadas))

    async def disponible(self) -> bool:
        return self.arriba


class AlmacenCaido:
    async def incrementar(self, clave: str, ttl_s: int) -> int:
        raise ErrorAlmacen("ConnectionError")

    async def crear_si_no_existe(self, clave: str, valor: str, ttl_s: int) -> bool:
        raise ErrorAlmacen("ConnectionError")

    async def leer(self, clave: str) -> str | None:
        raise ErrorAlmacen("ConnectionError")

    async def escribir(self, clave: str, valor: str, ttl_s: int) -> None:
        raise ErrorAlmacen("ConnectionError")

    async def borrar(self, clave: str) -> None:
        raise ErrorAlmacen("ConnectionError")

    async def disponible(self) -> bool:
        return False

    async def cerrar(self) -> None:
        return None


# --- HU33: Identidad y Suscripción -------------------------------------------------------------------------------------

DOCUMENTO_APTO = ("CC", "100200300")
CLIENTE_APTO = "cli-real-1"


class IdentidadFalsa:
    """Identidad con clientes por documento: `{(tipo, numero): {cliente_id, estado_kyc, consentimiento_vigente}}`."""

    def __init__(self) -> None:
        self.clientes: dict[tuple[str, str], dict[str, Any]] = {
            DOCUMENTO_APTO: {"cliente_id": CLIENTE_APTO, "estado_kyc": "VERIFICADO", "consentimiento_vigente": True},
            ("CC", "200300400"): {"cliente_id": "cli-real-2", "estado_kyc": "PENDIENTE", "consentimiento_vigente": True},
            ("CC", "300400500"): {"cliente_id": "cli-real-3", "estado_kyc": "VERIFICADO", "consentimiento_vigente": False},
        }
        self.consultas: list[tuple[str, str]] = []
        self.error: ErrorSocios | None = None

    async def identificar(self, tipo_documento: str, numero_documento: str) -> dict[str, Any] | None:
        self.consultas.append((tipo_documento, numero_documento))
        if self.error is not None:
            raise self.error
        return self.clientes.get((tipo_documento, numero_documento))


def _saga(socio: str, cliente_id: str, numero: int, cotizacion_id: str, **cambios: Any) -> dict[str, Any]:
    """Una saga de Suscripción tal como la guarda el doble; los campos con guion bajo solo los usa el doble."""
    base = {
        "id": f"11111111-0000-4000-8000-{numero:012d}",
        "cotizacion_id": cotizacion_id,
        "estado_saga": "EN_PROGRESO",
        "paso_actual": "DECISION",
        "pasos": ["DECISION", "COBRO", "FIRMA", "EMISION"],
        "producto": "microseguro_vida",
        "moneda": "COP",
        "monto_asegurado": 50000000,
        "periodicidad_pago": "ANUAL",
        "decision": None,
        "cobro": None,
        "firma": None,
        "poliza": None,
        "revision_asistida": False,
        "creada_en": "2026-11-19T10:00:00Z",
        "_socio": socio,
        "_cliente_id": cliente_id,
        "_clave": "",
    }
    return {**base, **cambios}


class SuscripcionesFalsas:
    """Suscripción con aislamiento por socio e idempotencia por (socio, cliente, clave), como la real."""

    HASH = "a" * 64
    OTP = "123456"

    def __init__(self) -> None:
        self.sagas: dict[str, dict[str, Any]] = {}
        self.llamadas: list[tuple[str, tuple[Any, ...]]] = []
        self.error: ErrorSocios | None = None

    def _propia(self, suscripcion_id: str, socio: str) -> dict[str, Any]:
        saga = self.sagas.get(suscripcion_id)
        if saga is None or saga["_socio"] != socio:
            raise RecursoNoEncontrado
        return saga

    @staticmethod
    def _publica(saga: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in saga.items() if not k.startswith("_")}

    async def iniciar(
        self, socio_client_id, cliente_id, clave, *, cotizacion_id, metodo_pago, acepta_terminos, periodicidad_pago
    ):
        self.llamadas.append(
            ("iniciar", (socio_client_id, cliente_id, clave, cotizacion_id, metodo_pago, acepta_terminos, periodicidad_pago))
        )
        if self.error is not None:
            raise self.error
        for saga in self.sagas.values():
            if (saga["_socio"], saga["_cliente_id"], saga["_clave"]) == (socio_client_id, cliente_id, clave):
                return self._publica(saga)
        saga = _saga(
            socio_client_id,
            cliente_id,
            len(self.sagas) + 1,
            cotizacion_id,
            _clave=clave,
            periodicidad_pago=periodicidad_pago,
        )
        self.sagas[saga["id"]] = saga
        return self._publica(saga)

    async def detalle(self, suscripcion_id, socio_client_id):
        self.llamadas.append(("detalle", (suscripcion_id, socio_client_id)))
        if self.error is not None:
            raise self.error
        return self._publica(self._propia(suscripcion_id, socio_client_id))

    async def documento_firma(self, suscripcion_id, socio_client_id):
        self.llamadas.append(("documento_firma", (suscripcion_id, socio_client_id)))
        self._propia(suscripcion_id, socio_client_id)
        return {
            "suscripcion_id": suscripcion_id,
            "numero_poliza": "POL-2026-000001",
            "documento_sha256": self.HASH,
            "estado": "PENDIENTE",
            "vence_en": "2026-11-20T10:00:00Z",
            "autenticacion": "OTP",
        }

    async def documento_pdf(self, suscripcion_id, socio_client_id):
        self.llamadas.append(("documento_pdf", (suscripcion_id, socio_client_id)))
        self._propia(suscripcion_id, socio_client_id)
        return b"%PDF-1.4 falso"

    async def solicitar_codigo(self, suscripcion_id, socio_client_id):
        self.llamadas.append(("solicitar_codigo", (suscripcion_id, socio_client_id)))
        self._propia(suscripcion_id, socio_client_id)

    async def firmar(self, suscripcion_id, socio_client_id, firma):
        self.llamadas.append(("firmar", (suscripcion_id, socio_client_id, firma)))
        if self.error is not None:
            raise self.error
        saga = self._propia(suscripcion_id, socio_client_id)
        if firma["codigo"] != self.OTP:
            raise ReglaDeNegocio("OTP_INVALIDO")
        saga.update(
            estado_saga="EMITIDA",
            paso_actual="EMISION",
            firma={"estado": "FIRMADA", "vence_en": None, "compensacion": None},
            poliza={
                "numero_poliza": "POL-2026-000001",
                "estado": "VIGENTE",
                "documento_estado": "GENERADO",
                "documento_intentos": 1,
                "documento_error": None,
                "vigencia_desde": "2026-11-19T10:05:00Z",
                "fecha_vencimiento": "2027-11-19T10:05:00Z",
                "fecha_emision": "2026-11-19T10:05:00Z",
                "documento_sha256": self.HASH,
            },
        )
        return {
            "suscripcion_id": suscripcion_id,
            "poliza_id": "pol-interna-1",
            "estado_saga": "EMITIDA",
            "documento_sha256": self.HASH,
            "firmada_en": "2026-11-19T10:05:00Z",
            "canal": firma["canal"],
            "metodo_autenticacion": "OTP",
            "modalidad": "SOCIO",
            "socio_client_id": socio_client_id,
        }
