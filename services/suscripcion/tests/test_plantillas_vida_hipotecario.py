"""HU26: la póliza y el certificado nombran el producto `vida_hipotecario` y su amparo, sin texto genérico."""

from app.suscripcion.certificado import cargar_textos
from app.suscripcion.documento import cargar_plantilla


def test_la_poliza_tiene_nombre_y_naturaleza_del_riesgo_de_vida_hipotecario(caplog):
    plantilla = cargar_plantilla()

    nombre, naturaleza = plantilla.producto("vida_hipotecario")

    assert nombre == "Seguro de vida hipotecario"
    assert "crédito hipotecario" in naturaleza
    assert "producto sin plantilla" not in caplog.text


def test_la_poliza_nombra_los_amparos_de_la_oferta_de_perfilamiento():
    plantilla = cargar_plantilla()

    assert plantilla.amparo("muerte") == "Muerte"
    assert plantilla.amparo("incapacidad_total_permanente") == "Incapacidad total y permanente"


def test_el_certificado_nombra_el_producto_y_el_amparo_en_los_dos_idiomas():
    textos = cargar_textos()

    assert textos["es-CO"]["productos"]["vida_hipotecario"] == "Seguro de vida hipotecario"
    assert textos["en-US"]["productos"]["vida_hipotecario"] == "Mortgage life insurance"
    assert textos["es-CO"]["amparos"]["incapacidad_total_permanente"] == "Incapacidad total y permanente"
    assert textos["en-US"]["amparos"]["incapacidad_total_permanente"] == "Total and permanent disability"
