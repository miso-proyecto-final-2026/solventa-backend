"""Formato es-CO, almacenes de documentos (carpeta local y S3 con Moto) y redacción de los logs (HU20)."""

import json
import logging
import os
import stat
import sys
from datetime import UTC, datetime

import boto3
import pytest
from app.logs import FiltroDatosSensibles, FormateadorJson, redactar
from app.suscripcion import formato_es_co as fmt
from app.suscripcion.almacen import AlmacenLocal, AlmacenS3
from moto import mock_aws

from app.contexto import correlacion_id, traza_id

BUCKET = "solventa-test-polizas"
CLAVE = "polizas/susc-1/ABCDEFGHJKMNPQRSTVWX.pdf"


@pytest.fixture(autouse=True)
def _credenciales_falsas(monkeypatch):
    for variable in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(variable, "prueba")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [(0, "$ 0"), (999, "$ 999"), (14_583, "$ 14.583"), (1_234_567, "$ 1.234.567"), (40_000_000, "$ 40.000.000")],
)
def test_los_montos_van_en_cop_sin_decimales_y_con_punto_de_miles(valor, esperado):
    assert fmt.monto(valor) == esperado


def test_las_fechas_van_en_espanol_y_en_hora_de_colombia():
    assert fmt.fecha(datetime(2026, 10, 26, 12, 0, tzinfo=UTC)) == "26 de octubre de 2026"
    assert fmt.fecha(datetime(2027, 1, 1, 3, 0, tzinfo=UTC)) == "31 de diciembre de 2026"  # 22:00 del día anterior en Bogotá


def test_el_almacen_local_guarda_el_pdf(tmp_path):
    AlmacenLocal(str(tmp_path)).guardar(CLAVE, b"%PDF-x", "application/pdf")
    assert (tmp_path / CLAVE).read_bytes() == b"%PDF-x"


@pytest.mark.skipif(sys.platform == "win32", reason="los permisos POSIX no aplican en Windows")
def test_el_almacen_local_crea_el_archivo_con_permisos_restringidos(tmp_path):
    AlmacenLocal(str(tmp_path)).guardar(CLAVE, b"x", "application/pdf")
    assert stat.S_IMODE(os.stat(tmp_path / CLAVE).st_mode) == 0o600


def test_el_almacen_local_rechaza_claves_que_salen_de_la_carpeta(tmp_path):
    with pytest.raises(ValueError):
        AlmacenLocal(str(tmp_path)).guardar("../fuera.pdf", b"x", "application/pdf")


@mock_aws
def test_el_almacen_s3_guarda_cifrado_y_versionado():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    almacen = AlmacenS3(BUCKET, s3)

    almacen.guardar(CLAVE, b"%PDF-1", "application/pdf")
    almacen.guardar(CLAVE, b"%PDF-2", "application/pdf")  # un reintento sobrescribe la clave: versión nueva

    cabecera = s3.head_object(Bucket=BUCKET, Key=CLAVE)
    assert cabecera["ServerSideEncryption"] == "AES256"
    assert cabecera["ContentType"] == "application/pdf"
    assert s3.get_object(Bucket=BUCKET, Key=CLAVE)["Body"].read() == b"%PDF-2"
    assert len(s3.list_object_versions(Bucket=BUCKET, Prefix=CLAVE)["Versions"]) == 2


def test_los_logs_redactan_tokens_y_numeros_largos():
    assert redactar("token tok_sim_abc123 y tarjeta 4111 1111 1111 1111") == "token [token-redactado] y tarjeta [numero-redactado]"


def test_el_formato_json_lleva_trace_id_y_correlation_id_y_el_filtro_redacta():
    registro = logging.LogRecord("suscripcion", logging.INFO, __file__, 1, "pago con %s", ("tok_sim_zzz",), None)
    t1, t2 = traza_id.set("traza-1"), correlacion_id.set("corr-1")
    try:
        FiltroDatosSensibles().filter(registro)
        linea = json.loads(FormateadorJson().format(registro))
    finally:
        traza_id.reset(t1)
        correlacion_id.reset(t2)

    assert linea["trace_id"] == "traza-1" and linea["correlation_id"] == "corr-1"
    assert linea["message"] == "pago con [token-redactado]"
