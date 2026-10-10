"""TTL de caché con dispersión aleatoria para evitar expiraciones simultáneas."""

from __future__ import annotations

import random


def ttl_con_jitter(
    base_s: int,
    jitter_min: float = 0.10,
    jitter_max: float = 0.15,
    rng: random.Random | None = None,
) -> int:
    """Devuelve base ± (jitter_min..jitter_max) de dispersión, en segundos enteros."""
    r = rng or random
    fraccion = r.uniform(jitter_min, jitter_max)
    signo = r.choice((-1, 1))
    return max(1, round(base_s * (1 + signo * fraccion)))
