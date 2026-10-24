"""Ensamblado: configuración desde el entorno y arranque de la aplicación."""

import pytest
from app.eventos import PublicadorMemoria
from app.suscripcion.config import ConfigSuscripcion
from app.suscripcion.wiring import construir_publicador, construir_servicio
from fastapi.testclient import TestClient

from app.configuracion import ErrorConfiguracion

ENTORNO = {
    "SUSCRIPCION_DATABASE_URL": "sqlite://",
    "IDENTIDAD_URL": "http://identidad",
    "COTIZACION_URL": "http://cotizacion",
    "PERFILAMIENTO_URL": "http://perfilamiento",
}


def test_la_configuracion_se_lee_del_entorno():
    c = ConfigSuscripcion.desde_entorno({**ENTORNO, "IDENTIDAD_SERVICE_KEY": "k", "SUSCRIPCION_ESPERA_BARRIDO_S": "5"})
    assert (c.database_url, c.identidad_url, c.identidad_key, c.espera_barrido_s) == ("sqlite://", "http://identidad", "k", 5.0)


def test_sin_base_de_datos_solo_arranca_en_modo_desarrollo(monkeypatch):
    config = ConfigSuscripcion.desde_entorno({k: v for k, v in ENTORNO.items() if k != "SUSCRIPCION_DATABASE_URL"})
    monkeypatch.delenv("SOLVENTA_MODO_DESARROLLO")
    with pytest.raises(ErrorConfiguracion):
        construir_servicio(config)


def test_sin_url_de_un_vecino_no_arranca_ni_en_desarrollo():
    config = ConfigSuscripcion.desde_entorno({k: v for k, v in ENTORNO.items() if k != "COTIZACION_URL"})
    with pytest.raises(RuntimeError, match="COTIZACION_URL"):
        construir_servicio(config)


def test_sin_brokers_el_publicador_es_en_memoria_solo_en_desarrollo(monkeypatch):
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    assert isinstance(construir_publicador(), PublicadorMemoria)
    monkeypatch.delenv("SOLVENTA_MODO_DESARROLLO")
    with pytest.raises(ErrorConfiguracion):
        construir_publicador()


def test_la_aplicacion_arranca_y_responde_health(monkeypatch):
    for k, v in ENTORNO.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    from app.main import app

    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        r = cliente.post("/v1/suscripciones", json={"cotizacion_id": "x", "metodo_pago": "t", "acepta_terminos": True})
        assert r.status_code == 401 and r.json()["error"]["code"] == "NO_AUTENTICADO"
