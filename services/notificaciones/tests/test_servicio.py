"""Caso de uso de Notificaciones (HU22): registrar los envíos de `poliza_emitida` y entregarlos con reintentos."""

import logging
from datetime import UTC, datetime

import pytest
from app.notificaciones import repositorio as repo
from app.notificaciones.modelos_db import BandejaPushDB, EnvioDB
from sqlalchemy import select

from tests.dobles import (
    AHORA,
    CORREO_CLIENTE,
    CertificadosFalsos,
    CorreoFalso,
    IdentidadFalsa,
    PushFalso,
    poliza_emitida,
)


async def _recibir(servicio, fabrica, evento):
    """Lo que hace el consumidor de Kafka: el manejador corre dentro de una transacción."""
    with fabrica() as s, s.begin():
        await servicio.manejar_evento(evento, s)


def _envios(fabrica) -> list[EnvioDB]:
    with fabrica() as s:
        return list(s.execute(select(EnvioDB).order_by(EnvioDB.canal)).scalars())


def _envio(fabrica, canal) -> EnvioDB:
    return next(e for e in _envios(fabrica) if e.canal == canal)


def _bandeja(fabrica) -> list[BandejaPushDB]:
    with fabrica() as s:
        return list(s.execute(select(BandejaPushDB)).scalars())


def _registrar_dispositivo(fabrica, idioma, cliente_id="cli-1", dispositivo_id="telefono-123456"):
    with fabrica() as s, s.begin():
        repo.registrar_dispositivo(s, cliente_id, dispositivo_id, idioma, AHORA)


# --- registrar (criterio 5) ------------------------------------------------------------------------------------------

async def test_un_evento_de_poliza_emitida_registra_un_envio_pendiente_por_canal(armar, fabrica):
    servicio = armar()

    await _recibir(servicio, fabrica, poliza_emitida())

    push, correo = _envios(fabrica)[1], _envios(fabrica)[0]
    assert (correo.canal, push.canal) == ("CORREO", "PUSH")
    for envio in (push, correo):
        assert (envio.estado, envio.intentos, envio.tipo, envio.cliente_id) == ("PENDIENTE", 0, "POLIZA_EMITIDA", "cli-1")
        assert envio.datos["numero_poliza"] == "36GBQKV2CWMM3EBB8Q6Q" and envio.datos["poliza_id"] == "pol-1"


async def test_reentregar_el_evento_no_crea_ni_envia_nada_dos_veces(armar, fabrica):
    correo, push = CorreoFalso(), PushFalso()
    servicio = armar(correo=correo, push=push)
    evento = poliza_emitida()

    await _recibir(servicio, fabrica, evento)
    await servicio.procesar_pendientes()
    await _recibir(servicio, fabrica, evento)  # misma entrega otra vez: mismo event_id
    await servicio.procesar_pendientes()

    assert len(_envios(fabrica)) == 2
    assert len(correo.enviados) == 1 and len(push.entregados) == 1


async def test_dos_eventos_distintos_son_dos_notificaciones(armar, fabrica):
    correo = CorreoFalso()
    servicio = armar(correo=correo)

    await _recibir(servicio, fabrica, poliza_emitida(poliza_id="pol-1"))
    await _recibir(servicio, fabrica, poliza_emitida(poliza_id="pol-2"))
    await servicio.procesar_pendientes()

    assert len(_envios(fabrica)) == 4 and len(correo.enviados) == 2


async def test_otros_eventos_del_topic_se_ignoran(armar, fabrica):
    servicio = armar()
    evento = poliza_emitida().model_copy(update={"event_type": "poliza_cancelada"})

    await _recibir(servicio, fabrica, evento)

    assert _envios(fabrica) == []


