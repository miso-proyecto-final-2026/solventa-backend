"""Siembra 5 asesores en staging. Lo ejecuta el Job `solventa-identidad-seed` de la infra. Es idempotente.

Solo crea usuarios internos con rol ASESOR (sin clientes ni operador).

Variables de entorno:
    ENTORNO                   obligatoria; debe valer "staging" (salvaguarda contra otros entornos).
    ASESORES_PASSWORD         obligatoria; clave compartida de los asesores. Viene de Secrets Manager
                              (solventa/staging/asesores-password) por un ExternalSecret; no tiene valor por defecto.
    ASESORES_ACTUALIZAR_CLAVE "1" para reescribir la clave de los asesores que ya existen (cambio de clave del demo).
                              Sin ella, los existentes no se tocan.
    ASESORES_DOMINIO          dominio de los correos (por defecto solventa.test).

Uso manual (la imagen incluye scripts/; DATABASE_URL ya lo define el Deployment):
    kubectl -n staging exec -i deploy/solventa-identidad -- \
        env ENTORNO=staging ASESORES_PASSWORD='<clave>' python scripts/sembrar_asesores_staging.py
"""

import os
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

CREADO = "creado"
ACTUALIZADO = "clave actualizada"
SIN_CAMBIOS = "ya existía (sin cambios)"

# (usuario del correo, nombre)
ASESORES_STAGING = [
    ("asesor1", "Asesor Staging 1"),
    ("asesor2", "Asesor Staging 2"),
    ("asesor3", "Asesor Staging 3"),
    ("asesor4", "Asesor Staging 4"),
    ("asesor5", "Asesor Staging 5"),
]


def sembrar(db, password: str, dominio: str = DOMINIO, actualizar_clave: bool = False) -> list[tuple[str, str]]:
    """Crea los asesores que falten. Con `actualizar_clave`, reescribe la clave de los que ya existen.

    Devuelve (email, resultado) con resultado CREADO, ACTUALIZADO o SIN_CAMBIOS.
    """
    resultado: list[tuple[str, str]] = []
    for usuario, nombre in ASESORES_STAGING:
        email = f"{usuario}@{dominio}".lower()
        existente = db.query(UsuarioInterno).filter_by(email=email).first()
        if existente is None:
            db.add(UsuarioInterno(email=email, nombre=nombre, password_hash=hash_password(password), rol=ROL_ASESOR))
            resultado.append((email, CREADO))
        elif actualizar_clave:
            existente.password_hash = hash_password(password)
            resultado.append((email, ACTUALIZADO))
        else:
            resultado.append((email, SIN_CAMBIOS))
    db.commit()
    return resultado


def main() -> None:
    entorno = os.environ.get("ENTORNO", "")
    if entorno != ENTORNO_PERMITIDO:
        sys.exit(f'ENTORNO="{entorno}": este script solo corre con ENTORNO={ENTORNO_PERMITIDO}.')
    password = os.environ.get("ASESORES_PASSWORD", "")
    if not password:
        sys.exit("Defina ASESORES_PASSWORD (la clave compartida de los asesores).")
    actualizar = os.environ.get("ASESORES_ACTUALIZAR_CLAVE", "") == "1"
    with SessionLocal() as db:
        resultado = sembrar(db, password, DOMINIO, actualizar)
    for email, estado in resultado:
        print(f"{email}: {estado}")


if __name__ == "__main__":
    main()
