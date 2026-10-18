"""Pruebas de la trazabilidad de consultas a proveedores externos (HU11)."""

import asyncio
import inspect as inspeccion
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.contexto import correlacion_id, cotizacion_en_curso
from app.correlacion import CABECERA, MiddlewareCorrelacion, normalizar
from app.cotizacion.api import router
from app.cotizacion.errores import registrar_errores
from app.cotizacion.perfil import AdaptadorPerfilProveedores
from app.cotizacion.repositorio import (
    RepositorioCotizaciones,
    RepositorioTrazabilidad,
    crear_engine,
    crear_tablas_locales,
)
from app.cotizacion.servicio import ServicioCotizacion
from app.cotizacion.trazabilidad import EscritorTrazabilidad
from app.proveedores.cache import CacheMemoria
from app.proveedores.config import ConfigProveedores
from app.proveedores.consentimiento import ConsentimientoHttp, ConsentimientoStub
from app.proveedores.dominio import (
    CATEGORIAS_POR_FUENTE,
    EstadoConsentimiento,
    Fuente,
    RegistroTrazabilidad,
    ResultadoConsulta,
    VerificacionConsentimiento,
)
from app.proveedores.servicio import ServicioPerfil
from app.proveedores.simuladores import (
    ConfigSimulador,
    ModoFalla,
    SimuladorOpenData,
    SimuladorOpenFinance,
)
from tests.cotizacion.test_api import DISP, VIDA

RAPIDO = {"latencia_min_ms": 0.0, "latencia_max_ms": 1.0}
CLAVE = "clave-auditor"
H = {"X-Service-Key": CLAVE}
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
# valores que devuelven los simuladores y que jamás deben aparecer en la huella
PAYLOAD_SENSIBLE = ("4500000", "0.32", "empleado", "monthlyIncomeCop", "debtRatio")


async def _sin_espera(_s: float) -> None:
    return None


class Entorno:
    def __init__(self):
        self.engine = crear_engine("sqlite:///:memory:")
        crear_tablas_locales(self.engine)
        fabrica = sessionmaker(self.engine, expire_on_commit=False)
        self.repo_traza = RepositorioTrazabilidad(fabrica)
        self.repo_cot = RepositorioCotizaciones(fabrica)
        self.escritor = EscritorTrazabilidad(self.repo_traza, dormir=_sin_espera)

    def servicio_perfil(
        self,
        modo_of=ModoFalla.NINGUNO,
        modo_od=ModoFalla.NINGUNO,
        consentimiento=None,
        **cfg,
    ) -> ServicioPerfil:
        servicio = ServicioPerfil(
            {
                Fuente.OPEN_FINANCE: SimuladorOpenFinance(
                    ConfigSimulador(modo=modo_of, **RAPIDO)
                ),
                Fuente.OPEN_DATA: SimuladorOpenData(
                    ConfigSimulador(modo=modo_od, **RAPIDO)
                ),
            },
            CacheMemoria(),
            consentimiento or ConsentimientoStub(EstadoConsentimiento.OTORGADO),
            ConfigProveedores(**{"timeout_s": 0.05, **cfg}),
        )
        servicio.conectar_trazabilidad(self.escritor.encolar)
        return servicio

    def app(self, servicio_perfil: ServicioPerfil, reglas) -> FastAPI:
        cotizacion = ServicioCotizacion(
            reglas,
            AdaptadorPerfilProveedores(servicio_perfil),
            self.repo_cot,
            trazabilidad=self.repo_traza,
        )
        app = FastAPI()
        app.include_router(router)
        registrar_errores(app)
        app.add_middleware(MiddlewareCorrelacion)
        app.state.servicio_cotizacion = cotizacion
        app.state.auditor_api_keys = (CLAVE,)
        return app

    async def registros(self) -> list[RegistroTrazabilidad]:
        await self.escritor.vaciar()
        with self.engine.connect() as c:
            ids = [
                r[0]
                for r in c.exec_driver_sql(
                    "SELECT cotizacion_id FROM trazabilidad_consultas"
                )
            ]
        todos = []
        for cid in {i for i in ids if i}:
            todos += self.repo_traza.listar_por_cotizacion(cid)
        return todos

    def filas(self) -> list[dict]:
        with self.engine.connect() as c:
            cursor = c.exec_driver_sql(
                "SELECT * FROM trazabilidad_consultas ORDER BY consultado_en, id"
            )
            return [dict(r._mapping) for r in cursor]


