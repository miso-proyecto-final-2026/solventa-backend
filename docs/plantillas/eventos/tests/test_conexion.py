"""Parámetros de conexión a MSK: SASL/IAM (staging) o PLAINTEXT (solo para pruebas locales)."""

import pytest
from eventos.conexion import (
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


# Defecto que solo apareció contra MSK real: aiokafka exige un ssl_context con SASL_SSL y, sin él, ni siquiera construye el cliente.
def test_iam_incluye_un_contexto_tls():
    import ssl

    p = parametros_seguridad("IAM", region="us-east-1")

    assert isinstance(p["ssl_context"], ssl.SSLContext)
    assert p["ssl_context"].verify_mode == ssl.CERT_REQUIRED and p["ssl_context"].check_hostname is True


def test_plaintext_no_lleva_contexto_tls():
    assert "ssl_context" not in parametros_seguridad("PLAINTEXT")


async def test_los_clientes_de_aiokafka_se_construyen_con_los_parametros_iam():
    """Construir no abre conexiones: es el único modo de ejercitar la ruta IAM sin un MSK."""
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    seguridad = parametros_seguridad("IAM", region="us-east-1")

    AIOKafkaProducer(bootstrap_servers=["b-1:9098"], **seguridad)
    AIOKafkaConsumer("t", bootstrap_servers=["b-1:9098"], group_id="g", **seguridad)
