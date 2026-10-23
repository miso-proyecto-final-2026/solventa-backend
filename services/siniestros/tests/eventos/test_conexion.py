"""Parámetros de conexión a MSK: SASL/IAM (staging) o PLAINTEXT (solo para pruebas locales)."""

import pytest

from app.eventos.conexion import (
    ProveedorTokenIam,
    brokers_desde_entorno,
    parametros_seguridad,
)


def test_por_defecto_se_usa_iam_con_oauthbearer_sobre_tls():
    p = parametros_seguridad("IAM", region="us-east-1")

    assert p["security_protocol"] == "SASL_SSL" and p["sasl_mechanism"] == "OAUTHBEARER"
    assert isinstance(p["sasl_oauth_token_provider"], ProveedorTokenIam)


def test_plaintext_no_lleva_credenciales():
    assert parametros_seguridad("PLAINTEXT") == {"security_protocol": "PLAINTEXT"}


def test_iam_exige_region():
    with pytest.raises(ValueError, match="AWS_REGION"):
        parametros_seguridad("IAM", region=None)


def test_una_seguridad_desconocida_es_un_error():
    with pytest.raises(ValueError):
        parametros_seguridad("SIN_SEGURIDAD")


def test_se_lee_del_entorno(monkeypatch):
    monkeypatch.setenv("KAFKA_SEGURIDAD", "PLAINTEXT")

    assert parametros_seguridad() == {"security_protocol": "PLAINTEXT"}


async def test_el_token_se_firma_con_la_region_y_se_devuelve_sin_la_expiracion():
    llamadas = []

    def firmar(region):
        llamadas.append(region)
        return "token-firmado", 1_700_000_000_000

    assert await ProveedorTokenIam("us-east-1", firmar).token() == "token-firmado"
    assert llamadas == ["us-east-1"]


def test_los_brokers_se_leen_separados_por_coma(monkeypatch):
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "b-1:9098, b-2:9098")

    assert brokers_desde_entorno() == ["b-1:9098", "b-2:9098"]


def test_sin_brokers_falla_con_un_mensaje_claro(monkeypatch):
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)

    with pytest.raises(ValueError, match="KAFKA_BOOTSTRAP_SERVERS"):
        brokers_desde_entorno()
