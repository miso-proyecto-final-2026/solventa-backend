"""Verificación de identidad (HU02) por la API, con el proveedor simulado en sus cuatro modos."""

from datetime import UTC, datetime, timedelta

import pytest

from app.kyc.config import ConfigKyc
from app.kyc.dominio import RETO_VIDA
from app.kyc.servicio import ServicioVerificacion
from app.kyc.simulador import (
    ConfigSimulador,
    ModoSimulador,
    ProveedorKycSimulado,
    VerificadorSimulado,
)
from app.kyc.wiring import obtener_servicio_kyc
from app.main import app
from app.models import VerificacionKyc
from app.routers.clientes import get_db as get_db_dependencia
from tests.helpers import registrar, registrar_segundo

JPEG = b"\xff\xd8\xff\xe0" + b"imagen-de-prueba" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"imagen-de-prueba" * 8
CLAVE_SERVICIO = "clave-de-prueba"


class AlmacenMemoria:
    def __init__(self):
        self.objetos: dict[str, tuple[bytes, str]] = {}

    def guardar(self, clave, contenido, content_type):
        self.objetos[clave] = (contenido, content_type)


class Reloj:
    def __init__(self):
        self.ahora = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.ahora

    def avanzar(self, **kwargs):
        self.ahora += timedelta(**kwargs)


class Entorno:
    def __init__(self, modo, config):
        self.proveedor = ProveedorKycSimulado(ConfigSimulador(modo=modo))
        self.almacen = AlmacenMemoria()
        self.reloj = Reloj()
        self.servicio = ServicioVerificacion(
            VerificadorSimulado(self.proveedor), self.almacen, config, self.reloj
        )
        app.dependency_overrides[obtener_servicio_kyc] = lambda: self.servicio


@pytest.fixture
def entorno(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEYS", CLAVE_SERVICIO)

    def construir(modo=ModoSimulador.EXITO, **config):
        return Entorno(modo, ConfigKyc(timeout_s=0.2, **config))

    return construir


def _archivos(anverso=JPEG, reverso=JPEG, selfie=JPEG, tipo="image/jpeg"):
    return {
        "documento_anverso": ("anverso.jpg", anverso, tipo),
        "documento_reverso": ("reverso.jpg", reverso, tipo),
        "selfie": ("selfie.jpg", selfie, tipo),
    }


def enviar(client, cliente_id, auth, clave="clave-0001", reto=RETO_VIDA, archivos=None, extra=None):
    return client.post(
        f"/v1/clientes/{cliente_id}/verificaciones",
        data={"reto": reto},
        files=archivos or _archivos(),
        headers={**auth, "Idempotency-Key": clave, **(extra or {})},
    )


def estado(client, cliente_id, headers):
    return client.get(f"/v1/clientes/{cliente_id}/verificacion", headers=headers)


def _sesion_bd():
    return next(app.dependency_overrides[get_db_dependencia]())


@pytest.fixture(autouse=True)
def _limpiar_override_kyc():
    yield
    app.dependency_overrides.pop(obtener_servicio_kyc, None)


# -- criterio 4: verificación exitosa ---------------------------------------------------------


def test_verificacion_exitosa_marca_al_cliente_como_verificado(client, entorno):
    env = entorno()
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth)

    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["estado"] == "VERIFICADO"
    assert cuerpo["motivo"] is None
    consulta = estado(client, cliente_id, auth).json()
    assert consulta["estado_kyc"] == "VERIFICADO"
    assert consulta["ultima_verificacion_id"] == cuerpo["id"]
    assert env.proveedor.casos_abiertos == 1


def test_las_tres_imagenes_quedan_en_el_almacen_con_claves_sin_datos_personales(client, entorno):
    env = entorno()
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth, archivos=_archivos(anverso=PNG, reverso=PNG, selfie=PNG, tipo="image/png"))

    verificacion_id = r.json()["id"]
    claves = sorted(env.almacen.objetos)
    assert claves == [
        f"kyc/{cliente_id}/{verificacion_id}/anverso.png",
        f"kyc/{cliente_id}/{verificacion_id}/reverso.png",
        f"kyc/{cliente_id}/{verificacion_id}/selfie.png",
    ]
    assert "100200300" not in "".join(claves)


