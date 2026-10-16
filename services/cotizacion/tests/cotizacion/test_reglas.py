import copy
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from app.cotizacion.dominio import PerfilUnificado
from app.cotizacion.errores import ErrorReglas
from app.cotizacion.motor import calcular_prima
from app.cotizacion.reglas import DIRECTORIO_REGLAS, ReglasTarifarias, cargar_reglas


@pytest.fixture
def crudo():
    texto = (DIRECTORIO_REGLAS / "v1.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(texto)


def test_carga_las_reglas_v1_con_dos_productos(reglas):
    assert reglas.version == "2026.10.1-prototipo"
    assert set(reglas.productos) == {"microseguro_vida", "proteccion_dispositivos"}
    assert reglas.productos["microseguro_vida"].base.tasa_anual == Decimal("0.0035")


@pytest.mark.parametrize("nombre", ["../v1", "v1/../../x", "", "v 1", "v1.yaml\x00"])
def test_rechaza_nombres_de_version_peligrosos(nombre):
    with pytest.raises(ErrorReglas):
        cargar_reglas(nombre)


def test_version_inexistente():
    with pytest.raises(ErrorReglas, match="no existe"):
        cargar_reglas("v999")


def test_yaml_invalido_se_reporta_como_error_de_reglas(tmp_path):
    (tmp_path / "roto.yaml").write_text("version: [sin cerrar", encoding="utf-8")
    with pytest.raises(ErrorReglas, match="inválidas"):
        cargar_reglas("roto", tmp_path)


def test_clave_desconocida_se_rechaza(crudo):
    crudo["productos"]["microseguro_vida"]["tasa_anaul"] = 1  # error de tipeo
    with pytest.raises(ValidationError):
        ReglasTarifarias.model_validate(crudo)


def test_tabla_con_hueco_se_rechaza(crudo):
    rangos = crudo["productos"]["microseguro_vida"]["factores_datos"]["edad"]["rangos"]
    rangos[1]["hasta"] = 35  # deja un hueco [35, 40)
    with pytest.raises(ValidationError, match="contiguos"):
        ReglasTarifarias.model_validate(crudo)


def test_tabla_que_no_cubre_todo_se_rechaza(crudo):
    rangos = crudo["productos"]["microseguro_vida"]["factores_datos"]["edad"]["rangos"]
    rangos[0]["desde"] = 18
    with pytest.raises(ValidationError, match="cubrir todos"):
        ReglasTarifarias.model_validate(crudo)


def test_senal_que_viola_su_sentido_se_rechaza(crudo):
    senal = crudo["productos"]["microseguro_vida"]["senales"]["ratio_endeudamiento"]
    senal["rangos"][3]["factor"] = 0.50  # más endeudamiento no puede bajar el precio
    with pytest.raises(ValidationError, match="no decrecientes"):
        ReglasTarifarias.model_validate(crudo)


def test_categorias_deben_coincidir_con_los_valores_permitidos(crudo):
    cats = crudo["productos"]["proteccion_dispositivos"]["factores_datos"][
        "tipo_dispositivo"
    ]
    del cats["categorias"]["tableta"]
    with pytest.raises(ValidationError, match="categorías"):
        ReglasTarifarias.model_validate(crudo)


def test_campo_de_valor_debe_existir(crudo):
    crudo["productos"]["microseguro_vida"]["base"]["campo_valor"] = "no_existe"
    with pytest.raises(ValidationError, match="campo_valor"):
        ReglasTarifarias.model_validate(crudo)


def test_cambiar_una_tarifa_cambia_el_precio_sin_tocar_el_motor(
    tmp_path, crudo, reglas
):
    """ASR de modificabilidad: una regla nueva es un archivo, no un cambio de lógica."""
    crudo["version"] = "2026.11.0-prototipo"
    crudo["productos"]["microseguro_vida"]["base"]["tasa_anual"] = 0.0070
    (tmp_path / "v2.yaml").write_text(yaml.safe_dump(crudo), encoding="utf-8")
    v2 = cargar_reglas("v2", tmp_path)
    datos = {"edad": 35, "suma_asegurada": 50_000_000}
    perfil = PerfilUnificado("c1")

    p1 = calcular_prima("microseguro_vida", datos, perfil, reglas)
    p2 = calcular_prima("microseguro_vida", datos, perfil, v2)

    assert (p1.prima_anual, p2.prima_anual) == (Decimal(175000), Decimal(350000))
    assert (p1.version_reglas, p2.version_reglas) == (
        reglas.version,
        "2026.11.0-prototipo",
    )


def test_agregar_un_tercer_producto_no_requiere_cambios_en_el_motor(tmp_path, crudo):
    nuevo = copy.deepcopy(crudo["productos"]["proteccion_dispositivos"])
    nuevo["nombre"] = "Protección de bicicleta"
    nuevo["campos"] = {
        "valor_bicicleta": {"tipo": "entero", "min": 500000, "max": 20000000}
    }
    nuevo["base"] = {
        "campo_valor": "valor_bicicleta",
        "tasa_anual": 0.05,
        "prima_minima": 50000,
    }
    nuevo["factores_datos"] = {}
    nuevo["coberturas"] = [
        {"tipo": "robo", "porcentaje_valor": 1.0, "deducible_porcentaje": 0.2}
    ]
    crudo["productos"]["proteccion_bicicleta"] = nuevo
    (tmp_path / "v3.yaml").write_text(yaml.safe_dump(crudo), encoding="utf-8")
    v3 = cargar_reglas("v3", tmp_path)

    r = calcular_prima(
        "proteccion_bicicleta",
        {"valor_bicicleta": 1_000_000},
        PerfilUnificado("c1"),
        v3,
    )

    # 1.000.000 * 5 % = 50.000; factor de riesgo base 1,05 -> 52.500
    assert r.prima_anual == Decimal(52500)
    assert [c.tipo for c in r.coberturas] == ["robo"]
