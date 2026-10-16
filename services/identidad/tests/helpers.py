"""Utilidades compartidas por las pruebas de Identidad."""

PAYLOAD_CLIENTE = {
    "nombre": "Ana",
    "apellido": "Pérez",
    "tipo_documento": "CC",
    "numero_documento": "100200300",
    "email": "ana.perez@example.com",
    "celular": "3001234567",
    "password": "Clave1234",
    "acepto_terminos": True,
}


def registrar(client, **overrides):
    """Registra un cliente y devuelve (cliente_id, cabeceras de sesión)."""
    cuerpo = client.post("/v1/clientes", json={**PAYLOAD_CLIENTE, **overrides}).json()
    return cuerpo["id"], {"Authorization": f"Bearer {cuerpo['access_token']}"}


def registrar_segundo(client):
    return registrar(
        client, numero_documento="200300400", email="beto@example.com", celular="3109876543"
    )