@pytest.fixture
def entorno():
    e = Entorno()
    yield e
    e.engine.dispose()


def _cliente(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


# --- criterio 1: registro por consulta, un resultado por cada caso -----------------


async def test_consulta_exitosa_registra_ok_por_cada_proveedor(entorno):
    servicio = entorno.servicio_perfil()

    await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    filas = {f["proveedor"]: f for f in entorno.filas()}
    assert set(filas) == {"open_finance", "open_data"}
    for fuente in Fuente:
        f = filas[fuente.value]
        assert f["resultado"] == "OK" and not f["hit_cache"]
        assert f["cliente_id"] == "c1"
        assert json.loads(f["categorias"]) == list(CATEGORIAS_POR_FUENTE[fuente])
        assert f["consentimiento_id"] == "stub-c1"
        assert f["latencia_ms"] >= 0
        assert f["consultado_en"] is not None


async def test_segunda_consulta_sale_de_cache_y_tambien_deja_huella(entorno):
    servicio = entorno.servicio_perfil()

    await servicio.obtener_perfil("c1")
    await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    filas = entorno.filas()
    assert len(filas) == 4
    assert sorted((f["resultado"], bool(f["hit_cache"])) for f in filas) == [
        ("OK", False),
        ("OK", False),
        ("OK", True),
        ("OK", True),
    ]


async def test_un_proveedor_que_no_responde_a_tiempo_se_registra_como_timeout(entorno):
    servicio = entorno.servicio_perfil(modo_of=ModoFalla.TIMEOUT)

    await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    resultados = {f["proveedor"]: f["resultado"] for f in entorno.filas()}
    assert resultados == {"open_finance": "TIMEOUT", "open_data": "OK"}


@pytest.mark.parametrize("modo", [ModoFalla.ERROR_5XX, ModoFalla.VACIO])
async def test_un_proveedor_que_falla_se_registra_como_degradado(entorno, modo):
    servicio = entorno.servicio_perfil(modo_od=modo)

    await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    resultados = {f["proveedor"]: f["resultado"] for f in entorno.filas()}
    assert resultados == {"open_finance": "OK", "open_data": "DEGRADADO"}


async def test_con_el_circuito_abierto_se_registra_degradado_sin_llamar_al_proveedor(
    entorno,
):
    servicio = entorno.servicio_perfil(modo_of=ModoFalla.ERROR_5XX, umbral_fallos=1)
    await servicio.obtener_perfil("c1")  # abre el circuito de open_finance

    await servicio.obtener_perfil("c2")
    await entorno.escritor.vaciar()

    de_c2 = [f for f in entorno.filas() if f["cliente_id"] == "c2"]
    assert {f["proveedor"]: f["resultado"] for f in de_c2} == {
        "open_finance": "DEGRADADO",
        "open_data": "OK",
    }


class CacheSoloRespaldo(CacheMemoria):
    """Nada vigente: solo queda la copia de respaldo (lo que ocurre tras expirar el TTL)."""

    async def obtener(self, cliente_id, fuente):
        return None


async def test_degradado_servido_desde_respaldo_conserva_la_causa(entorno):
    servicio = entorno.servicio_perfil()
    servicio._cache = CacheSoloRespaldo()
    await servicio.obtener_perfil("c1")  # llena el respaldo
    servicio._proveedores[Fuente.OPEN_FINANCE].config = ConfigSimulador(
        modo=ModoFalla.TIMEOUT, **RAPIDO
    )
    servicio._proveedores[Fuente.OPEN_DATA].config = ConfigSimulador(
        modo=ModoFalla.ERROR_5XX, **RAPIDO
    )

    perfil = await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    assert perfil.degradado and all(f.hit_cache for f in perfil.fuentes)
    segunda = entorno.filas()[2:]
    assert {f["proveedor"]: f["resultado"] for f in segunda} == {
        "open_finance": "TIMEOUT",
        "open_data": "DEGRADADO",
    }
    assert not any(
        f["hit_cache"] for f in segunda
    )  # el respaldo no cuenta como acierto vigente


async def test_sin_consentimiento_se_registra_la_omision_por_cada_proveedor(entorno):
    servicio = entorno.servicio_perfil(
        consentimiento=ConsentimientoStub(EstadoConsentimiento.NO_OTORGADO)
    )

    await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    filas = entorno.filas()
    assert {f["proveedor"] for f in filas} == {"open_finance", "open_data"}
    assert {f["resultado"] for f in filas} == {"SIN_CONSENTIMIENTO"}
    assert {f["latencia_ms"] for f in filas} == {0.0}
    assert {f["consentimiento_id"] for f in filas} == {None}


async def test_un_consentimiento_revocado_registra_su_id(entorno):
    class Revocado:
        async def verificar(self, _cliente_id):
            return VerificacionConsentimiento(
                EstadoConsentimiento.REVOCADO, "consent-77"
            )

    servicio = entorno.servicio_perfil(consentimiento=Revocado())

    await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    assert {(f["resultado"], f["consentimiento_id"]) for f in entorno.filas()} == {
        ("SIN_CONSENTIMIENTO", "consent-77")
    }


# --- criterio 7: cobertura completa -------------------------------------------


async def test_el_100_por_ciento_de_las_consultas_deja_huella(entorno):
    escenarios = [
        entorno.servicio_perfil(),
        entorno.servicio_perfil(modo_of=ModoFalla.TIMEOUT),
        entorno.servicio_perfil(modo_od=ModoFalla.ERROR_5XX),
        entorno.servicio_perfil(modo_od=ModoFalla.VACIO),
        entorno.servicio_perfil(modo_of=ModoFalla.ERROR_5XX, umbral_fallos=1),
        entorno.servicio_perfil(
            consentimiento=ConsentimientoStub(EstadoConsentimiento.REVOCADO)
        ),
        entorno.servicio_perfil(
            consentimiento=ConsentimientoStub(EstadoConsentimiento.NO_OTORGADO)
        ),
    ]
    consultas = 0
    for servicio in escenarios:
        for cliente in ("a", "b", "a"):  # incluye aciertos de caché
            await servicio.obtener_perfil(cliente)
            consultas += 1
    await entorno.escritor.vaciar()

    assert len(entorno.filas()) == consultas * len(Fuente)
    assert entorno.escritor.perdidos == entorno.escritor.descartados == 0
    assert {f["resultado"] for f in entorno.filas()} == {
        r.value for r in ResultadoConsulta
    }


# --- criterio 2: sin datos sensibles ------------------------------------------


async def test_la_huella_y_los_logs_no_contienen_payloads_ni_datos_personales(
    entorno, caplog
):
    caplog.set_level(logging.DEBUG)
    servicio = entorno.servicio_perfil()

    await servicio.obtener_perfil("c1")
    await servicio.obtener_perfil("c1")
    await entorno.escritor.vaciar()

    volcado = json.dumps(entorno.filas(), default=str) + caplog.text
    for sensible in PAYLOAD_SENSIBLE:
        assert sensible not in volcado
    assert set(entorno.filas()[0]) == {
        "id",
        "cliente_id",
        "proveedor",
        "categorias",
        "consultado_en",
        "consentimiento_id",
        "resultado",
        "latencia_ms",
        "hit_cache",
        "cotizacion_id",
        "correlacion_id",
    }


# --- criterio 5: correlación y enlace con la cotización -------------------------


async def test_la_huella_lleva_la_correlacion_y_la_cotizacion_en_curso(entorno):
    servicio = entorno.servicio_perfil()
    t1 = correlacion_id.set("corr-1")
    t2 = cotizacion_en_curso.set("cot-1")
    try:
        await servicio.obtener_perfil("c1")
    finally:
        cotizacion_en_curso.reset(t2)
        correlacion_id.reset(t1)
    await entorno.escritor.vaciar()

    assert {(f["correlacion_id"], f["cotizacion_id"]) for f in entorno.filas()} == {
        ("corr-1", "cot-1")
    }


async def test_consultar_el_perfil_sin_cotizacion_registra_sin_cotizacion_id(entorno):
    await entorno.servicio_perfil().obtener_perfil("c1")
    await entorno.escritor.vaciar()

    assert {f["cotizacion_id"] for f in entorno.filas()} == {None}


@pytest.mark.parametrize(
    "entrante",
    ["abc-123", "9f1c2d3e-aaaa-bbbb-cccc-0123456789ab", "a.b_c:d-e"],
)
def test_una_correlacion_valida_se_conserva(entrante):
    assert normalizar(entrante) == entrante


@pytest.mark.parametrize(
    "entrante", [None, "", "con espacio", "x" * 65, "linea\nnueva", "<script>", "a/b"]
)
def test_una_correlacion_ausente_o_peligrosa_se_reemplaza(entrante):
    resultado = normalizar(entrante)

    assert resultado != entrante and len(resultado) == 36


async def test_de_la_solicitud_http_a_la_huella_por_correlacion_y_cotizacion(
    entorno, reglas
):
    app = entorno.app(entorno.servicio_perfil(), reglas)
    async with _cliente(app) as http:
        r = await http.post(
            "/v1/cotizaciones", json=VIDA, headers={CABECERA: "corr-http-1"}
        )
        sin_cabecera = await http.post("/v1/cotizaciones", json=VIDA)

    assert r.status_code == 201
    assert r.headers[CABECERA] == "corr-http-1"
    assert (
        sin_cabecera.headers[CABECERA]
        and sin_cabecera.headers[CABECERA] != "corr-http-1"
    )
    registros = await entorno.registros()
    de_la_primera = [x for x in registros if x.cotizacion_id == r.json()["id"]]
    assert len(de_la_primera) == 2
    assert {x.correlacion_id for x in de_la_primera} == {"corr-http-1"}
    assert {
        x.correlacion_id
        for x in registros
        if x.cotizacion_id == sin_cabecera.json()["id"]
    } == {sin_cabecera.headers[CABECERA]}


async def test_el_contexto_no_se_filtra_entre_solicitudes(entorno, reglas):
    app = entorno.app(entorno.servicio_perfil(), reglas)
    async with _cliente(app) as http:
        await http.post("/v1/cotizaciones", json=VIDA, headers={CABECERA: "corr-a"})

    assert correlacion_id.get() is None and cotizacion_en_curso.get() is None


# --- criterio 3: consulta por cotización ---------------------------------------


async def test_el_auditor_lista_las_consultas_de_una_cotizacion_en_orden(
    entorno, reglas
):
    app = entorno.app(entorno.servicio_perfil(modo_od=ModoFalla.ERROR_5XX), reglas)
    async with _cliente(app) as http:
        c = (await http.post("/v1/cotizaciones", json=DISP)).json()
        otra = (await http.post("/v1/cotizaciones", json=VIDA)).json()
        await entorno.escritor.vaciar()

        r = await http.get(f"/v1/cotizaciones/{c['id']}/consultas-externas", headers=H)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["cotizacion_id"] == c["id"]
    consultas = cuerpo["consultas"]
    assert {x["proveedor"]: x["resultado"] for x in consultas} == {
        "open_finance": "OK",
        "open_data": "DEGRADADO",
    }
    instantes = [x["consultado_en"] for x in consultas]
    assert instantes == sorted(instantes)
    assert {x["cotizacion_id"] for x in consultas} == {c["id"]}
    assert otra["id"] != c["id"]  # las de la otra cotización no se mezclan
    assert all(x["consentimiento_id"] == "stub-cli-1" for x in consultas)


async def test_el_acceso_a_las_consultas_exige_la_credencial_de_auditor(
    entorno, reglas
):
    app = entorno.app(entorno.servicio_perfil(), reglas)
    async with _cliente(app) as http:
        c = (await http.post("/v1/cotizaciones", json=VIDA)).json()
        ruta = f"/v1/cotizaciones/{c['id']}/consultas-externas"

        sin = await http.get(ruta)
        mala = await http.get(ruta, headers={"X-Service-Key": "otra"})
        no_existe = await http.get(
            "/v1/cotizaciones/no-existe/consultas-externas", headers=H
        )

    assert (sin.status_code, mala.status_code) == (401, 403)
    assert no_existe.status_code == 404
    assert no_existe.json()["error"]["code"] == "COTIZACION_NO_ENCONTRADA"


async def test_una_cotizacion_sin_consultas_devuelve_lista_vacia(entorno, reglas):
    app = entorno.app(entorno.servicio_perfil(), reglas)
    async with _cliente(app) as http:
        c = (await http.post("/v1/cotizaciones", json=VIDA)).json()
        await entorno.escritor.vaciar()
        with entorno.engine.begin() as conexion:
            conexion.exec_driver_sql("DELETE FROM trazabilidad_consultas")

        r = await http.get(f"/v1/cotizaciones/{c['id']}/consultas-externas", headers=H)

    assert r.status_code == 200 and r.json()["consultas"] == []


# --- criterio 4: inmutabilidad -------------------------------------------------


def test_el_repositorio_de_trazabilidad_solo_inserta_y_lee():
    metodos = {
        n
        for n, _ in inspeccion.getmembers(
            RepositorioTrazabilidad, inspeccion.isfunction
        )
        if not n.startswith("_")
    }

    assert metodos == {"insertar_lote", "listar_por_cotizacion"}


# --- criterio 6: no bloquea el camino crítico ------------------------------------


class AlmacenQueFalla:
    def __init__(self, fallos: int | None = None):
        self.fallos = fallos  # None = siempre falla
        self.intentos = 0
        self.guardados: list[RegistroTrazabilidad] = []

    def insertar_lote(self, registros):
        self.intentos += 1
        if self.fallos is None or self.intentos <= self.fallos:
            raise RuntimeError("base caída")
        self.guardados.extend(registros)


def _registro(i: int = 0, **kw) -> RegistroTrazabilidad:
    return RegistroTrazabilidad(
        id=f"r{i}",
        cliente_id="c1",
        proveedor="open_finance",
        categorias=("ingresos",),
        consultado_en=T0,
        resultado=ResultadoConsulta.OK,
        latencia_ms=1.0,
        hit_cache=False,
        **kw,
    )


async def test_si_la_escritura_falla_la_cotizacion_se_emite_igual(entorno, reglas):
    entorno.escritor = EscritorTrazabilidad(
        AlmacenQueFalla(), reintentos=2, dormir=_sin_espera
    )
    app = entorno.app(entorno.servicio_perfil(), reglas)
    entorno.escritor.iniciar()

    async with _cliente(app) as http:
        r = await http.post("/v1/cotizaciones", json=VIDA)
    await entorno.escritor.vaciar()

    assert r.status_code == 201
    assert entorno.escritor.perdidos == 2  # las dos fuentes, tras reintentar


async def test_se_reintenta_con_espera_creciente_hasta_lograr_escribir():
    almacen = AlmacenQueFalla(fallos=2)
    esperas: list[float] = []

    async def registrar_espera(s):
        esperas.append(s)

    escritor = EscritorTrazabilidad(
        almacen, reintentos=3, espera_s=0.1, dormir=registrar_espera
    )
    escritor.encolar(_registro())
    await escritor.vaciar()

    assert almacen.intentos == 3 and len(almacen.guardados) == 1
    assert esperas == [0.1, 0.2]
    assert (escritor.escritos, escritor.perdidos) == (1, 0)


async def test_agotados_los_reintentos_se_alerta_y_se_cuenta_el_registro_perdido(
    caplog,
):
    escritor = EscritorTrazabilidad(AlmacenQueFalla(), reintentos=2, dormir=_sin_espera)
    caplog.set_level(logging.ERROR)

    escritor.encolar(_registro(7))
    await escritor.vaciar()

    assert escritor.perdidos == 1
    assert "ALERTA" in caplog.text and "r7" in caplog.text


async def test_un_registro_invalido_no_hace_perder_al_resto_del_lote():
    class RechazaUno:
        def __init__(self):
            self.guardados = []

        def insertar_lote(self, registros):
            if any(r.id == "malo" for r in registros):
                raise ValueError("no cabe")
            self.guardados.extend(registros)

    almacen = RechazaUno()
    escritor = EscritorTrazabilidad(almacen, reintentos=1, dormir=_sin_espera)
    for i, nombre in enumerate(["a", "malo", "b"]):
        escritor.encolar(replace(_registro(i), id=nombre))
    await escritor.vaciar()

    assert sorted(r.id for r in almacen.guardados) == ["a", "b"]
    assert (escritor.escritos, escritor.perdidos) == (2, 1)


async def test_con_la_cola_llena_se_descarta_con_alerta_sin_lanzar(caplog):
    escritor = EscritorTrazabilidad(AlmacenQueFalla(fallos=0), capacidad=2)
    caplog.set_level(logging.ERROR)

    for i in range(5):
        escritor.encolar(_registro(i))  # no debe lanzar ni bloquear

    assert escritor.descartados == 3
    assert "cola de trazabilidad llena" in caplog.text


async def test_encolar_no_espera_a_la_base():
    class Lento:
        def insertar_lote(self, _r):
            import time

            time.sleep(0.3)

    escritor = EscritorTrazabilidad(Lento())
    escritor.iniciar()
    inicio = asyncio.get_running_loop().time()

    for i in range(20):
        escritor.encolar(_registro(i))

    assert asyncio.get_running_loop().time() - inicio < 0.05
    await escritor.detener()


async def test_al_detener_se_escribe_lo_pendiente():
    almacen = AlmacenQueFalla(fallos=0)
    escritor = EscritorTrazabilidad(almacen)
    escritor.iniciar()
    for i in range(7):
        escritor.encolar(_registro(i))

    await escritor.detener()

    assert len(almacen.guardados) == 7


async def test_un_destino_que_lanza_no_rompe_la_consulta_al_proveedor():
    def roto(_registro):
        raise RuntimeError("cola rota")

    servicio = ServicioPerfil(
        {Fuente.OPEN_FINANCE: SimuladorOpenFinance(ConfigSimulador(**RAPIDO))},
        CacheMemoria(),
        ConsentimientoStub(EstadoConsentimiento.OTORGADO),
        trazar=roto,
    )

    perfil = await servicio.obtener_perfil("c1")

    assert perfil.fuentes and not perfil.degradado


# --- consentimiento_id desde Identidad (HU11) ----------------------------------


def _http(respuesta: httpx.Response) -> ConsentimientoHttp:
    return ConsentimientoHttp(
        "http://identidad",
        "k",
        cliente=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: respuesta),
            base_url="http://identidad",
        ),
    )