# -- criterio 5: rechazo con reintentos --------------------------------------------------------


def test_rechazo_deja_motivo_generico_y_descuenta_un_intento(client, entorno):
    entorno(ModoSimulador.RECHAZO)
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth)

    assert r.status_code == 201
    assert r.json()["estado"] == "RECHAZADO"
    assert r.json()["motivo"] == "VERIFICACION_NO_SUPERADA"
    assert r.json()["intentos_restantes"] == 2
    assert estado(client, cliente_id, auth).json()["estado_kyc"] == "RECHAZADO"


def test_tras_agotar_los_intentos_se_bloquea_y_luego_se_libera(client, entorno):
    env = entorno(ModoSimulador.RECHAZO, max_intentos=3, bloqueo_min=30)
    cliente_id, auth = registrar(client)
    for i in range(3):
        assert enviar(client, cliente_id, auth, clave=f"intento-{i:04d}").status_code == 201

    bloqueado = enviar(client, cliente_id, auth, clave="intento-0004")

    assert bloqueado.status_code == 429
    assert int(bloqueado.headers["Retry-After"]) > 0
    assert estado(client, cliente_id, auth).json()["bloqueado_hasta"] is not None
    assert env.proveedor.llamadas == 3  # el intento bloqueado no llegó al proveedor

    env.reloj.avanzar(minutes=31)
    env.proveedor.config = ConfigSimulador(modo=ModoSimulador.EXITO)

    assert enviar(client, cliente_id, auth, clave="intento-0005").json()["estado"] == "VERIFICADO"


def test_el_maximo_de_intentos_es_configuracion(client, entorno):
    entorno(ModoSimulador.RECHAZO, max_intentos=1)
    cliente_id, auth = registrar(client)

    enviar(client, cliente_id, auth, clave="intento-0001")

    assert enviar(client, cliente_id, auth, clave="intento-0002").status_code == 429


# -- criterio 6: proveedor lento o caído -------------------------------------------------------


@pytest.mark.parametrize("modo", [ModoSimulador.TIMEOUT, ModoSimulador.ERROR_5XX])
def test_proveedor_lento_o_caido_deja_el_caso_en_revision_sin_rechazar(client, entorno, modo):
    env = entorno(modo)
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth)

    assert r.status_code == 201
    assert r.json()["estado"] == "EN_REVISION"
    assert r.json()["intentos_restantes"] == 3  # no cuenta como rechazo
    assert estado(client, cliente_id, auth).json()["estado_kyc"] == "EN_REVISION"
    assert len(env.almacen.objetos) == 3  # las imágenes no se pierden


def test_un_caso_en_revision_no_admite_otro_envio(client, entorno):
    entorno(ModoSimulador.ERROR_5XX)
    cliente_id, auth = registrar(client)
    enviar(client, cliente_id, auth, clave="intento-0001")

    r = enviar(client, cliente_id, auth, clave="intento-0002")

    assert r.status_code == 409


# -- criterio 8: idempotencia ------------------------------------------------------------------


def test_reenviar_la_misma_verificacion_no_abre_otro_caso_en_el_proveedor(client, entorno):
    env = entorno()
    cliente_id, auth = registrar(client)

    primero = enviar(client, cliente_id, auth, clave="misma-clave")
    reintento = enviar(client, cliente_id, auth, clave="misma-clave")

    assert primero.status_code == 201
    assert reintento.status_code == 200
    assert reintento.json()["id"] == primero.json()["id"]
    assert env.proveedor.llamadas == 1
    assert env.proveedor.casos_abiertos == 1
    assert len(env.almacen.objetos) == 3
    assert _sesion_bd().query(VerificacionKyc).count() == 1


def test_el_reintento_de_un_caso_en_revision_devuelve_el_mismo_resultado(client, entorno):
    env = entorno(ModoSimulador.ERROR_5XX)
    cliente_id, auth = registrar(client)

    primero = enviar(client, cliente_id, auth, clave="misma-clave")
    reintento = enviar(client, cliente_id, auth, clave="misma-clave")

    assert reintento.status_code == 200
    assert reintento.json()["estado"] == primero.json()["estado"] == "EN_REVISION"
    assert env.proveedor.llamadas == 1


