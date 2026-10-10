from app.proveedores.circuit_breaker import CircuitBreaker
from app.proveedores.dominio import EstadoCircuito


def test_inicia_cerrado_y_permite_solicitudes():
    cb = CircuitBreaker()
    assert cb.estado is EstadoCircuito.CERRADO
    assert cb.permite_solicitud()


def test_abre_tras_5_fallos_consecutivos():
    cb = CircuitBreaker(umbral_fallos=5)
    for _ in range(4):
        cb.registrar_fallo()
    assert cb.estado is EstadoCircuito.CERRADO
    cb.registrar_fallo()
    assert cb.estado is EstadoCircuito.ABIERTO
    assert not cb.permite_solicitud()


def test_un_exito_reinicia_el_contador_de_fallos():
    cb = CircuitBreaker(umbral_fallos=3)
    cb.registrar_fallo()
    cb.registrar_fallo()
    cb.registrar_exito()
    cb.registrar_fallo()
    cb.registrar_fallo()
    assert cb.estado is EstadoCircuito.CERRADO


def test_ciclo_completo_cerrado_abierto_semiabierto_cerrado():
    cb = CircuitBreaker(umbral_fallos=1)
    cb.registrar_fallo()
    assert cb.estado is EstadoCircuito.ABIERTO
    assert cb.iniciar_sondeo()
    assert cb.estado is EstadoCircuito.SEMIABIERTO
    assert not cb.permite_solicitud()  # el sondeo no consume solicitudes de usuario
    cb.resultado_sondeo(exito=True)
    assert cb.estado is EstadoCircuito.CERRADO
    assert cb.permite_solicitud()


def test_sondeo_fallido_reabre_el_circuito():
    cb = CircuitBreaker(umbral_fallos=1)
    cb.registrar_fallo()
    cb.iniciar_sondeo()
    cb.resultado_sondeo(exito=False)
    assert cb.estado is EstadoCircuito.ABIERTO


def test_iniciar_sondeo_solo_aplica_desde_abierto():
    cb = CircuitBreaker()
    assert not cb.iniciar_sondeo()
    assert cb.estado is EstadoCircuito.CERRADO


def test_no_se_reabre_ni_cierra_por_solicitudes_con_circuito_abierto():
    cb = CircuitBreaker(umbral_fallos=1)
    cb.registrar_fallo()
    cb.registrar_exito()  # una respuesta tardía no debe cerrar el circuito
    assert cb.estado is EstadoCircuito.ABIERTO


def test_registra_instante_de_apertura_con_reloj_inyectado():
    cb = CircuitBreaker(umbral_fallos=1, reloj=lambda: 42.0)
    cb.registrar_fallo()
    assert cb.abierto_desde == 42.0
