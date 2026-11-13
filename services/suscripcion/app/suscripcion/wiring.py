"""Ensamblado de Suscripción desde la configuración del entorno."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.configuracion import permitir_sustituto_local
from app.eventos import (
    ConsumidorEventos,
    PublicadorEventos,
    PublicadorKafka,
    PublicadorMemoria,
    RegistroEventos,
    brokers_desde_entorno,
    parametros_seguridad,
)
from app.logs import ganchos_http
from app.suscripcion.almacen import AlmacenDocumentos, AlmacenLocal, AlmacenS3
from app.suscripcion.certificado import FirmadorEnlaces, ServicioCertificado
from app.suscripcion.clientes import (
    ClienteCotizacion,
    ClienteIdentidad,
    ClientePerfilamiento,
)
from app.suscripcion.cobros import manejar_resultado_cobro
from app.suscripcion.config import (
    GRUPO_PAGOS,
    SQLITE_LOCAL,
    TOPIC_RESULTADOS_PAGOS,
    ConfigSuscripcion,
)
from app.suscripcion.documento import cargar_plantilla
from app.suscripcion.enlace_firma import ServicioEnlaceFirma
from app.suscripcion.firma import ServicioFirma
from app.suscripcion.firma_asistida import ServicioFirmaAsistida
from app.suscripcion.modelos_db import TABLA_EVENTOS_PROCESADOS
from app.suscripcion.polizas import GeneradorDocumentos
from app.suscripcion.proveedor_firma import ProveedorFirmaSimulado
from app.suscripcion.reglas import cargar_reglas
from app.suscripcion.repositorio import crear_engine, crear_tablas_locales
from app.suscripcion.revision import UmbralesPrioridad
from app.suscripcion.revision_asistida import ServicioRevisionAsistida
from app.suscripcion.servicio import ServicioSuscripcion


def _http(url: str | None, variable: str, config: ConfigSuscripcion) -> httpx.AsyncClient:
    if not url:
        # Sin sustituto: Suscripción no tiene sentido sin sus vecinos, ni siquiera en desarrollo.
        raise RuntimeError(f"Falta {variable} (URL del servicio vecino)")
    return httpx.AsyncClient(base_url=url.rstrip("/"), timeout=config.timeout_s, event_hooks=ganchos_http())


def construir_servicio(config: ConfigSuscripcion | None = None) -> tuple[ServicioSuscripcion, Engine, list[httpx.AsyncClient]]:
    config = config or ConfigSuscripcion.desde_entorno()
    url = config.database_url
    if url is None:
        permitir_sustituto_local("SUSCRIPCION_DATABASE_URL", f"la base SQLite local ({SQLITE_LOCAL})")
        url = SQLITE_LOCAL
    engine = crear_engine(url)
    if engine.dialect.name == "sqlite":
        crear_tablas_locales(engine)
    clientes = [
        _http(config.identidad_url, "IDENTIDAD_URL", config),
        _http(config.cotizacion_url, "COTIZACION_URL", config),
        _http(config.perfilamiento_url, "PERFILAMIENTO_URL", config),
    ]
    servicio = ServicioSuscripcion(
        fabrica=sessionmaker(bind=engine, expire_on_commit=False),
        identidad=ClienteIdentidad(clientes[0], config.identidad_key),
        cotizaciones=ClienteCotizacion(clientes[1]),
        perfiles=ClientePerfilamiento(clientes[2], config.perfilamiento_key),
        reglas=cargar_reglas(),
    )
    return servicio, engine, clientes


def construir_almacen(config: ConfigSuscripcion) -> AlmacenDocumentos:
    """S3 si hay bucket (con `AWS_ENDPOINT_URL`, Moto en local); si no, una carpeta local solo en desarrollo."""
    if config.polizas_bucket:
        return AlmacenS3(config.polizas_bucket)
    permitir_sustituto_local("POLIZAS_BUCKET", f"la carpeta local de documentos ({config.polizas_documentos_dir})")
    return AlmacenLocal(config.polizas_documentos_dir)


def construir_generador(config: ConfigSuscripcion, engine: Engine, clientes: list[httpx.AsyncClient]) -> GeneradorDocumentos:
    """Genera el PDF de las pólizas registradas. `clientes` es la lista de `construir_servicio` (Identidad, Cotización, …)."""
    return GeneradorDocumentos(
        fabrica=sessionmaker(bind=engine, expire_on_commit=False),
        almacen=construir_almacen(config),
        identidad=ClienteIdentidad(clientes[0], config.identidad_key),
        cotizaciones=ClienteCotizacion(clientes[1]),
        plantilla=cargar_plantilla(),
        max_intentos=config.polizas_max_intentos,
        espera_base_s=config.polizas_reintento_base_s,
        espera_maxima_s=config.polizas_reintento_maximo_s,
    )


def construir_servicio_firma(config: ConfigSuscripcion, engine: Engine, clientes: list[httpx.AsyncClient]) -> ServicioFirma:
    """Firma de la póliza (HU21). El proveedor es el simulado (T4); un proveedor real sería otro adaptador del mismo puerto."""
    return ServicioFirma(
        fabrica=sessionmaker(bind=engine, expire_on_commit=False),
        identidad=ClienteIdentidad(clientes[0], config.identidad_key),
        almacen=construir_almacen(config),
        proveedor=ProveedorFirmaSimulado(config.firma_sim_otp),
        reglas=cargar_reglas(),
    )


def construir_servicio_asistida(
    servicio: ServicioSuscripcion,
    firma: ServicioFirma,
    certificados: ServicioCertificado,
    enlaces: ServicioEnlaceFirma,
    engine: Engine,
    clientes: list[httpx.AsyncClient],
    config: ConfigSuscripcion,
) -> ServicioFirmaAsistida:
    """Venta asistida (HU39): lo que hace el asesor sobre la saga, con los mismos servicios de HU15 y HU21."""
    return ServicioFirmaAsistida(
        fabrica=sessionmaker(bind=engine, expire_on_commit=False),
        suscripciones=servicio,
        firma=firma,
        identidad=ClienteIdentidad(clientes[0], config.identidad_key),
        certificados=certificados,
        enlaces=enlaces,
        espera_correo=timedelta(seconds=cargar_reglas().correo_espera_segundos),
    )


def construir_servicio_revision(engine: Engine, clientes: list[httpx.AsyncClient], config: ConfigSuscripcion) -> ServicioRevisionAsistida:
    """Revisión asistida (HU16): bandeja y decisión del asesor sobre los casos derivados."""
    return ServicioRevisionAsistida(
        fabrica=sessionmaker(bind=engine, expire_on_commit=False),
        identidad=ClienteIdentidad(clientes[0], config.identidad_key),
        cotizaciones=ClienteCotizacion(clientes[1]),
        umbrales=UmbralesPrioridad(config.revision_prioridad_alta_horas, config.revision_prioridad_media_horas),
    )


def construir_servicio_enlace_firma(config: ConfigSuscripcion, firma: ServicioFirma, engine: Engine) -> ServicioEnlaceFirma:
    """Firma por enlace (HU39). La clave se deriva de la de los certificados: un token de un tipo no sirve para el otro."""
    reglas = cargar_reglas()
    firmador = None
    if config.certificados_clave_enlaces:
        clave = hmac.new(config.certificados_clave_enlaces.encode(), b"enlace-firma", hashlib.sha256).hexdigest()
        firmador = FirmadorEnlaces(clave, timedelta(hours=reglas.firma_enlace_vigencia_horas))
    return ServicioEnlaceFirma(
        fabrica=sessionmaker(bind=engine, expire_on_commit=False),
        firma=firma,
        firmador=firmador,
        vigencia=timedelta(hours=reglas.firma_enlace_vigencia_horas),
        espera_correo=timedelta(seconds=reglas.correo_espera_segundos),
    )


def construir_servicio_certificado(config: ConfigSuscripcion, engine: Engine, clientes: list[httpx.AsyncClient]) -> ServicioCertificado:
    """Certificado de cobertura (HU22). Sin la clave de enlaces el servicio arranca, pero no emite ni acepta enlaces."""
    firmador = (
        FirmadorEnlaces(config.certificados_clave_enlaces, timedelta(hours=config.certificados_vigencia_enlace_h))
        if config.certificados_clave_enlaces
        else None
    )
    return ServicioCertificado(
        fabrica=sessionmaker(bind=engine, expire_on_commit=False),
        identidad=ClienteIdentidad(clientes[0], config.identidad_key),
        almacen=construir_almacen(config),
        firmador=firmador,
    )


def construir_publicador() -> PublicadorEventos:
    """Kafka real si la plataforma entrega los brokers; en desarrollo, un publicador en memoria."""
    import os

    if not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
        permitir_sustituto_local("KAFKA_BOOTSTRAP_SERVERS", "un publicador en memoria (los eventos no salen del proceso)")
        return PublicadorMemoria()
    return PublicadorKafka(brokers_desde_entorno(), parametros_seguridad(), client_id="ms-suscripcion")


def construir_consumidor_cobros(engine: Engine, publicador: PublicadorEventos) -> ConsumidorEventos | None:
    """Consumidor de los resultados de MS Pagos (HU18). Sin Kafka (solo desarrollo) no hay nada que consumir."""
    import os

    if not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
        return None
    return ConsumidorEventos(
        brokers=brokers_desde_entorno(),
        seguridad=parametros_seguridad(),
        topic=TOPIC_RESULTADOS_PAGOS,
        grupo=GRUPO_PAGOS,
        manejador=manejar_resultado_cobro,
        fabrica_sesiones=sessionmaker(bind=engine, expire_on_commit=False),
        registro=RegistroEventos(TABLA_EVENTOS_PROCESADOS),
        publicador=publicador,
    )
