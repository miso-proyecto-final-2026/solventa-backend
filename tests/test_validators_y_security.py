import pytest

from app.security import hash_password, verify_password
from app.validators import (
    validar_celular,
    validar_documento,
    validar_email,
    validar_password,
    validar_tipo_documento,
)


@pytest.mark.parametrize(
    "valor",
    ["ana@example.com", "ANA.PEREZ@Example.Com"],
)
def test_validar_email_acepta_formatos_validos(valor):
    assert "@" in validar_email(valor)


@pytest.mark.parametrize("valor", ["no-es-correo", "sin-arroba.com", "a@b"])
def test_validar_email_rechaza_formatos_invalidos(valor):
    with pytest.raises(ValueError):
        validar_email(valor)


def test_validar_celular_exige_diez_digitos_empezando_en_3():
    assert validar_celular("3001234567") == "3001234567"
    with pytest.raises(ValueError):
        validar_celular("2001234567")
    with pytest.raises(ValueError):
        validar_celular("300123")


def test_validar_documento_acepta_alfanumerico_entre_5_y_15():
    assert validar_documento("CC12345") == "CC12345"
    with pytest.raises(ValueError):
        validar_documento("123")
    with pytest.raises(ValueError):
        validar_documento("#######")


def test_validar_tipo_documento():
    assert validar_tipo_documento("CC") == "CC"
    with pytest.raises(ValueError):
        validar_tipo_documento("RUT")


def test_validar_password_politica_minima():
    assert validar_password("Clave1234") == "Clave1234"
    with pytest.raises(ValueError):
        validar_password("corta1")
    with pytest.raises(ValueError):
        validar_password("solamenteletras")
    with pytest.raises(ValueError):
        validar_password("12345678")


def test_hash_password_no_devuelve_el_texto_en_claro():
    hash_resultante = hash_password("Clave1234")
    assert hash_resultante != "Clave1234"
    assert verify_password("Clave1234", hash_resultante)
    assert not verify_password("otra-clave", hash_resultante)