@pytest.mark.parametrize("falta", ["poliza_id", "suscripcion_id", "cliente_id", "numero_poliza", "producto", "vigencia_desde"])
async def test_un_evento_mal_formado_se_rechaza_para_que_vaya_a_la_cola_de_fallidos(armar, fabrica, falta):
    servicio = armar()

    with pytest.raises(ValueError, match=falta):
        await _recibir(servicio, fabrica, poliza_emitida(**{falta: None}))

    assert _envios(fabrica) == []


# --- idioma (criterio 9) ---------------------------------------------------------------------------------------------

async def test_sin_dispositivo_los_textos_salen_en_es_co(armar, fabrica):
    servicio = armar()

    await _recibir(servicio, fabrica, poliza_emitida())

    assert {e.idioma for e in _envios(fabrica)} == {"es-CO"}


async def test_el_idioma_del_dispositivo_del_cliente_manda(armar, fabrica):
    correo = CorreoFalso()
    servicio = armar(correo=correo)
    _registrar_dispositivo(fabrica, "en-US")

    await _recibir(servicio, fabrica, poliza_emitida())
    await servicio.procesar_pendientes()

    (mensaje,) = correo.enviados
    assert mensaje.asunto == "Your policy is active" and "Policy number" in mensaje.texto
    (notificacion,) = _bandeja(fabrica)
    assert (notificacion.titulo, notificacion.idioma) == ("Subscription completed", "en-US")


async def test_el_idioma_de_otro_cliente_no_se_usa(armar, fabrica):
    servicio = armar()
    _registrar_dispositivo(fabrica, "en-US", cliente_id="cli-2")

    await _recibir(servicio, fabrica, poliza_emitida(cliente_id="cli-1"))

    assert {e.idioma for e in _envios(fabrica)} == {"es-CO"}


# --- entregar el push (criterios 2 y 8) ------------------------------------------------------------------------------

async def test_el_push_llega_a_la_bandeja_con_prioridad_alta_y_enlaza_a_la_suscripcion(armar, fabrica):
    servicio = armar()
    await _recibir(servicio, fabrica, poliza_emitida())

    await servicio.procesar_pendientes()

    (n,) = _bandeja(fabrica)
    assert (n.titulo, n.prioridad, n.cliente_id, n.suscripcion_id) == ("Suscripción completada", "ALTA", "cli-1", "susc-1")
    assert n.recibida_en is None
    assert _envio(fabrica, "PUSH").estado == "ENVIADO" and _envio(fabrica, "PUSH").referencia_proveedor == f"bandeja:{n.envio_id}"


async def test_la_notificacion_no_lleva_el_numero_de_poliza_ni_datos_financieros(armar, fabrica):
    servicio = armar()
    await _recibir(servicio, fabrica, poliza_emitida())
    await servicio.procesar_pendientes()

    (n,) = _bandeja(fabrica)

    visible = f"{n.titulo} {n.texto} {n.titulo_publico} {n.texto_publico}"
    assert "36GBQKV2CWMM3EBB8Q6Q" not in visible and "175" not in visible and "$" not in visible and "COP" not in visible
    assert "pol-1" not in visible and CORREO_CLIENTE not in visible
    assert n.texto_publico == "Tienes una notificación nueva."  # lo que se ve con la pantalla de bloqueo


# --- entregar el correo (criterio 3) ---------------------------------------------------------------------------------

async def test_el_correo_lleva_los_detalles_de_la_poliza_y_el_enlace_con_su_vencimiento(armar, fabrica):
    correo, certificados = CorreoFalso(), CertificadosFalsos()
    servicio = armar(correo=correo, certificados=certificados)
    await _recibir(servicio, fabrica, poliza_emitida())

    await servicio.procesar_pendientes()

    (c,) = correo.enviados
    assert c.destinatario == CORREO_CLIENTE and c.asunto == "Tu póliza está activa"
    assert "Hola, Ana:" in c.texto and "36GBQKV2CWMM3EBB8Q6Q" in c.texto and "Microseguro de vida" in c.texto
    assert "Desde el 30 de octubre de 2026 hasta el 30 de octubre de 2027" in c.texto
    assert "https://borde.example/v1/certificados/token-de-prueba" in c.texto and "31 de octubre de 2026" in c.texto
    assert 'href="https://borde.example/v1/certificados/token-de-prueba"' in c.html
    assert certificados.llamadas == [("pol-1", "es-CO")]
    assert _envio(fabrica, "CORREO").estado == "ENVIADO" and _envio(fabrica, "CORREO").referencia_proveedor == "msg-1"


