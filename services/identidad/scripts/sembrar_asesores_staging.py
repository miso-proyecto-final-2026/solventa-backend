"""Siembra 5 asesores en staging. Uso manual; no forma parte del despliegue. Es idempotente.

Solo crea usuarios internos con rol ASESOR (sin clientes ni operador). Si el correo ya existe no se toca,
tampoco su clave.

Uso: la imagen no incluye scripts/, así que se envía por stdin al Pod de identidad (que ya trae DATABASE_URL):
    kubectl -n staging exec -i deploy/solventa-identidad -- env ENTORNO=staging python - < sembrar_asesores_staging.py

Claves:
    - Con ASESORES_PASSWORD todos los asesores nuevos usan esa clave.
    - Sin ella se genera una clave aleatoria por asesor y se imprime UNA sola vez al terminar.
    No hay claves por defecto ni se guardan en el repositorio.

Salvaguarda: se niega a correr si ENTORNO no es "staging".
"""

import os
import secrets
import sys
from pathlib import Path

# Por stdin (`python -`) no existe __file__: el directorio de trabajo (/srv en el Pod) ya contiene `app`.
if "__file__" in globals():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.models import ROL_ASESOR, UsuarioInterno
from app.security import hash_password

ENTORNO_PERMITIDO = "staging"
DOMINIO = os.environ.get("ASESORES_DOMINIO", "solventa.test")

# (usuario del correo, nombre)
ASESORES_STAGING = [
    ("asesor1", "Asesor Staging 1"),
    ("asesor2", "Asesor Staging 2"),
    ("asesor3", "Asesor Staging 3"),
    ("asesor4", "Asesor Staging 4"),
    ("asesor5", "Asesor Staging 5"),
]


def sembrar(db, dominio: str = DOMINIO, password: str | None = None) -> list[tuple[str, str | None]]:
    """Crea los asesores que falten. Devuelve (email, clave) de los creados y (email, None) de los que ya existían."""
    resultado: list[tuple[str, str | None]] = []
    for usuario, nombre in ASESORES_STAGING:
        email = f"{usuario}@{dominio}".lower()
        if db.query(UsuarioInterno).filter_by(email=email).first() is not None:
            resultado.append((email, None))
            continue
        clave = password or secrets.token_urlsafe(12)
        db.add(UsuarioInterno(email=email, nombre=nombre, password_hash=hash_password(clave), rol=ROL_ASESOR))
        resultado.append((email, clave))
    db.commit()
    return resultado


def main() -> None:
    entorno = os.environ.get("ENTORNO", "")
    if entorno != ENTORNO_PERMITIDO:
        sys.exit(f'ENTORNO="{entorno}": este script solo corre con ENTORNO={ENTORNO_PERMITIDO}.')
    password = os.environ.get("ASESORES_PASSWORD") or None
    with SessionLocal() as db:
        resultado = sembrar(db, DOMINIO, password)
    for email, clave in resultado:
        if clave is None:
            print(f"{email}: ya existía (sin cambios)")
        elif password:
            print(f"{email}: creado (clave de ASESORES_PASSWORD)")
        else:
            print(f"{email}: creado, clave {clave}")
    if not password and any(c for _, c in resultado):
        print("Guarde las claves ahora: no se vuelven a mostrar.")


if __name__ == "__main__":
    main()
