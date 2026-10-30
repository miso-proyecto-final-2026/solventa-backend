"""El token de la pasarela no se confunde con un número de tarjeta (HU18, HA05).

La app arma el token como `tok_sim_<uuid>`. La regla amplia de `parece_pan` aceptaba guiones entre cualquier dígito y,
sumando los de segmentos vecinos, rechazaba cerca del 2 % de los UUID: el cobro de esa compra se perdía en la cola de
fallidos y la app se quedaba esperando.
"""

import random
import uuid

import pytest
from app.pagos.dominio import identificador_parece_pan, parece_pan
from app.pagos.errores import SolicitudInvalida
from app.pagos.servicio import ComandoCobro

# UUID reales que la regla amplia rechazaba, hallados al medirla con 200 000 tokens aleatorios.
UUID_QUE_LA_REGLA_AMPLIA_RECHAZABA = [
    "3f85ff64-f746-40b3-9725-48489248206a",
    "6f0d4a12-2505-4074-8125-780a05441aa4",
    "bd2fbaea-31be-4557-9869-139660fffaed",
]
PAN_PRUEBA = "4111111111111111"  # número de prueba público de Visa, no una tarjeta real


@pytest.mark.parametrize("identificador", UUID_QUE_LA_REGLA_AMPLIA_RECHAZABA)
def test_la_regla_amplia_confundia_estos_uuid_con_una_tarjeta_y_la_nueva_no(identificador):
    token = f"tok_sim_{identificador}"

    assert parece_pan(token)  # el problema que se corrige
    assert not identificador_parece_pan(token)


def test_ningun_token_con_uuid_aleatorio_se_toma_por_una_tarjeta():
    azar = random.Random(2026)
    falsos = [
        token
        for token in (f"tok_sim_{uuid.UUID(int=azar.getrandbits(128), version=4)}" for _ in range(100_000))
        if identificador_parece_pan(token)
    ]

    assert falsos == []


@pytest.mark.parametrize("modo", ["aprobar", "rechazar", "perder", "tardar", "caida"])
def test_los_prefijos_de_modo_de_la_pasarela_simulada_tampoco_se_rechazan(modo):
    assert not identificador_parece_pan(f"tok_sim_{modo}_{UUID_QUE_LA_REGLA_AMPLIA_RECHAZABA[0]}")


@pytest.mark.parametrize(
    "texto",
    [
        PAN_PRUEBA,  # corrido
        f"tok_{PAN_PRUEBA}",  # dentro de un token
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "tok_4111-1111-1111-1111",
        "tok_4111-1111-1111-111",  # 15 dígitos, último grupo corto
        "3782 822463 10005",  # American Express
        "3782-822463-10005",
        "tok_" + "9" * 19,
        "4111111111111111111",  # 19 dígitos
    ],
)
def test_las_formas_reales_de_escribir_una_tarjeta_se_siguen_rechazando(texto):
    assert identificador_parece_pan(texto)


@pytest.mark.parametrize("texto", ["tok_sim_abc", "tok_prueba_1234", "tok_" + "1" * 12, "4111-1111", "12 34"])
def test_lo_que_no_tiene_forma_de_tarjeta_pasa(texto):
    assert not identificador_parece_pan(texto)


def test_el_comando_de_cobro_acepta_el_token_que_antes_se_perdia():
    comando = ComandoCobro.desde_evento(
        {
            "suscripcion_id": "susc-1",
            "cliente_id": "cli-1",
            "metodo_pago": f"tok_sim_{UUID_QUE_LA_REGLA_AMPLIA_RECHAZABA[0]}",
            "monto": "175000.00",
            "moneda": "COP",
            "periodicidad_pago": "ANUAL",
            "idempotency_key": "cobro_prima:susc-1",
        }
    )

    assert comando.token.endswith(UUID_QUE_LA_REGLA_AMPLIA_RECHAZABA[0])


def test_el_comando_de_cobro_sigue_rechazando_un_numero_de_tarjeta():
    with pytest.raises(SolicitudInvalida) as error:
        ComandoCobro.desde_evento(
            {
                "suscripcion_id": "susc-1",
                "cliente_id": "cli-1",
                "metodo_pago": "tok_4111-1111-1111-1111",
                "monto": "175000.00",
                "moneda": "COP",
                "idempotency_key": "cobro_prima:susc-1",
            }
        )

    assert "4111" not in str(error.value.detalles)  # el valor no se copia al detalle
