"""Integración (HU20, criterio 4): cobro confirmado -> póliza -> PDF cifrado y versionado en S3 (Moto) -> `documento_s3_key`."""

import boto3
import pytest
from app.suscripcion.almacen import AlmacenLocal, AlmacenS3
from app.suscripcion.config import ConfigSuscripcion
from app.suscripcion.wiring import construir_almacen
from moto import mock_aws

from app.configuracion import ErrorConfiguracion
from tests.test_polizas import _cobrada, _generador, _poliza

BUCKET = "solventa-staging-polizas-prueba"


@pytest.fixture(autouse=True)
def _aws_falso(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "prueba")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "prueba")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


async def test_el_cobro_confirmado_deja_el_pdf_cifrado_en_s3_y_la_clave_en_la_poliza(armar, fabrica):
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        _, susc = await _cobrada(armar, fabrica)

        await _generador(fabrica, AlmacenS3(BUCKET)).procesar_pendientes()

        poliza = _poliza(fabrica, susc.id)
        assert poliza.documento_estado == "GENERADO"
        assert poliza.documento_s3_key == f"polizas/{susc.id}/{poliza.numero_poliza}.pdf"
        objeto = s3.get_object(Bucket=BUCKET, Key=poliza.documento_s3_key)
        assert objeto["Body"].read().startswith(b"%PDF")
        assert objeto["ServerSideEncryption"] == "AES256"
        assert objeto["VersionId"] != "null"


async def test_si_el_bucket_no_existe_el_documento_queda_pendiente_y_el_pago_intacto(armar, fabrica):
    with mock_aws():
        _, susc = await _cobrada(armar, fabrica)

        await _generador(fabrica, AlmacenS3("no-existe")).procesar_pendientes()

        poliza = _poliza(fabrica, susc.id)
        assert (poliza.documento_estado, poliza.documento_error) == ("PENDIENTE", "NoSuchBucket")


def test_con_bucket_el_almacen_es_s3():
    assert isinstance(construir_almacen(ConfigSuscripcion(polizas_bucket=BUCKET)), AlmacenS3)


def test_sin_bucket_en_desarrollo_el_almacen_es_la_carpeta_local(tmp_path):
    almacen = construir_almacen(ConfigSuscripcion(polizas_documentos_dir=str(tmp_path)))
    assert isinstance(almacen, AlmacenLocal)


def test_sin_bucket_fuera_de_desarrollo_no_arranca(monkeypatch):
    monkeypatch.delenv("SOLVENTA_MODO_DESARROLLO")
    with pytest.raises(ErrorConfiguracion, match="POLIZAS_BUCKET"):
        construir_almacen(ConfigSuscripcion())


def test_la_configuracion_de_polizas_se_lee_del_entorno():
    c = ConfigSuscripcion.desde_entorno(
        {
            "POLIZAS_BUCKET": "b",
            "POLIZAS_DOCUMENTOS_DIR": "/tmp/docs",
            "POLIZAS_VIGENCIA_MESES": "6",
            "POLIZAS_MAX_INTENTOS": "3",
            "POLIZAS_REINTENTO_BASE_S": "2",
            "POLIZAS_REINTENTO_MAXIMO_S": "20",
        }
    )
    assert (c.polizas_bucket, c.polizas_documentos_dir, c.polizas_vigencia_meses) == ("b", "/tmp/docs", 6)
    assert (c.polizas_max_intentos, c.polizas_reintento_base_s, c.polizas_reintento_maximo_s) == (3, 2.0, 20.0)


def test_por_defecto_la_vigencia_es_de_12_meses_y_hay_5_intentos():
    c = ConfigSuscripcion.desde_entorno({})
    assert (c.polizas_bucket, c.polizas_vigencia_meses, c.polizas_max_intentos) == (None, 12, 5)

