"""Revocación simultánea contra PostgreSQL real (HU07, criterio 6).

Varias réplicas revocan a la vez el mismo consentimiento: todas responden 200 con el mismo estado y solo
queda un registro de auditoría `REVOCADO`. Se omite sola si Docker no está disponible.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models import Consentimiento, ConsentimientoRegistro
from app.routers.clientes import get_db as get_db_dependencia
from tests.helpers import registrar
from tests.test_migraciones import alembic, base_migrada, engine_pg, postgres_url  # noqa: F401

pytestmark = pytest.mark.contenedores

REPLICAS = 8
TIPO = "OPEN_FINANCE_OPEN_DATA"


def test_varias_replicas_revocando_a_la_vez_dejan_un_solo_registro(base_migrada):  # noqa: F811
    Sesion = sessionmaker(bind=base_migrada, autoflush=False, autocommit=False)

    def get_db_pg():
        sesion = Sesion()
        try:
            yield sesion
        finally:
            sesion.close()

    app.dependency_overrides[get_db_dependencia] = get_db_pg

    @event.listens_for(base_migrada, "after_cursor_execute")
    def _alargar_la_lectura(conn, cursor, statement, parameters, context, executemany):
        # Ensancha la ventana entre leer el estado y escribir la revocación: sin bloqueo de fila, todas las
        # réplicas leen OTORGADO antes de que alguna escriba. Con FOR UPDATE se serializan.
        if "FROM identidad.consentimientos" in statement and statement.lstrip().startswith("SELECT"):
            time.sleep(0.3)

    cliente_id, auth = registrar(TestClient(app))
    TestClient(app).post(
        f"/v1/clientes/{cliente_id}/consentimientos",
        json={"tipo": TIPO, "version_texto": "v1", "canal": "MOVIL"},
        headers=auth,
    )
    barrera = threading.Barrier(REPLICAS)
    resultados: list[tuple[int, dict]] = []

    def replica():
        http = TestClient(app)
        barrera.wait()  # todas disparan a la vez
        r = http.delete(f"/v1/clientes/{cliente_id}/consentimientos/{TIPO}", headers=auth)
        resultados.append((r.status_code, r.json()))

    try:
        hilos = [threading.Thread(target=replica) for _ in range(REPLICAS)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=60)
    finally:
        app.dependency_overrides.pop(get_db_dependencia, None)
        event.remove(base_migrada, "after_cursor_execute", _alargar_la_lectura)

    assert [c for c, _ in resultados] == [200] * REPLICAS, resultados
    assert len({cuerpo["fecha_revocacion"] for _, cuerpo in resultados}) == 1
    with Sesion() as s:
        assert s.query(ConsentimientoRegistro).filter_by(evento="REVOCADO").count() == 1
        assert s.query(Consentimiento).one().estado == "REVOCADO"
