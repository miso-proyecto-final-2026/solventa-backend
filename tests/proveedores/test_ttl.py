import random

from app.proveedores.ttl import ttl_con_jitter


def test_ttl_queda_dentro_de_la_dispersion_10_15_por_ciento():
    rng = random.Random(7)
    for _ in range(500):
        ttl = ttl_con_jitter(300, rng=rng)
        assert 255 <= ttl <= 345  # 300 ± 15 %
        assert not (270 < ttl < 330)  # nunca dentro de ±10 %


def test_ttl_es_determinista_con_semilla():
    assert ttl_con_jitter(300, rng=random.Random(1)) == ttl_con_jitter(300, rng=random.Random(1))


def test_ttl_minimo_es_un_segundo():
    assert ttl_con_jitter(0, rng=random.Random(1)) == 1
