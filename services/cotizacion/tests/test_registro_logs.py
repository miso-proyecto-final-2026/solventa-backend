import logging

import pytest

from app.registro_logs import configurar_logs


@pytest.fixture(autouse=True)
def restaurar_raiz():
    raiz = logging.getLogger()
    nivel, manejadores = raiz.level, list(raiz.handlers)
    yield
    raiz.setLevel(nivel)
    raiz.handlers[:] = manejadores


def test_por_defecto_los_logs_de_la_aplicacion_salen_en_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    logging.getLogger().handlers.clear()

    configurar_logs()

    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("cotizacion.emision").isEnabledFor(logging.INFO)


def test_el_nivel_se_puede_cambiar_con_la_variable_de_entorno(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    logging.getLogger().handlers.clear()

    configurar_logs()

    assert logging.getLogger().level == logging.WARNING
    assert not logging.getLogger("cotizacion.emision").isEnabledFor(logging.INFO)


def test_un_nivel_desconocido_vuelve_a_info(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "ruidoso")
    logging.getLogger().handlers.clear()

    configurar_logs()

    assert logging.getLogger().level == logging.INFO


def test_llamarla_dos_veces_no_duplica_los_manejadores(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    logging.getLogger().handlers.clear()

    configurar_logs()
    configurar_logs()

    assert len(logging.getLogger().handlers) == 1
