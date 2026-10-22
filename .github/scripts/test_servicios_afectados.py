import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from servicios_afectados import CONFIG, servicios_afectados

SERVICIOS = [
    {"carpeta": "cotizacion", "servicio": "solventa-backend"},
    {"carpeta": "identidad", "servicio": "solventa-identidad"},
]


def nombres(archivos):
    return [s["servicio"] for s in servicios_afectados(archivos, SERVICIOS)]


def test_un_servicio():
    assert nombres(["services/identidad/app/main.py"]) == ["solventa-identidad"]


def test_varios_archivos_del_mismo_servicio_no_duplican():
    assert nombres(["services/identidad/a.py", "services/identidad/tests/b.py"]) == ["solventa-identidad"]


def test_dos_servicios_en_orden_estable():
    assert nombres(["services/identidad/a.py", "services/cotizacion/b.py"]) == [
        "solventa-backend",
        "solventa-identidad",
    ]


def test_rutas_fuera_de_services_no_despliegan_nada():
    assert nombres(["README.md", ".github/workflows/ci.yml", "docs/api/x.yaml"]) == []


def test_servicio_no_registrado_se_ignora():
    assert nombres(["services/nuevo/app/main.py"]) == []


def test_archivo_suelto_en_services_no_cuenta():
    assert nombres(["services/identidad"]) == []


def test_la_configuracion_real_es_valida_y_sin_duplicados():
    servicios = json.loads(CONFIG.read_text(encoding="utf-8"))["servicios"]
    assert servicios and all({"carpeta", "servicio"} <= set(s) for s in servicios)
    assert len({s["carpeta"] for s in servicios}) == len(servicios)
    assert len({s["servicio"] for s in servicios}) == len(servicios)
    for s in servicios:
        assert (CONFIG.parent.parent / "services" / s["carpeta"]).is_dir()


def test_el_script_imprime_la_matriz_de_github_actions():
    script = Path(__file__).parent / "servicios_afectados.py"
    salida = subprocess.run(
        [sys.executable, str(script)],
        input="services/perfilamiento/app/x.py\nREADME.md\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(salida) == {"include": [{"carpeta": "perfilamiento", "servicio": "solventa-perfilamiento"}]}
    assert "\n" == salida[-1] and " " not in salida.strip()
