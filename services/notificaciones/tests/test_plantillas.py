"""Textos de las notificaciones (HU22, criterios 8 y 9): idioma, formatos y contenido seguro."""

from datetime import UTC, datetime

import pytest
from app.notificaciones.dominio import IDIOMAS, Prioridad
from app.notificaciones.plantillas import (
    TEXTOS,
    construir_correo,
    construir_push,
    fecha,
    normalizar_idioma,
)

DESDE = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
HASTA = datetime(2027, 10, 30, 20, 0, tzinfo=UTC)


def _correo(idioma="es-CO", **cambios):
    datos = {
        "destinatario": "ana@example.com", "nombre": "Ana", "idioma": idioma, "numero_poliza": "36GBQKV2CWMM3EBB8Q6Q",
        "producto": "microseguro_vida", "vigencia_desde": DESDE, "vigencia_hasta": HASTA,
        "enlace": "https://borde.example/v1/certificados/abc", "enlace_vence": datetime(2026, 10, 31, 20, 0, tzinfo=UTC),
    }
    datos.update(cambios)
    return construir_correo(**datos)


def test_ambos_idiomas_tienen_las_mismas_claves():
    assert set(TEXTOS) == set(IDIOMAS)
    assert set(TEXTOS["es-CO"]) == set(TEXTOS["en-US"])
    assert set(TEXTOS["es-CO"]["productos"]) == set(TEXTOS["en-US"]["productos"])


@pytest.mark.parametrize(("valor", "esperado"), [("es-CO", "es-CO"), ("en-US", "en-US"), ("fr-FR", "es-CO"), (None, "es-CO"), ("", "es-CO")])
def test_un_idioma_que_no_hay_cae_a_es_co(valor, esperado):
    assert normalizar_idioma(valor) == esperado


def test_las_fechas_salen_en_el_formato_y_la_zona_de_cada_idioma():
    madrugada = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)  # todavía 31 de octubre en Colombia

    assert fecha(madrugada, "es-CO") == "31 de octubre de 2026"
    assert fecha(madrugada, "en-US") == "October 31, 2026"


def test_el_push_es_de_prioridad_alta_y_su_version_publica_no_dice_nada_de_la_poliza():
    for idioma in IDIOMAS:
        push = construir_push(envio_id="e1", cliente_id="c1", suscripcion_id="s1", idioma=idioma)

        assert push.prioridad is Prioridad.ALTA
        assert push.titulo != push.titulo_publico
        assert push.titulo_publico == "Solventa"
        for texto in (push.titulo, push.texto, push.titulo_publico, push.texto_publico):
            assert not any(c.isdigit() for c in texto)


def test_el_correo_en_espanol_lleva_los_detalles_y_el_enlace():
    c = _correo()

    assert c.asunto == "Tu póliza está activa"
    for esperado in ("Hola, Ana:", "36GBQKV2CWMM3EBB8Q6Q", "Microseguro de vida", "30 de octubre de 2026", "https://borde.example/v1/certificados/abc"):
        assert esperado in c.texto
    assert "El enlace vence el 31 de octubre de 2026" in c.texto


def test_el_correo_en_ingles_traduce_todo():
    c = _correo("en-US")

    assert c.asunto == "Your policy is active"
    for esperado in ("Hello, Ana:", "Policy number", "Micro life insurance", "From October 30, 2026 to October 30, 2027", "expires on October 31, 2026"):
        assert esperado in c.texto
    assert "Hola" not in c.texto and "póliza" not in c.texto


def test_sin_nombre_el_saludo_es_generico():
    assert _correo(nombre=None).texto.startswith("Hola:")


def test_un_producto_sin_traduccion_se_muestra_con_su_clave():
    assert "producto_nuevo" in _correo(producto="producto_nuevo").texto


def test_el_html_escapa_lo_que_viene_de_fuera():
    c = _correo(nombre="<script>alert(1)</script>", enlace='https://x.example/?a="1"&b=<2>')

    assert "<script>" not in c.html and "&lt;script&gt;" in c.html
    assert 'href="https://x.example/?a=&quot;1&quot;&amp;b=&lt;2&gt;"' in c.html
    assert '<html lang="es-CO">' in c.html


def test_el_html_ofrece_un_boton_y_no_muestra_la_url_como_texto():
    c = _correo()

    assert 'href="https://borde.example/v1/certificados/abc"' in c.html
    assert ">Descargar certificado</a>" in c.html
    assert ">https://borde.example/v1/certificados/abc<" not in c.html  # la URL no se ve como texto
    assert "https://borde.example/v1/certificados/abc" in c.texto  # la versión de texto plano sí la lleva
    assert "El enlace vence el 31 de octubre de 2026" in c.html


def test_el_boton_en_ingles_se_titula_en_ingles():
    c = _correo("en-US")

    assert ">Download certificate</a>" in c.html and "Descargar" not in c.html


def test_sin_enlace_el_correo_manda_a_la_aplicacion():
    c = _correo(enlace=None, enlace_vence=None)

    assert "desde la aplicación" in c.texto and "href" not in c.html
