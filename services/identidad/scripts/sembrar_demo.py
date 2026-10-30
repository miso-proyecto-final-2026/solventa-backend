"""Siembra el asesor demo, el operador demo (opcional) y cinco clientes demo para probar HU38 y HU19 en local.
Es idempotente.

Uso (desde services/identidad):
    ASESOR_DEMO_EMAIL=asesor@solventa.test ASESOR_DEMO_PASSWORD=<clave> python scripts/sembrar_demo.py
    # opcional, HU19: OPERADOR_DEMO_EMAIL=operador@solventa.test OPERADOR_DEMO_PASSWORD=<clave>

La clave no tiene valor por defecto ni se guarda en el repositorio. Los `cliente_id` son fijos
porque Cotización los usa para asignarles un perfil financiero simulado
(`app/proveedores/perfiles_demo.yaml`).
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.models import ROL_ASESOR, ROL_OPERADOR, Cliente, Consentimiento, UsuarioInterno
from app.security import hash_password

TIPO_CONSENTIMIENTO = "OPEN_FINANCE_OPEN_DATA"

# (id, nombre, apellido, documento, consentimiento)
CLIENTES_DEMO = [
    ("00000000-0000-4000-8000-0000000000a1", "Ana", "Favorable", "1000000001", "OTORGADO"),
    ("00000000-0000-4000-8000-0000000000b2", "Bruno", "Riesgoso", "1000000002", "OTORGADO"),
    ("00000000-0000-4000-8000-0000000000c3", "Carla", "Neutra", "1000000003", "OTORGADO"),
    ("00000000-0000-4000-8000-0000000000d4", "Diego", "Revocado", "1000000004", "REVOCADO"),
    ("00000000-0000-4000-8000-0000000000e5", "Elena", "SinConsentimiento", "1000000005", None),
]


def _usuario_interno(db, email: str, password: str, nombre: str, rol: str) -> None:
    email = email.strip().lower()
    if db.query(UsuarioInterno).filter_by(email=email).first() is None:
        db.add(UsuarioInterno(email=email, nombre=nombre, password_hash=hash_password(password), rol=rol))


def sembrar(db, email: str, password: str, operador: tuple[str, str] | None = None) -> None:
    _usuario_interno(db, email, password, "Asesor Demo", ROL_ASESOR)
    if operador is not None:
        _usuario_interno(db, operador[0], operador[1], "Operador Demo", ROL_OPERADOR)
    ahora = datetime.now(UTC)
    for cid, nombre, apellido, documento, consentimiento in CLIENTES_DEMO:
        if db.get(Cliente, cid) is None:
            db.add(
                Cliente(
                    id=cid,
                    nombre=nombre,
                    apellido=apellido,
                    tipo_documento="CC",
                    numero_documento=documento,
                    email=f"{nombre.lower()}.demo@example.com",
                    celular="3000000000",
                    password_hash=hash_password(password),
                    estado_kyc="VERIFICADO",
                )
            )
            db.flush()
        if consentimiento and db.query(Consentimiento).filter_by(cliente_id=cid, tipo=TIPO_CONSENTIMIENTO).first() is None:
            db.add(
                Consentimiento(
                    cliente_id=cid,
                    tipo=TIPO_CONSENTIMIENTO,
                    estado=consentimiento,
                    version_texto="v1",
                    fecha_otorgamiento=ahora,
                    fecha_revocacion=ahora if consentimiento == "REVOCADO" else None,
                )
            )
    db.commit()


def main() -> None:
    email = os.environ.get("ASESOR_DEMO_EMAIL", "")
    password = os.environ.get("ASESOR_DEMO_PASSWORD", "")
    if not email or not password:
        sys.exit("Defina ASESOR_DEMO_EMAIL y ASESOR_DEMO_PASSWORD.")
    op_email = os.environ.get("OPERADOR_DEMO_EMAIL", "")
    op_password = os.environ.get("OPERADOR_DEMO_PASSWORD", "")
    if bool(op_email) != bool(op_password):
        sys.exit("Defina OPERADOR_DEMO_EMAIL y OPERADOR_DEMO_PASSWORD juntos, o ninguno.")
    operador = (op_email, op_password) if op_email else None
    with SessionLocal() as db:
        sembrar(db, email, password, operador)
    extra = f", operador {op_email}" if operador else ""
    print(f"Sembrados: asesor {email}{extra} y {len(CLIENTES_DEMO)} clientes demo (CC 1000000001..1000000005).")


if __name__ == "__main__":
    main()
