"""El sembrado de asesores en staging es idempotente, usa la clave dada y respeta la salvaguarda de entorno."""

import importlib.util
from pathlib import Path

import pytest

from app.models import ROL_ASESOR, UsuarioInterno
from app.security import verify_password

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "sembrar_asesores_staging.py"
_spec = importlib.util.spec_from_file_location("sembrar_asesores_staging", _RUTA)
sembrar_asesores = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sembrar_asesores)


def _asesores(db):
    return {u.email: u for u in db.query(UsuarioInterno).all()}


def test_crea_cinco_asesores_con_la_clave_dada(sesion_bd):
    resultado = sembrar_asesores.sembrar(sesion_bd, "ClaveDemo2026")
    usuarios = _asesores(sesion_bd)
    assert len(usuarios) == 5
    assert {u.rol for u in usuarios.values()} == {ROL_ASESOR}
    assert [estado for _, estado in resultado] == [sembrar_asesores.CREADO] * 5
    assert all(verify_password("ClaveDemo2026", u.password_hash) for u in usuarios.values())


def test_segunda_ejecucion_no_duplica_ni_cambia_claves(sesion_bd):
    sembrar_asesores.sembrar(sesion_bd, "ClaveDemo2026")
    hashes = {e: u.password_hash for e, u in _asesores(sesion_bd).items()}
    resultado = sembrar_asesores.sembrar(sesion_bd, "OtraClave2026")
    assert [estado for _, estado in resultado] == [sembrar_asesores.SIN_CAMBIOS] * 5
    assert {e: u.password_hash for e, u in _asesores(sesion_bd).items()} == hashes


def test_actualizar_clave_reescribe_la_de_los_asesores_existentes(sesion_bd):
    sembrar_asesores.sembrar(sesion_bd, "ClaveDemo2026")
    resultado = sembrar_asesores.sembrar(sesion_bd, "ClaveNueva2026", actualizar_clave=True)
    usuarios = _asesores(sesion_bd)
    assert len(usuarios) == 5
    assert [estado for _, estado in resultado] == [sembrar_asesores.ACTUALIZADO] * 5
    assert all(verify_password("ClaveNueva2026", u.password_hash) for u in usuarios.values())
    assert not any(verify_password("ClaveDemo2026", u.password_hash) for u in usuarios.values())


def test_actualizar_clave_tambien_crea_los_que_faltan(sesion_bd):
    sembrar_asesores.sembrar(sesion_bd, "ClaveDemo2026")
    sesion_bd.delete(_asesores(sesion_bd)["asesor3@solventa.test"])
    sesion_bd.commit()
    resultado = dict(sembrar_asesores.sembrar(sesion_bd, "ClaveNueva2026", actualizar_clave=True))
    assert resultado["asesor3@solventa.test"] == sembrar_asesores.CREADO
    assert len(_asesores(sesion_bd)) == 5


def test_se_niega_fuera_de_staging(monkeypatch):
    monkeypatch.setenv("ASESORES_PASSWORD", "ClaveDemo2026")
    monkeypatch.setenv("ENTORNO", "produccion")
    with pytest.raises(SystemExit):
        sembrar_asesores.main()
    monkeypatch.delenv("ENTORNO")
    with pytest.raises(SystemExit):
        sembrar_asesores.main()


def test_exige_la_clave_en_staging(monkeypatch):
    monkeypatch.setenv("ENTORNO", "staging")
    monkeypatch.delenv("ASESORES_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as salida:
        sembrar_asesores.main()
    assert "ASESORES_PASSWORD" in str(salida.value)
