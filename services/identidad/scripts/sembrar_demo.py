"""Siembra el asesor demo y cinco clientes demo para probar HU38 en local. Es idempotente.

Uso (desde services/identidad):
    ASESOR_DEMO_EMAIL=asesor@solventa.test ASESOR_DEMO_PASSWORD=<clave> python scripts/sembrar_demo.py

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
from app.models import Cliente, Consentimiento, UsuarioInterno
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


def sembrar(db, email: str, password: str) -> None:
    email = email.strip().lower()
    if db.query(UsuarioInterno).filter_by(email=email).first() is None:
        db.add(
            UsuarioInterno(
                email=email, nombre="Asesor Demo", password_hash=hash_password(password), rol="ASESOR"
            )
        )
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
    with SessionLocal() as db:
        sembrar(db, email, password)
    print(f"Sembrados: asesor {email} y {len(CLIENTES_DEMO)} clientes demo (CC 1000000001..1000000005).")


if __name__ == "__main__":
    main()
