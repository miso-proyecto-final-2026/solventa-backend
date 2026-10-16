"""Almacenes de evidencias KYC: disco local (desarrollo) y S3 cifrado."""

import stat

import boto3
import pytest
from moto import mock_aws

from app.kyc.almacen import AlmacenLocal, AlmacenS3, construir_almacen
from app.kyc.config import ConfigKyc

BUCKET = "solventa-kyc-prueba"


def test_el_almacen_local_guarda_el_contenido_con_permisos_restringidos(tmp_path):
    almacen = AlmacenLocal(str(tmp_path))

    almacen.guardar("kyc/c1/v1/selfie.jpg", b"contenido", "image/jpeg")

    destino = tmp_path / "kyc/c1/v1/selfie.jpg"
    assert destino.read_bytes() == b"contenido"
    assert stat.S_IMODE(destino.stat().st_mode) == 0o600


@pytest.mark.parametrize("clave", ["../fuera.jpg", "kyc/../../fuera.jpg", "/etc/pasa.jpg"])
def test_el_almacen_local_no_escribe_fuera_de_su_directorio(tmp_path, clave):
    with pytest.raises(ValueError):
        AlmacenLocal(str(tmp_path / "evidencias")).guardar(clave, b"x", "image/jpeg")


@mock_aws
def test_el_almacen_s3_guarda_cifrado_con_su_tipo():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)

    AlmacenS3(BUCKET, s3).guardar("kyc/c1/v1/anverso.png", b"contenido", "image/png")

    objeto = s3.get_object(Bucket=BUCKET, Key="kyc/c1/v1/anverso.png")
    assert objeto["Body"].read() == b"contenido"
    assert objeto["ServerSideEncryption"] == "AES256"
    assert objeto["ContentType"] == "image/png"


def test_con_bucket_configurado_se_usa_s3_y_sin_bucket_el_disco_local(tmp_path):
    con_bucket = construir_almacen(ConfigKyc(bucket_evidencias=BUCKET))
    sin_bucket = construir_almacen(ConfigKyc(directorio_local=str(tmp_path)))

    assert isinstance(con_bucket, AlmacenS3)
    assert isinstance(sin_bucket, AlmacenLocal)


def test_la_configuracion_kyc_sale_del_entorno():
    config = ConfigKyc.desde_entorno(
        {
            "KYC_TIMEOUT_S": "2.5",
            "KYC_MAX_INTENTOS": "5",
            "KYC_BLOQUEO_MIN": "10",
            "KYC_MAX_BYTES_IMAGEN": "1000",
            "KYC_EXIGIR_HTTPS": "true",
            "KYC_EVIDENCIAS_BUCKET": BUCKET,
        }
    )

    assert (config.timeout_s, config.max_intentos, config.bloqueo_min) == (2.5, 5, 10)
    assert config.max_bytes_imagen == 1000
    assert config.exigir_https is True
    assert config.bucket_evidencias == BUCKET


def test_los_valores_por_defecto_son_los_de_la_historia():
    config = ConfigKyc.desde_entorno({})

    assert config.timeout_s == 5.0
    assert config.max_intentos == 3
    assert config.exigir_https is False
    assert config.bucket_evidencias is None