@pytest.mark.parametrize(("modo", "url"), [("sin_enlaces", "https://borde.example"), ("ok", None)])
async def test_sin_enlace_disponible_el_correo_sale_igual_y_manda_a_la_aplicacion(armar, fabrica, modo, url):
    correo = CorreoFalso()
    servicio = armar(correo=correo, certificados=CertificadosFalsos(modo), url_publica=url)
    await _recibir(servicio, fabrica, poliza_emitida())

    await servicio.procesar_pendientes()

    (c,) = correo.enviados
    assert "desde la aplicación" in c.texto and "http" not in c.texto and "36GBQKV2CWMM3EBB8Q6Q" in c.texto


async def test_si_suscripcion_no_responde_el_correo_se_reintenta_y_no_sale_sin_enlace(armar, fabrica, reloj):
    correo, certificados = CorreoFalso(), CertificadosFalsos("caido")
    servicio = armar(correo=correo, certificados=certificados)
    await _recibir(servicio, fabrica, poliza_emitida())

    await servicio.procesar_pendientes()
    assert correo.enviados == [] and _envio(fabrica, "CORREO").ultimo_error == "DependenciaNoDisponible"

    certificados.modo = "ok"
    reloj.avanzar(6)
    await servicio.procesar_pendientes()
    assert len(correo.enviados) == 1 and "token-de-prueba" in correo.enviados[0].texto


# --- fallos que no revierten nada (criterio 6) -----------------------------------------------------------------------

async def test_si_el_correo_falla_el_push_igual_se_entrega(armar, fabrica):
    correo = CorreoFalso(fallos=99)
    servicio = armar(correo=correo)
    await _recibir(servicio, fabrica, poliza_emitida())

    atendidos = await servicio.procesar_pendientes()

    assert atendidos == 2
    assert _envio(fabrica, "PUSH").estado == "ENVIADO" and len(_bandeja(fabrica)) == 1
    assert (_envio(fabrica, "CORREO").estado, _envio(fabrica, "CORREO").intentos) == ("PENDIENTE", 1)


async def test_un_fallo_se_reintenta_con_espera_creciente_hasta_enviarse(armar, fabrica, reloj):
    correo = CorreoFalso(fallos=2)
    servicio = armar(correo=correo)
    await _recibir(servicio, fabrica, poliza_emitida())

    await servicio.procesar_pendientes()  # intento 1 falla; el siguiente, en 5 s
    assert await servicio.procesar_pendientes() == 0  # todavía dentro de la espera
    reloj.avanzar(4)
    assert await servicio.procesar_pendientes() == 0
    reloj.avanzar(1.5)
    await servicio.procesar_pendientes()  # intento 2 falla; el siguiente, en 10 s
    reloj.avanzar(9)
    assert await servicio.procesar_pendientes() == 0
    reloj.avanzar(1.5)
    await servicio.procesar_pendientes()  # intento 3 envía

    e = _envio(fabrica, "CORREO")
    assert (e.estado, e.intentos, e.ultimo_error) == ("ENVIADO", 3, None)
    assert len(correo.enviados) == 1


