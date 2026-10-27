"""Pruebas del detector de números de tarjeta (HA05, criterio 5). Solo usa números de prueba públicos de las marcas."""

import json

import pytest
from barrido_pan import (
    barrer_json,
    barrer_texto,
    es_pan_probable,
    luhn,
    resumen_texto,
)

# Números de prueba públicos de cada marca: no son tarjetas reales.
VISA = "4111111111111111"
MASTERCARD = "5555555555554444"
AMEX = "378282246310005"
DINERS = "30569309025904"


@pytest.mark.parametrize("pan", [VISA, MASTERCARD, AMEX, DINERS])
def test_los_numeros_de_prueba_de_las_marcas_se_detectan(pan):
    assert luhn(pan)
    assert es_pan_probable(pan)


@pytest.mark.parametrize("texto", ["4111 1111 1111 1111", "4111-1111-1111-1111", "5555 5555 5555 4444", "3782 822463 10005"])
def test_se_detectan_con_espacios_o_guiones(texto):
    assert len(barrer_texto(texto)) == 1


def test_se_detectan_dentro_de_un_texto_mas_largo():
    hallazgos = barrer_texto(f"pago rechazado tarjeta={VISA} cliente=abc")
    assert [h.marca for h in hallazgos] == ["visa"]


@pytest.mark.parametrize(
    "ruido",
    [
        "1761600000000",  # marca de tiempo en milisegundos (13 dígitos)
        "1761600000000123",  # microsegundos
        "a803212e-8c33-42cf-9b37-11c290503ab7",  # UUID
        "4111111111111112",  # 16 dígitos que no pasan el dígito de control
        "3001234567",  # celular
        "9999999999999999",  # pasa Luhn pero ninguna marca empieza por 9
        "00000000000000000000",  # 20 dígitos
        "2026-10-27T18:36:00",  # fecha
    ],
)
def test_el_ruido_comun_no_se_confunde_con_un_pan(ruido):
    assert barrer_texto(ruido) == []


def test_un_numero_de_prueba_dentro_de_un_numero_largo_no_cuenta():
    assert barrer_texto("9" + VISA + "9") == []


def test_los_hallazgos_nunca_contienen_el_numero():
    hallazgos = barrer_texto(f"x {VISA} y")
    volcado = repr(hallazgos) + resumen_texto(hallazgos)
    assert VISA not in volcado and "4111" not in volcado and "1111" not in volcado


def test_se_recorren_estructuras_json_anidadas():
    cuerpo = {"a": {"b": [{"nota": f"tarjeta {VISA}"}]}, "ok": "sin datos", "n": 7}
    hallazgos = barrer_json(cuerpo)
    assert len(hallazgos) == 1 and hallazgos[0].ruta == "a.b[0].nota"
    assert barrer_json(json.loads('{"x": "nada"}')) == []


def test_un_valor_numerico_json_tambien_se_revisa():
    assert len(barrer_json({"tarjeta": int(VISA)})) == 1


# --- barrido de la base de datos y de logs -------------------------------------------------------------------------

import sqlalchemy as sa
from barrido_pan import barrer_base, barrer_logs, main


@pytest.fixture
def base():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("create table cobros (id integer primary key, nota text, datos json, monto integer, tarjeta bigint)"))
        c.execute(sa.text("create table limpia (id integer primary key, descripcion text, creada bigint)"))
        c.execute(
            sa.text("insert into cobros values (1, :n, :d, 38500, :t)"),
            {"n": f"reintento con {VISA}", "d": json.dumps({"a": {"b": MASTERCARD}}), "t": int(AMEX)},
        )
        c.execute(sa.text("insert into cobros values (2, 'sin datos', :d, 38500, 1761600000000)"), {"d": json.dumps({"x": "ok"})})
        c.execute(sa.text("insert into limpia values (1, 'texto normal', 1761600000000)"))
    return engine


def test_el_barrido_de_la_base_encuentra_columnas_de_texto_json_y_numericas(base):
    resultado = barrer_base(base)
    por_columna = {(r.tabla, r.columna): r.coincidencias for r in resultado}

    assert por_columna[("cobros", "nota")] == 1
    assert por_columna[("cobros", "datos")] == 1
    assert por_columna[("cobros", "tarjeta")] == 1


def test_el_barrido_no_marca_columnas_limpias_ni_marcas_de_tiempo(base):
    por_columna = {(r.tabla, r.columna): r.coincidencias for r in barrer_base(base)}

    assert por_columna[("limpia", "descripcion")] == 0
    assert por_columna[("limpia", "creada")] == 0
    assert por_columna[("cobros", "monto")] == 0


def test_el_barrido_cuenta_filas_y_columnas_revisadas(base):
    resultado = barrer_base(base)

    assert {r.tabla for r in resultado} == {"cobros", "limpia"}
    assert next(r for r in resultado if (r.tabla, r.columna) == ("cobros", "nota")).filas == 2


def test_el_informe_de_la_base_no_contiene_ningun_numero(base, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    resultado = barrer_base(base)
    volcado = repr(resultado) + repr([r.__dict__ for r in resultado])

    for pan in (VISA, MASTERCARD, AMEX):
        assert pan not in volcado


def test_el_barrido_de_logs_cuenta_lineas_con_pan(tmp_path):
    texto = "\n".join(["INFO todo bien", f"WARN tarjeta {VISA} rechazada", "INFO ts=1761600000000", f"ERROR otra {MASTERCARD}"])

    hallazgos = barrer_logs(texto.splitlines())

    assert [h.linea for h in hallazgos] == [2, 4]
    assert VISA not in repr(hallazgos) and MASTERCARD not in repr(hallazgos)


def test_la_linea_de_comandos_de_logs_sale_con_error_si_hay_pan(capsys, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"x {VISA}\ny\n"))

    codigo = main(["logs"])

    salida = capsys.readouterr().out
    assert codigo == 1 and "1 línea" in salida and VISA not in salida


def test_la_linea_de_comandos_de_logs_sale_limpia_sin_pan(capsys, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("todo normal\nts=1761600000000\n"))

    assert main(["logs"]) == 0
    assert "0 líneas" in capsys.readouterr().out
