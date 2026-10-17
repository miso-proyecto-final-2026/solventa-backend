"""Desglose del precio contra PostgreSQL y Redis reales (HU14).

Se omiten solas si Docker no está disponible.
"""

import statistics
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import exc, text
from sqlalchemy.orm import sessionmaker

from app.cotizacion.dominio import CotizacionEmitida, PerfilUnificado
from app.cotizacion.modelos_db import SCHEMA
from app.cotizacion.motor import calcular_prima
from app.cotizacion.repositorio import RepositorioCotizaciones
from tests.cotizacion.test_modulo_contenedores import VIDA, _cotizar

pytestmark = pytest.mark.contenedores

CLAVE = "clave-auditor-contenedores"
AHORA = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def test_flujo_real_perfil_de_redis_a_desglose_consultable(app_real, base_migrada):
    client = app_real(AUDITOR_API_KEYS=CLAVE)
    cliente_id = str(uuid.uuid4())
    c = _cotizar(client, cliente_id)

    r = client.get(
        f"/v1/cotizaciones/{c['id']}/desglose", headers={"X-Service-Key": CLAVE}
    )

    assert r.status_code == 200
    d = r.json()
    assert d["prima_anual"] == c["prima_anual"]
    assert sum(x["monto"] for x in d["componentes"]) == d["prima_anual"]
    assert cliente_id not in r.text  # sin datos personales
    senales = [f for f in d["factores"] if f["etapa"] == "senal"]
    assert senales
    assert all(
        f["fuente"] and f["obtenido_en"] for f in senales
    )  # HU09 + HU10 llegan hasta aquí
    assert (
        client.get(f"/v1/cotizaciones/{c['id']}").json()["componentes"]
        == c["componentes"]
    )


def test_el_desglose_no_se_consulta_sin_credencial_en_la_app_real(
    app_real, base_migrada
):
    client = app_real(AUDITOR_API_KEYS=CLAVE)
    c = _cotizar(client)

    assert client.get(f"/v1/cotizaciones/{c['id']}/desglose").status_code == 401
    assert (
        client.get(
            f"/v1/cotizaciones/{c['id']}/desglose", headers={"X-Service-Key": "x"}
        ).status_code
        == 403
    )


def test_la_base_rechaza_modificar_o_borrar_un_desglose(app_real, base_migrada):
    client = app_real()
    c = _cotizar(client)

    for sentencia in (
        f"UPDATE {SCHEMA}.desgloses_precio SET version_esquema = 2",
        f"DELETE FROM {SCHEMA}.desgloses_precio",
        f"UPDATE {SCHEMA}.desgloses_precio SET detalle = '{{}}'::json",
    ):
        with (
            pytest.raises(exc.DBAPIError, match="solo inserción"),
            base_migrada.begin() as conexion,
        ):
            conexion.execute(text(sentencia))

    with base_migrada.connect() as conexion:
        filas = conexion.execute(
            text(
                f"SELECT cotizacion_id, version_esquema FROM {SCHEMA}.desgloses_precio"
            )
        ).all()
    assert [tuple(f) for f in filas] == [(c["id"], 1)]


def test_no_se_puede_insertar_un_segundo_desglose_para_la_misma_cotizacion(
    app_real,
    base_migrada,
):
    client = app_real()
    c = _cotizar(client)

    with pytest.raises(exc.IntegrityError), base_migrada.begin() as conexion:
        conexion.execute(
            text(
                f"INSERT INTO {SCHEMA}.desgloses_precio "
                "(cotizacion_id, version_esquema, detalle, created_at) "
                "VALUES (:id, 1, '{}'::json, now())"
            ),
            {"id": c["id"]},
        )


def _emitida(reglas, desglose: bool) -> CotizacionEmitida:
    perfil = PerfilUnificado(
        "x",
        senales={"ratio_endeudamiento": 0.32, "reportes_negativos": 0},
        fuentes=("open_finance", "open_data"),
        procedencia={"ratio_endeudamiento": ("open_finance", AHORA)},
    )
    r = calcular_prima(VIDA["producto"], VIDA["datos"], perfil, reglas)
    c = CotizacionEmitida(
        id=str(uuid.uuid4()),
        cliente_id=str(uuid.uuid4()),
        producto=r.producto,
        estado="EMITIDA",
        moneda=r.moneda,
        prima_anual=r.prima_anual,
        prima_mensual=r.prima_mensual,
        coberturas=r.coberturas,
        componentes=r.componentes,
        version_reglas=r.version_reglas,
        degradado=r.degradado,
        aproximada=r.aproximada,
        vigente_hasta=AHORA + timedelta(days=30),
        creada_en=AHORA,
        desglose=r.desglose,
    )
    return c if desglose else replace(c, desglose=None)


def _p95(muestras: list[float]) -> float:
    return sorted(muestras)[int(len(muestras) * 0.95) - 1]


def test_persistir_el_desglose_no_aumenta_de_forma_medible_el_p95(
    base_migrada,
    reglas,
):
    """Criterio 7: el desglose se escribe en la misma transacción que la cotización."""
    import time

    repo = RepositorioCotizaciones(sessionmaker(base_migrada, expire_on_commit=False))

    def medir(con_desglose: bool, n: int = 300) -> list[float]:
        muestras = []
        for i in range(n + 20):
            c = _emitida(reglas, con_desglose)
            t = time.perf_counter()
            repo.guardar(c, dict(VIDA["datos"]))
            if i >= 20:  # descarta el calentamiento
                muestras.append((time.perf_counter() - t) * 1000)
        return muestras

    # intercaladas para que la deriva de la máquina afecte a ambas por igual
    sin_a, con_a = medir(False), medir(True)
    sin_b, con_b = medir(False), medir(True)
    sin, con = sin_a + sin_b, con_a + con_b
    delta_p95 = _p95(con) - _p95(sin)
    print(
        f"\nguardar cotización: p95 sin desglose={_p95(sin):.2f} ms, "
        f"con desglose={_p95(con):.2f} ms, delta={delta_p95:.2f} ms "
        f"(mediana {statistics.median(sin):.2f} -> {statistics.median(con):.2f} ms)"
    )

    assert delta_p95 <= 5.0, (
        f"el desglose suma {delta_p95:.2f} ms al p95 de la escritura"
    )
