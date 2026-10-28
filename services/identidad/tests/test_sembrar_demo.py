"""El sembrado demo (HU38) es idempotente y deja los consentimientos esperados."""

import importlib.util
from pathlib import Path

from app.models import Cliente, Consentimiento, UsuarioInterno

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "sembrar_demo.py"
_spec = importlib.util.spec_from_file_location("sembrar_demo", _RUTA)
sembrar_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sembrar_demo)


def test_sembrar_dos_veces_no_duplica_nada(sesion_bd):
    for _ in range(2):
        sembrar_demo.sembrar(sesion_bd, "asesor@solventa.test", "Asesor1234")
    assert sesion_bd.query(UsuarioInterno).count() == 1
    assert sesion_bd.query(Cliente).count() == 5
    assert sesion_bd.query(Consentimiento).count() == 4  # el quinto cliente no tiene consentimiento


def test_los_clientes_demo_tienen_consentimientos_distintos(sesion_bd):
    sembrar_demo.sembrar(sesion_bd, "asesor@solventa.test", "Asesor1234")
    estados = {
        c.cliente_id: c.estado for c in sesion_bd.query(Consentimiento).all()
    }
    assert estados["00000000-0000-4000-8000-0000000000a1"] == "OTORGADO"
    assert estados["00000000-0000-4000-8000-0000000000d4"] == "REVOCADO"
    assert "00000000-0000-4000-8000-0000000000e5" not in estados


def test_el_operador_demo_es_opcional_e_idempotente(sesion_bd):
    for _ in range(2):
        sembrar_demo.sembrar(sesion_bd, "asesor@solventa.test", "Asesor1234", ("operador@solventa.test", "Operador1234"))
    roles = sorted(u.rol for u in sesion_bd.query(UsuarioInterno).all())
    assert roles == ["ASESOR", "OPERADOR"]