async def test_agotados_los_intentos_el_envio_queda_fallido_y_visible_al_operador(armar, fabrica, reloj):
    servicio = armar(correo=CorreoFalso(fallos=99), max_intentos=3)
    await _recibir(servicio, fabrica, poliza_emitida())

    for _ in range(3):
        await servicio.procesar_pendientes()
        reloj.avanzar(120)
    assert await servicio.procesar_pendientes() == 0  # ya no se toca

    e = _envio(fabrica, "CORREO")
    assert (e.estado, e.intentos, e.ultimo_error, e.proximo_intento_en) == ("FALLIDO", 3, "MessageRejected", None)
    with fabrica() as s:
        assert [x.canal for x in repo.envios_fallidos(s, 10)] == ["CORREO"]
    assert _envio(fabrica, "PUSH").estado == "ENVIADO"  # el otro canal no se enteró


async def test_un_proceso_que_cae_a_mitad_deja_el_envio_reintentable_y_al_agotarse_fallido(armar, fabrica, reloj):
    servicio = armar(max_intentos=2)
    await _recibir(servicio, fabrica, poliza_emitida())
    servicio._reclamar(10)  # toma ambos y «cae» antes de entregar
    reloj.avanzar(6)
    servicio._reclamar(10)  # segundo intento, también sin terminar
    reloj.avanzar(11)

    servicio._reclamar(10)  # ya no quedan intentos

    assert {e.estado for e in _envios(fabrica)} == {"FALLIDO"}
    assert {e.ultimo_error for e in _envios(fabrica)} == {"SinRespuesta"}


async def test_un_cliente_sin_contacto_en_identidad_agota_los_intentos_sin_inventar_un_correo(armar, fabrica, reloj):
    correo = CorreoFalso()
    servicio = armar(correo=correo, identidad=IdentidadFalsa(contactos={}), max_intentos=2)
    await _recibir(servicio, fabrica, poliza_emitida())

    for _ in range(2):
        await servicio.procesar_pendientes()
        reloj.avanzar(60)

    assert correo.enviados == []
    e = _envio(fabrica, "CORREO")
    assert (e.estado, e.ultimo_error) == ("FALLIDO", "ClienteSinContacto")


async def test_un_envio_ya_enviado_no_se_vuelve_a_tomar(armar, fabrica, reloj):
    correo = CorreoFalso()
    servicio = armar(correo=correo)
    await _recibir(servicio, fabrica, poliza_emitida())
    await servicio.procesar_pendientes()

    reloj.avanzar(3600)
    assert await servicio.procesar_pendientes() == 0
    assert len(correo.enviados) == 1


# --- privacidad ------------------------------------------------------------------------------------------------------

async def test_ni_la_base_ni_los_logs_guardan_el_correo_del_cliente(armar, fabrica, caplog):
    servicio = armar(correo=CorreoFalso(fallos=1))
    with caplog.at_level(logging.DEBUG):
        await _recibir(servicio, fabrica, poliza_emitida())
        await servicio.procesar_pendientes()

    assert CORREO_CLIENTE not in caplog.text
    with fabrica() as s:
        for e in s.execute(select(EnvioDB)).scalars():
            assert CORREO_CLIENTE not in repr(e.datos) and CORREO_CLIENTE not in (e.ultimo_error or "")


def test_la_espera_entre_intentos_crece_y_tiene_tope(armar):
    servicio = armar(espera_base_s=5, espera_maxima_s=300)

    assert [servicio.espera(i) for i in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]


async def test_el_vencimiento_del_enlace_se_muestra_en_la_fecha_de_colombia_y_no_en_la_de_utc(armar, fabrica):
    correo = CorreoFalso()
    # 03:00 UTC del 1 de noviembre son las 22:00 del 31 de octubre en Colombia.
    servicio = armar(correo=correo, certificados=CertificadosFalsos(vence=datetime(2026, 11, 1, 3, 0, tzinfo=UTC)))
    await _recibir(servicio, fabrica, poliza_emitida())

    await servicio.procesar_pendientes()

    assert "vence el 31 de octubre de 2026" in correo.enviados[0].texto
