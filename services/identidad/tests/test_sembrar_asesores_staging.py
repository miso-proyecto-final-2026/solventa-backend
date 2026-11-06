"""El sembrado manual de asesores en staging es idempotente y respeta la salvaguarda de entorno."""

import importlib.util
from pathlib import Path

import pytest

from app.models import ROL_ASESOR, UsuarioInterno

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "sembrar_asesores_staging.py"
_spec = importlib.util.spec_from_file_location("sembrar_asesores_staging", _RUTA)
sembrar_asesores = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sembrar_asesores)


def test_crea_cinco_asesores_con_clave_propia(sesion_bd):
    resultado = sembrar_asesores.sembrar(sesion_bd)
    usuarios = sesion_bd.query(UsuarioInterno).all()
    assert len(usuarios) == 5
    assert {u.rol for u in usuarios} == {ROL_ASESOR}
    claves = [c for _, c in resultado]
    assert all(claves) and len(set(claves)) == 5


def test_segunda_ejecucion_no_duplica_ni_cambia_claves(sesion_bd):
    sembrar_asesores.sembrar(sesion_bd)
    hashes = {u.email: u.password_hash for u in sesion_bd.query(UsuarioInterno).all()}
    resultado = sembrar_asesores.sembrar(sesion_bd)
    assert all(c is None for _, c in resultado)
    assert {u.email: u.password_hash for u in sesion_bd.query(UsuarioInterno).all()} == hashes


def test_password_compartida_se_usa_en_todos(sesion_bd):
    resultado = sembrar_asesores.sembrar(sesion_bd, password="Staging1234")
    assert [c for _, c in resultado] == ["Staging1234"] * 5


def test_se_niega_fuera_de_staging(monkeypatch):
    monkeypatch.setenv("ENTORNO", "produccion")
    with pytest.raises(SystemExit):
        sembrar_asesores.main()
    monkeypatch.delenv("ENTORNO")
    with pytest.raises(SystemExit):
        sembrar_asesores.main()