# -- otras reglas de negocio -------------------------------------------------------------------


def test_un_cliente_ya_verificado_no_se_vuelve_a_verificar(client, entorno):
    entorno()
    cliente_id, auth = registrar(client)
    enviar(client, cliente_id, auth, clave="intento-0001")

    assert enviar(client, cliente_id, auth, clave="intento-0002").status_code == 409


def test_el_reto_de_vida_debe_ser_el_definido(client, entorno):
    env = entorno()
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth, reto="SONREIR")

    assert r.status_code == 422
    assert env.proveedor.llamadas == 0
    assert env.almacen.objetos == {}


# -- validación de imágenes (criterio 7) -------------------------------------------------------


def test_rechaza_archivos_que_no_son_imagen(client, entorno):
    env = entorno()
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth, archivos=_archivos(tipo="application/pdf"))

    assert r.status_code == 415
    assert env.almacen.objetos == {}


def test_rechaza_un_archivo_que_dice_ser_jpeg_pero_no_lo_es(client, entorno):
    entorno()
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth, archivos=_archivos(selfie=b"esto no es una imagen"))

    assert r.status_code == 422


def test_rechaza_imagenes_que_superan_el_tamano_maximo(client, entorno):
    entorno(max_bytes_imagen=64)
    cliente_id, auth = registrar(client)

    r = enviar(client, cliente_id, auth, archivos=_archivos(selfie=JPEG + b"x" * 200))

    assert r.status_code == 413


def test_exige_idempotency_key(client, entorno):
    entorno()
    cliente_id, auth = registrar(client)

    sin_clave = client.post(
        f"/v1/clientes/{cliente_id}/verificaciones",
        data={"reto": RETO_VIDA},
        files=_archivos(),
        headers=auth,
    )

    assert sin_clave.status_code == 422
    assert enviar(client, cliente_id, auth, clave="corta").status_code == 422


def test_en_el_cluster_exige_https(client, entorno):
    entorno(exigir_https=True)
    cliente_id, auth = registrar(client)

    http = enviar(client, cliente_id, auth, extra={"X-Forwarded-Proto": "http"})
    https = enviar(client, cliente_id, auth, clave="clave-0002", extra={"X-Forwarded-Proto": "https"})

    assert http.status_code == 400
    assert https.status_code == 201


# -- seguridad ---------------------------------------------------------------------------------


def test_sin_sesion_no_se_puede_verificar(client, entorno):
    entorno()
    cliente_id, _ = registrar(client)

    assert enviar(client, cliente_id, {}).status_code == 401


def test_un_cliente_no_verifica_ni_consulta_a_otro(client, entorno):
    entorno()
    ana_id, _ = registrar(client)
    _, beto = registrar_segundo(client)

    assert enviar(client, ana_id, beto).status_code == 403
    assert estado(client, ana_id, beto).status_code == 403


def test_un_servicio_puede_consultar_el_estado_pero_no_enviar(client, entorno):
    entorno()
    cliente_id, auth = registrar(client)
    enviar(client, cliente_id, auth)
    servicio = {"X-Service-Key": CLAVE_SERVICIO}

    assert estado(client, cliente_id, servicio).json()["estado_kyc"] == "VERIFICADO"
    assert enviar(client, cliente_id, servicio, clave="clave-0009").status_code == 403


# -- resultado que consume HU03 ----------------------------------------------------------------


def test_sin_verificaciones_el_estado_es_pendiente_con_todos_los_intentos(client, entorno):
    entorno(max_intentos=3)
    cliente_id, auth = registrar(client)

    cuerpo = estado(client, cliente_id, auth).json()

    assert cuerpo["estado_kyc"] == "PENDIENTE"
    assert cuerpo["intentos_restantes"] == 3
    assert cuerpo["bloqueado_hasta"] is None
    assert cuerpo["ultima_verificacion_id"] is None
    assert set(cuerpo) == {
        "cliente_id",
        "estado_kyc",
        "motivo",
        "intentos_restantes",
        "bloqueado_hasta",
        "ultima_verificacion_id",
    }
