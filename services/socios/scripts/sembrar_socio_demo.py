"""Siembra idempotente del socio de prueba de HU32 (no hay portal de registro de socios: fuera de alcance).

Uso local, desde services/socios:

    SOCIO_DEMO_CLIENT_SECRET=... python scripts/sembrar_socio_demo.py

En staging lo ejecuta el Job `seed_command` de services.yaml tras la migración; el secreto llega desde Secrets
Manager (`seed_secret_env_vars`) y solo lo ve ese Job. El script guarda solo el hash bcrypt y nunca imprime el secreto.

Variables:
- SOCIOS_DATABASE_URL o DATABASE_URL: base de MS Socios (SQLite local si falta y SOLVENTA_MODO_DESARROLLO=1).
- SOCIO_DEMO_CLIENT_SECRET (obligatoria, 24 a 72 caracteres).
- SOCIO_DEMO_CLIENT_ID (por defecto `socio-demo`), SOCIO_DEMO_NOMBRE, SOCIO_DEMO_CUOTA_RPS (por defecto 5),
  SOCIO_DEMO_ENDPOINTS (separados por coma; por defecto `cotizaciones:crear`).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.socios import repositorio as repo
from app.socios.acceso import ENDPOINT_COTIZACIONES, ENDPOINTS_CONOCIDOS
from app.socios.registro import VERSION_V1
from app.socios.secretos import hash_secreto
from sqlalchemy.orm import Session, sessionmaker

from app.configuracion import ErrorConfiguracion, permitir_sustituto_local

LONGITUD_MINIMA_SECRETO = 24


def sembrar(
    fabrica: sessionmaker[Session],
    *,
    client_id: str,
    client_secret: str,
    nombre: str,
    cuota_rps: int,
    endpoints: list[str],
    rondas: int | None = None,
) -> None:
    desconocidos = set(endpoints) - ENDPOINTS_CONOCIDOS
    if desconocidos:
        raise ValueError(f"Endpoints desconocidos: {sorted(desconocidos)}")
    if cuota_rps <= 0:
        raise ValueError("La cuota debe ser positiva.")
    if len(client_secret) < LONGITUD_MINIMA_SECRETO:
        raise ValueError(f"El secreto debe tener al menos {LONGITUD_MINIMA_SECRETO} caracteres.")
    secreto_hash = hash_secreto(client_secret) if rondas is None else hash_secreto(client_secret, rondas)
    with fabrica.begin() as sesion:
        repo.guardar_socio(
            sesion,
            client_id=client_id,
            nombre_socio=nombre,
            client_secret_hash=secreto_hash,
            version=VERSION_V1,
            endpoints=endpoints,
            cuota_rps=cuota_rps,
            ahora=datetime.now(UTC),
        )


def main(env: dict[str, str] | None = None) -> int:
    e = os.environ if env is None else env
    secreto = e.get("SOCIO_DEMO_CLIENT_SECRET")
    if not secreto:
        print("Defina SOCIO_DEMO_CLIENT_SECRET (no se usa un valor por defecto).", file=sys.stderr)
        return 2
    url = e.get("SOCIOS_DATABASE_URL") or e.get("DATABASE_URL")
    if not url:
        try:
            permitir_sustituto_local("DATABASE_URL", "SQLite local", e)
        except ErrorConfiguracion as error:
            print(str(error), file=sys.stderr)
            return 2
        url = "sqlite:///./socios-local.db"

    engine = repo.crear_engine(url)
    if engine.dialect.name == "sqlite":
        repo.crear_tablas_locales(engine)
    client_id = e.get("SOCIO_DEMO_CLIENT_ID", "socio-demo")
    endpoints = [x.strip() for x in e.get("SOCIO_DEMO_ENDPOINTS", ENDPOINT_COTIZACIONES).split(",") if x.strip()]
    try:
        sembrar(
            sessionmaker(bind=engine, expire_on_commit=False),
            client_id=client_id,
            client_secret=secreto,
            nombre=e.get("SOCIO_DEMO_NOMBRE", "Socio de prueba"),
            cuota_rps=int(e.get("SOCIO_DEMO_CUOTA_RPS", "5")),
            endpoints=endpoints,
        )
    except ValueError as error:
        print(f"No se sembró el socio: {error}", file=sys.stderr)
        return 2
    print(f"Socio {client_id} sembrado con contrato {VERSION_V1}: {', '.join(endpoints) or 'sin endpoints'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
