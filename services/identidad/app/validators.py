import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CELULAR_RE = re.compile(r"^3\d{9}$")
DOCUMENTO_RE = re.compile(r"^[A-Za-z0-9]{5,15}$")
TIPOS_DOCUMENTO = {"CC", "CE", "PASAPORTE"}


def validar_email(valor: str) -> str:
    if not EMAIL_RE.match(valor):
        raise ValueError("El correo electrónico no tiene un formato válido")
    return valor.lower()


def validar_celular(valor: str) -> str:
    if not CELULAR_RE.match(valor):
        raise ValueError("El celular debe tener 10 dígitos y empezar por 3")
    return valor


def validar_documento(valor: str) -> str:
    if not DOCUMENTO_RE.match(valor):
        raise ValueError("El número de documento debe tener entre 5 y 15 caracteres alfanuméricos")
    return valor


def validar_tipo_documento(valor: str) -> str:
    if valor not in TIPOS_DOCUMENTO:
        raise ValueError(f"El tipo de documento debe ser uno de: {', '.join(sorted(TIPOS_DOCUMENTO))}")
    return valor


def validar_password(valor: str) -> str:
    if len(valor) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres")
    if not re.search(r"[A-Za-z]", valor):
        raise ValueError("La contraseña debe incluir al menos una letra")
    if not re.search(r"\d", valor):
        raise ValueError("La contraseña debe incluir al menos un número")
    return valor