async def test_se_lee_el_id_del_consentimiento_que_informa_identidad():
    v = await _http(
        httpx.Response(200, json={"estado": "OTORGADO", "id": "abc-123"})
    ).verificar("c1")

    assert (v.estado, v.consentimiento_id) == (EstadoConsentimiento.OTORGADO, "abc-123")


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"estado": "OTORGADO"},
        {"estado": "OTORGADO", "id": 5},
        {"estado": "OTORGADO", "id": ""},
    ],
)
async def test_si_identidad_no_informa_un_id_valido_queda_sin_id(cuerpo):
    v = await _http(httpx.Response(200, json=cuerpo)).verificar("c1")

    assert (v.estado, v.consentimiento_id) == (EstadoConsentimiento.OTORGADO, None)


async def test_un_cuerpo_que_no_es_objeto_se_trata_como_no_otorgado():
    v = await _http(httpx.Response(200, json=["x"])).verificar("c1")

    assert (v.estado, v.consentimiento_id) == (EstadoConsentimiento.NO_OTORGADO, None)


async def test_el_stub_solo_da_id_cuando_hay_consentimiento():
    otorgado = await ConsentimientoStub(EstadoConsentimiento.OTORGADO).verificar("c1")
    revocado = await ConsentimientoStub(EstadoConsentimiento.REVOCADO).verificar("c1")

    assert (
        otorgado.consentimiento_id == "stub-c1" and revocado.consentimiento_id is None
    )


def test_encolar_cuesta_microsegundos_y_no_toca_la_base():
    import time

    almacen = AlmacenQueFalla(fallos=0)
    escritor = EscritorTrazabilidad(almacen, capacidad=5000)
    registro = _registro()

    inicio = time.perf_counter()
    for _ in range(2000):
        escritor.encolar(registro)
    por_llamada_ms = (time.perf_counter() - inicio) * 1000 / 2000

    assert almacen.intentos == 0  # nada se escribe dentro de la solicitud
    assert por_llamada_ms < 0.1, f"encolar tarda {por_llamada_ms:.4f} ms"
