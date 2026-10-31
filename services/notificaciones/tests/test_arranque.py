"""Ensamblado: configuración desde el entorno y arranque de la aplicación."""

import pytest
from app.eventos import PublicadorMemoria
from app.notificaciones.canales import CorreoSes, CorreoSimulado
from app.notificaciones.config import ConfigNotificaciones
from app.notificaciones.wiring import construir_correo, construir_ensamblado
from fastapi.testclient import TestClient

from app.configuracion import ErrorConfiguracion

ENTORNO = {"NOTIFICACIONES_DATABASE_URL": "sqlite://", "IDENTIDAD_URL": "http://identidad", "SUSCRIPCION_URL": "http://suscripcion"}


def test_la_configuracion_se_lee_del_entorno():
    c = ConfigNotificaciones.desde_entorno(
        {
            **ENTORNO,
            "URL_PUBLICA": "https://borde.example/",
            "CORREO_PROVEEDOR": " SES ",
            "CORREO_REMITENTE": "notificaciones@solventa.example",
            "AWS_REGION": "us-east-1",
            "NOTIFICACIONES_MAX_INTENTOS": "3",
        }
    )

    assert (c.database_url, c.url_publica, c.correo_proveedor, c.correo_remitente, c.correo_region, c.max_intentos) == (
        "sqlite://", "https://borde.example", "ses", "notificaciones@solventa.example", "us-east-1", 3,
    )


def test_los_valores_por_defecto_son_los_documentados():
    c = ConfigNotificaciones.desde_entorno({})

    assert (c.correo_proveedor, c.max_intentos, c.reintento_base_s, c.reintento_maximo_s, c.url_publica) == ("simulado", 5, 5.0, 300.0, None)


def test_el_correo_se_elige_por_configuracion():
    assert isinstance(construir_correo(ConfigNotificaciones.desde_entorno({})), CorreoSimulado)
    entorno = {"CORREO_PROVEEDOR": "ses", "CORREO_REMITENTE": "a@solventa.example", "AWS_REGION": "us-east-1"}
    ses = construir_correo(ConfigNotificaciones.desde_entorno(entorno))
    assert isinstance(ses, CorreoSes)


def test_un_proveedor_de_correo_que_no_existe_se_rechaza_con_los_validos():
    with pytest.raises(ValueError, match="simulado, ses"):
        construir_correo(ConfigNotificaciones.desde_entorno({"CORREO_PROVEEDOR": "paloma"}))


def test_ses_sin_remitente_no_arranca():
    with pytest.raises(ValueError, match="CORREO_REMITENTE"):
        construir_correo(ConfigNotificaciones.desde_entorno({"CORREO_PROVEEDOR": "ses"}))


def test_sin_base_de_datos_solo_arranca_en_modo_desarrollo(monkeypatch):
    monkeypatch.delenv("SOLVENTA_MODO_DESARROLLO")
    with pytest.raises(ErrorConfiguracion):
        construir_ensamblado(ConfigNotificaciones.desde_entorno({"IDENTIDAD_URL": "http://i", "SUSCRIPCION_URL": "http://s"}))


@pytest.mark.parametrize("falta", ["IDENTIDAD_URL", "SUSCRIPCION_URL"])
def test_sin_las_urls_de_los_vecinos_no_arranca_ni_en_desarrollo(falta):
    entorno = {k: v for k, v in ENTORNO.items() if k != falta}

    with pytest.raises(RuntimeError, match=falta):
        construir_ensamblado(ConfigNotificaciones.desde_entorno(entorno))


def test_en_desarrollo_sin_kafka_se_ensambla_con_un_publicador_en_memoria():
    e = construir_ensamblado(ConfigNotificaciones.desde_entorno(ENTORNO))

    assert isinstance(e.publicador, PublicadorMemoria) and e.consumidor is None


def test_la_aplicacion_arranca_y_responde_health(monkeypatch):
    for k, v in ENTORNO.items():
        monkeypatch.setenv(k, v)
    from app.main import app

    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
