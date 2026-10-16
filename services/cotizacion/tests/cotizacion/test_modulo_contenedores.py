"""Pruebas de módulo con PostgreSQL y Redis reales en contenedores (HU12).

Se omiten solas si Docker no está disponible. Ejercitan la app completa
(lifespan, Redis cache-aside, Alembic y Postgres) como en el ambiente real.
"""

import uuid

import pytest
import redis as redis_sync
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.cotizacion.modelos_db import SCHEMA

VIDA = {
    "producto": "microseguro_vida",
    "datos": {"edad": 35, "suma_asegurada": 50_000_000},
}

pytestmark = pytest.mark.contenedores


@pytest.fixture
def app_real(base_migrada, postgres_url, redis_url, monkeypatch):
    """La app completa, con la configuración tomada del entorno como en producción."""
    monkeypatch.setenv("COTIZACION_DATABASE_URL", postgres_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    for prefijo in ("OPEN_FINANCE_SIM", "OPEN_DATA_SIM"):
        monkeypatch.setenv(f"{prefijo}_LATENCIA_MIN_MS", "0")
        monkeypatch.setenv(f"{prefijo}_LATENCIA_MAX_MS", "2")

    def _abrir(**env) -> TestClient:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from app.main import app

        cliente = TestClient(app)
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente

    abiertos: list[TestClient] = []
    yield _abrir
    for c in abiertos:
        c.__exit__(None, None, None)


def _cotizar(client: TestClient, cliente_id: str | None = None) -> dict:
    r = client.post(
        "/v1/cotizaciones", json={"cliente_id": cliente_id or str(uuid.uuid4()), **VIDA}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_flujo_completo_persiste_en_postgres_y_usa_la_cache_de_redis(
    app_real, base_migrada
):
    client = app_real()
    cliente_id = str(uuid.uuid4())

    primera = _cotizar(client, cliente_id)
    segunda = _cotizar(client, cliente_id)

    assert primera["prima_anual"] == segunda["prima_anual"] == 175_000
    assert (primera["metadata"]["hit_cache"], segunda["metadata"]["hit_cache"]) == (
        False,
        True,
    )
    with base_migrada.connect() as c:
        cot = (
            c.execute(
                text(f'SELECT * FROM "{SCHEMA}".cotizaciones WHERE id = :i'),
                {"i": primera["id"]},
            )
            .mappings()
            .one()
        )
        coberturas = c.execute(
            text(
                f'SELECT tipo, monto, deducible FROM "{SCHEMA}".coberturas WHERE cotizacion_id = :i ORDER BY orden'
            ),
            {"i": primera["id"]},
        ).all()
        logs = (
            c.execute(
                text(
                    f'SELECT * FROM "{SCHEMA}".log_latencia WHERE cotizacion_id IN (:a, :b) ORDER BY id'
                ),
                {"a": primera["id"], "b": segunda["id"]},
            )
            .mappings()
            .all()
        )
    assert (
        cot["cliente_id"],
        cot["estado"],
        cot["moneda"],
        int(cot["prima_anual"]),
    ) == (
        cliente_id,
        "EMITIDA",
        "COP",
        175_000,
    )
    assert cot["version_reglas"] == "2026.10.1-prototipo"
    assert [(t, int(m), int(d)) for t, m, d in coberturas] == [
        ("muerte", 50_000_000, 0),
        ("incapacidad_total", 25_000_000, 0),
    ]
    assert [(lg["hit_cache"], lg["degradado"]) for lg in logs] == [
        (False, False),
        (True, False),
    ]
    assert all(lg["latencia_interna_ms"] > 0 for lg in logs)
    assert all(lg["latencia_cliente_ms"] is None for lg in logs)


def test_proveedor_caido_cotiza_degradado_y_lo_registra(app_real, base_migrada):
    client = app_real(OPEN_FINANCE_SIM_MODO="error_5xx", OPEN_DATA_SIM_MODO="error_5xx")

    c = _cotizar(client)

    assert (c["degradado"], c["prima_anual"]) == (True, 201_250)
    assert c["componentes"][-1] == {"concepto": "recargo_degradado", "monto": 26_250}
    with base_migrada.connect() as conn:
        log = conn.execute(
            text(
                f'SELECT degradado FROM "{SCHEMA}".log_latencia WHERE cotizacion_id = :i'
            ),
            {"i": c["id"]},
        ).scalar_one()
    assert log is True


def test_cliente_reporta_latencia_y_se_completa_en_postgres(app_real, base_migrada):
    client = app_real()
    c = _cotizar(client)

    r = client.put(
        f"/v1/cotizaciones/{c['id']}/latencia-cliente",
        json={"latencia_cliente_ms": 231.5},
    )

    assert r.status_code == 204
    with base_migrada.connect() as conn:
        valor = conn.execute(
            text(
                f'SELECT latencia_cliente_ms FROM "{SCHEMA}".log_latencia WHERE cotizacion_id = :i'
            ),
            {"i": c["id"]},
        ).scalar_one()
    assert valor == 231.5


def test_la_cache_de_redis_guarda_el_perfil_por_cliente(app_real, redis_url):
    client = app_real()
    cliente_id = str(uuid.uuid4())
    _cotizar(client, cliente_id)

    claves = redis_sync.from_url(redis_url).keys(f"perfil:{cliente_id}:*")

    assert claves  # open_finance y open_data, con su respaldo


def test_latencia_interna_p95_dentro_del_presupuesto_con_cache_caliente(
    app_real, base_migrada
):
    """Humo del presupuesto interno (criterio 8): <= 150 ms p95 con la caché tibia."""
    client = app_real()
    cliente_id = str(uuid.uuid4())
    _cotizar(client, cliente_id)  # calienta la caché

    latencias = sorted(
        _cotizar(client, cliente_id)["metadata"]["latencia_interna_ms"]
        for _ in range(60)
    )

    p95 = latencias[int(len(latencias) * 0.95) - 1]
    assert p95 <= 150, f"p95 interno {p95} ms supera el presupuesto"
