"""Configuración de MS Pagos desde variables de entorno.

La conexión a Kafka (`KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_SEGURIDAD`) la lee `app/eventos` (plantilla HS2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SQLITE_LOCAL = "sqlite:///./pagos.db"
TOPIC_COMANDOS = "solventa.suscripcion.comandos"   # MS Suscripción -> MS Pagos: cobro_prima_solicitado
TOPIC_RESULTADOS = "solventa.pagos.resultados"     # MS Pagos -> MS Suscripción (clave de partición: suscripcion_id)
TOPIC_NOTIFICACIONES = "solventa.notificaciones"   # MS Pagos -> notificaciones: aviso al operador por gracia vencida (HU19)
# El grupo de consumo debe empezar por el nombre del servicio: es lo único que permite su política de IAM.
GRUPO_COBROS = "solventa-pagos.cobros"
MONEDA = "COP"
# PROVISIONAL: la historia no fija la duración del período de gracia. Validar con negocio (la gestión es de HU19).
PERIODO_GRACIA_DIAS_PROVISIONAL = 3
# HU19 (supuesto a confirmar): un caso está «próximo a vencer» si le quedan menos de estas horas de gracia.
HORAS_POR_VENCER = 24


def _claves(crudo: str) -> dict[str, str]:
    """`nombre=clave,otro=clave2` → {clave: nombre}. Una clave sin nombre se llama «servicio»."""
    claves: dict[str, str] = {}
    for parte in crudo.split(","):
        parte = parte.strip()
        if not parte:
            continue
        nombre, separador, clave = parte.partition("=")
        if separador:
            if clave.strip():
                claves[clave.strip()] = nombre.strip() or "servicio"
        else:
            claves[nombre] = "servicio"
    return claves


@dataclass(frozen=True)
class ConfigPagos:
    # None = sin configurar; solo en modo desarrollo se sustituye por SQLITE_LOCAL.
    database_url: str | None = None
    # Clave de servicio → nombre del llamador. Vacío = nadie accede (falla cerrado).
    api_keys: dict[str, str] | None = None
    # None = sin configurar; solo en modo desarrollo se sustituye por un publicador en memoria sin consumidor.
    kafka_brokers: str | None = None
    # Adaptador de pasarela (ADR-0004): hoy solo existe `simulador`.
    pasarela_proveedor: str = "simulador"
    pasarela_timeout_s: float = 5.0
    # Modo por defecto del simulador: APROBAR, RECHAZAR, TARDAR o PERDER_RESPUESTA (ver README).
    simulador_modo: str = "APROBAR"
    simulador_demora_s: float = 10.0
    periodo_gracia_dias: int = PERIODO_GRACIA_DIAS_PROVISIONAL
    # Espera antes de volver a intentar (o conciliar) un cobro sin respuesta; se duplica en cada intento.
    reintento_base_s: float = 5.0
    reintento_maximo_s: float = 300.0
    # JWT del asesor u operador emitido por Identidad (ADR-0008); solo lo usa la gestión de cobros fallidos (HU19).
    asesor_jwt_secreto: str | None = None
    asesor_jwt_emisor: str = "solventa-identidad"
    horas_por_vencer: int = HORAS_POR_VENCER
    # Cada cuánto busca casos con la gracia vencida para avisar al operador.
    detector_gracia_intervalo_s: float = 60.0

    @classmethod
    def desde_entorno(cls, env: dict[str, str] | None = None) -> ConfigPagos:
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("PAGOS_DATABASE_URL") or e.get("DATABASE_URL"),
            api_keys=_claves(e.get("PAGOS_API_KEYS", "")),
            kafka_brokers=e.get("KAFKA_BOOTSTRAP_SERVERS") or None,
            pasarela_proveedor=e.get("PASARELA_PROVEEDOR", "simulador").strip().lower(),
            pasarela_timeout_s=float(e.get("PASARELA_TIMEOUT_S", "5")),
            simulador_modo=e.get("PASARELA_SIM_MODO", "APROBAR").strip().upper(),
            simulador_demora_s=float(e.get("PASARELA_SIM_DEMORA_S", "10")),
            periodo_gracia_dias=int(e.get("PERIODO_GRACIA_DIAS", str(PERIODO_GRACIA_DIAS_PROVISIONAL))),
            reintento_base_s=float(e.get("PAGOS_REINTENTO_BASE_S", "5")),
            reintento_maximo_s=float(e.get("PAGOS_REINTENTO_MAXIMO_S", "300")),
            asesor_jwt_secreto=e.get("ASESOR_JWT_SECRETO") or None,
            asesor_jwt_emisor=e.get("ASESOR_JWT_EMISOR", cls.asesor_jwt_emisor),
            horas_por_vencer=int(e.get("PAGOS_HORAS_POR_VENCER", str(HORAS_POR_VENCER))),
            detector_gracia_intervalo_s=float(e.get("PAGOS_DETECTOR_GRACIA_S", "60")),
        )
