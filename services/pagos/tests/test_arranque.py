"""Ensamblado: configuración desde el entorno y arranque de la aplicación."""

import pytest
from app.eventos import PublicadorMemoria
from app.pagos.config import PERIODO_GRACIA_DIAS_PROVISIONAL, ConfigPagos
from app.pagos.simulador import PasarelaSimulada
from app.pagos.wiring import construir_ensamblado
from fastapi.testclient import TestClient

from app.configuracion import ErrorConfiguracion

ENTORNO = {"PAGOS_DATABASE_URL": "sqlite://"}


def test_la_configuracion_se_lee_del_entorno():
    c = ConfigPagos.desde_entorno(
        {
            **ENTORNO,
            "PAGOS_API_KEYS": "suscripcion=k1,k2",
            "PASARELA_SIM_MODO": "rechazar",
            "PERIODO_GRACIA_DIAS": "5",
            "PASARELA_TIMEOUT_S": "2",
        }
    )
    assert (c.database_url, c.api_keys, c.simulador_modo, c.periodo_gracia_dias, c.pasarela_timeout_s) == (
        "sqlite://", {"k1": "suscripcion", "k2": "servicio"}, "RECHAZAR", 5, 2.0,
    )


def test_los_valores_por_defecto_son_los_documentados():
    c = ConfigPagos.desde_entorno({})
    assert (c.pasarela_proveedor, c.simulador_modo, c.periodo_gracia_dias, c.api_keys) == (
        "simulador", "APROBAR", PERIODO_GRACIA_DIAS_PROVISIONAL, {},
    )


def test_sin_base_de_datos_solo_arranca_en_modo_desarrollo(monkeypatch):
    monkeypatch.delenv("SOLVENTA_MODO_DESARROLLO")
    with pytest.raises(ErrorConfiguracion):
        construir_ensamblado(ConfigPagos.desde_entorno({}))


def test_sin_brokers_el_publicador_es_en_memoria_y_no_hay_consumidor_solo_en_desarrollo(monkeypatch):
    ensamblado = construir_ensamblado(ConfigPagos.desde_entorno(ENTORNO))
    assert isinstance(ensamblado.publicador, PublicadorMemoria) and ensamblado.consumidor is None
    assert isinstance(ensamblado.servicio._pasarela, PasarelaSimulada)
    monkeypatch.delenv("SOLVENTA_MODO_DESARROLLO")
    with pytest.raises(ErrorConfiguracion):
        construir_ensamblado(ConfigPagos.desde_entorno(ENTORNO))


async def test_con_brokers_se_arma_el_consumidor_del_grupo_del_servicio(monkeypatch):
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    monkeypatch.setenv("KAFKA_SEGURIDAD", "PLAINTEXT")
    ensamblado = construir_ensamblado(ConfigPagos.desde_entorno({**ENTORNO, "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092"}))
    assert ensamblado.consumidor is not None
    assert (ensamblado.consumidor._topic, ensamblado.consumidor._grupo) == ("solventa.suscripcion.comandos", "solventa-pagos.cobros")


def test_la_aplicacion_arranca_y_responde_health(monkeypatch):
    for k, v in ENTORNO.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    from app.main import app

    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        r = cliente.get("/v1/pagos/transacciones/x")
        assert r.status_code == 401 and r.json()["error"]["code"] == "NO_AUTENTICADO"
