"""Trazabilidad de consultas contra PostgreSQL y Redis reales (HU11).

Se omiten solas si Docker no está disponible.
"""

import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import exc, text
from sqlalchemy.orm import sessionmaker

from app.cotizacion.modelos_db import SCHEMA
from app.cotizacion.repositorio import RepositorioTrazabilidad
from app.proveedores.dominio import RegistroTrazabilidad, ResultadoConsulta
from tests.cotizacion.test_modulo_contenedores import _cotizar

pytestmark = pytest.mark.contenedores

CLAVE = "clave-auditor-contenedores"
H = {"X-Service-Key": CLAVE}


def _esperar_consultas(client, cotizacion_id, esperadas=2, segundos=5.0):
    """La escritura es asíncrona: se espera a que aparezca, con un tope."""
    limite = time.monotonic() + segundos
    while True:
        r = client.get(
            f"/v1/cotizaciones/{cotizacion_id}/consultas-externas", headers=H
        )
        if len(r.json().get("consultas", [])) >= esperadas or time.monotonic() > limite:
            return r
        time.sleep(0.05)


def test_flujo_real_de_la_solicitud_a_las_consultas_auditables(app_real, base_migrada):
    client = app_real(AUDITOR_API_KEYS=CLAVE)
    cliente_id = str(uuid.uuid4())
    c = _cotizar(client, cliente_id)

    r = _esperar_consultas(client, c["id"])

    assert r.status_code == 200
    consultas = r.json()["consultas"]
    assert {x["proveedor"] for x in consultas} == {"open_finance", "open_data"}
    assert {x["resultado"] for x in consultas} == {"OK"}
    assert {x["cotizacion_id"] for x in consultas} == {c["id"]}
    assert {x["cliente_id"] for x in consultas} == {cliente_id}
    assert all(x["consentimiento_id"] and x["correlacion_id"] for x in consultas)
    assert "4500000" not in r.text and "empleado" not in r.text  # sin payloads


def test_la_correlacion_de_la_solicitud_llega_a_la_base(app_real, base_migrada):
    client = app_real(AUDITOR_API_KEYS=CLAVE)
    r = client.post(
        "/v1/cotizaciones",
        json={
            "cliente_id": str(uuid.uuid4()),
            "producto": "microseguro_vida",
            "datos": {"edad": 35, "suma_asegurada": 50_000_000},
        },
        headers={"X-Correlation-Id": "corr-e2e-1"},
    )
    assert r.headers["X-Correlation-Id"] == "corr-e2e-1"

    consultas = _esperar_consultas(client, r.json()["id"]).json()["consultas"]

    assert {x["correlacion_id"] for x in consultas} == {"corr-e2e-1"}


def test_la_segunda_cotizacion_del_cliente_registra_los_aciertos_de_cache(
    app_real, base_migrada
):
    client = app_real(AUDITOR_API_KEYS=CLAVE)
    cliente_id = str(uuid.uuid4())
    _cotizar(client, cliente_id)
    segunda = _cotizar(client, cliente_id)

    consultas = _esperar_consultas(client, segunda["id"]).json()["consultas"]

    assert {x["hit_cache"] for x in consultas} == {True}


def test_la_base_rechaza_modificar_o_borrar_la_trazabilidad(app_real, base_migrada):
    client = app_real(AUDITOR_API_KEYS=CLAVE)
    c = _cotizar(client)
    _esperar_consultas(client, c["id"])

    for sentencia in (
        f"UPDATE {SCHEMA}.trazabilidad_consultas SET resultado = 'OK'",
        f"DELETE FROM {SCHEMA}.trazabilidad_consultas",
    ):
        with (
            pytest.raises(exc.DBAPIError, match="solo inserción"),
            base_migrada.begin() as conexion,
        ):
            conexion.execute(text(sentencia))

    with base_migrada.connect() as conexion:
        total = conexion.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.trazabilidad_consultas")
        ).scalar()
    assert total >= 2


def test_un_cliente_id_muy_largo_no_rompe_la_escritura_ni_la_consulta(
    app_real, base_migrada
):
    client = app_real()
    repo = RepositorioTrazabilidad(sessionmaker(base_migrada, expire_on_commit=False))

    repo.insertar_lote(
        [
            RegistroTrazabilidad(
                id=str(uuid.uuid4()),
                cliente_id="x" * 200,
                consentimiento_id="c" * 200,
                proveedor="open_data",
                categorias=("estrato",),
                consultado_en=datetime.now(UTC),
                resultado=ResultadoConsulta.SIN_CONSENTIMIENTO,
                latencia_ms=0.0,
                hit_cache=False,
            )
        ]
    )

    with base_migrada.connect() as conexion:
        longitud = conexion.execute(
            text(f"SELECT length(cliente_id) FROM {SCHEMA}.trazabilidad_consultas")
        ).scalar()
    with base_migrada.connect() as conexion:
        consentimiento = conexion.execute(
            text(
                f"SELECT length(consentimiento_id) FROM {SCHEMA}.trazabilidad_consultas"
            )
        ).scalar()
    assert (longitud, consentimiento) == (64, 64)
    assert client is not None
