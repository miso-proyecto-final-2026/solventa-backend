"""`GET /partners/v1/status` (criterio 7), versionado por ruta (criterio 8) y sin estado de negocio (criterio 9)."""

from app.socios.modelos_db import BaseSocios

from app.main import app
from tests.dobles import AlmacenCaido


def test_el_estado_es_publico_y_operativo(cliente):
    r = cliente.get("/partners/v1/status")

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["estado"] == "operativo"
    assert cuerpo["version"] == "v1"
    assert cuerpo["versiones_soportadas"] == ["v1"]
    assert cuerpo["dependencias"] == {"cotizacion": "ok", "base_datos": "ok", "almacen": "ok"}


def test_sin_cotizacion_no_esta_disponible(cliente, cotizaciones):
    cotizaciones.arriba = False

    r = cliente.get("/partners/v1/status")

    assert r.status_code == 503
    assert r.json()["estado"] == "no_disponible"
    assert r.json()["dependencias"]["cotizacion"] == "no_disponible"


def test_sin_almacen_no_esta_disponible(cliente):
    app.state.almacen = AlmacenCaido()

    assert cliente.get("/partners/v1/status").json()["estado"] == "no_disponible"


def test_sin_base_esta_degradado(cliente, monkeypatch):
    async def caida():
        return False

    monkeypatch.setattr(app.state.registro, "bd_disponible", caida)

    r = cliente.get("/partners/v1/status")

    assert r.status_code == 200
    assert r.json()["estado"] == "degradado"


def test_una_dependencia_lenta_cuenta_como_no_disponible(cliente, cotizaciones, monkeypatch):
    import asyncio

    async def lenta():
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(cotizaciones, "disponible", lenta)
    monkeypatch.setattr("app.socios.v1.api._TIMEOUT_ESTADO_S", 0.05)

    assert cliente.get("/partners/v1/status").json()["dependencias"]["cotizacion"] == "no_disponible"


def test_la_sonda_interna_queda_fuera_del_prefijo_publico(cliente):
    assert cliente.get("/health").json() == {"status": "ok"}


def test_las_rutas_publicas_estan_versionadas():
    rutas = set(app.openapi()["paths"])

    assert rutas == {
        "/oauth/token",
        "/partners/v1/cotizaciones",
        "/partners/v1/status",
        "/partners/v1/suscripciones",
        "/partners/v1/polizas/{suscripcion_id}",
        "/partners/v1/suscripciones/{suscripcion_id}/documento-firma",
        "/partners/v1/suscripciones/{suscripcion_id}/documento-firma/pdf",
        "/partners/v1/suscripciones/{suscripcion_id}/firma/codigo",
        "/partners/v1/suscripciones/{suscripcion_id}/firma",
    }
    assert all(r.startswith("/partners/v1/") for r in rutas - {"/oauth/token"})


def test_socios_no_tiene_tablas_de_negocio():
    assert set(BaseSocios.metadata.tables) == {"socios.clientes_api", "socios.contratos_api"}
