"""Idempotencia de POST /v1/clientes entre réplicas, contra PostgreSQL real (HU01, criterio 6).

Cada hilo es una «réplica»: su propio cliente HTTP y su propia sesión de base de datos, sin memoria
compartida. Se omite sola si Docker no está disponible.
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models import Cliente, IdempotenciaCliente
from app.routers.clientes import get_db as get_db_dependencia
from tests.test_clientes import _payload
from tests.test_migraciones import alembic, base_migrada, engine_pg, postgres_url  # noqa: F401

pytestmark = pytest.mark.contenedores

REPLICAS = 8


def test_el_mismo_reintento_en_varias_replicas_crea_una_sola_cuenta(base_migrada):  # noqa: F811
    Sesion = sessionmaker(bind=base_migrada, autoflush=False, autocommit=False)

    def get_db_pg():
        sesion = Sesion()
        try:
            yield sesion
        finally:
            sesion.close()

    app.dependency_overrides[get_db_dependencia] = get_db_pg
    barrera = threading.Barrier(REPLICAS)
    resultados: list[tuple[int, dict]] = []

    def replica():
        cliente_http = TestClient(app)
        barrera.wait()  # todas disparan a la vez
        r = cliente_http.post(
            "/v1/clientes", json=_payload(), headers={"Idempotency-Key": "reintento-concurrente"}
        )
        resultados.append((r.status_code, r.json()))

    try:
        hilos = [threading.Thread(target=replica) for _ in range(REPLICAS)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=60)
    finally:
        app.dependency_overrides.pop(get_db_dependencia, None)

    assert len(resultados) == REPLICAS
    assert [c for c, _ in resultados] == [201] * REPLICAS, resultados
    assert len({cuerpo["id"] for _, cuerpo in resultados}) == 1
    # Cada réplica recibió su propia sesión, sin que se haya guardado ninguno de los tokens.
    assert len({cuerpo["access_token"] for _, cuerpo in resultados}) == REPLICAS
    with Sesion() as s:
        assert s.query(Cliente).count() == 1
        assert s.query(IdempotenciaCliente).count() == 1
