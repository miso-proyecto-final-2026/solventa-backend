PAYLOAD_VALIDO = {
    "nombre": "Ana",
    "apellido": "Pérez",
    "tipo_documento": "CC",
    "numero_documento": "100200300",
    "email": "ana.perez@example.com",
    "celular": "3001234567",
    "password": "Clave1234",
    "acepto_terminos": True,
}


def _payload(**overrides):
    return {**PAYLOAD_VALIDO, **overrides}


def test_registro_exitoso_crea_cliente_pendiente(client):
    respuesta = client.post("/v1/clientes", json=_payload())

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["estado_kyc"] == "PENDIENTE"
    assert cuerpo["email"] == "ana.perez@example.com"
    assert cuerpo.get("access_token")
    assert "password" not in cuerpo
    assert "password_hash" not in cuerpo


def test_password_no_se_almacena_en_claro(client):
    respuesta = client.post("/v1/clientes", json=_payload())
    assert respuesta.status_code == 201
    assert "Clave1234" not in respuesta.text


def test_rechaza_password_que_no_cumple_la_politica(client):
    respuesta = client.post("/v1/clientes", json=_payload(password="corta1"))
    assert respuesta.status_code == 422
    assert "contraseña" in str(respuesta.json()).lower()


def test_rechaza_email_invalido(client):
    respuesta = client.post("/v1/clientes", json=_payload(email="no-es-un-correo"))
    assert respuesta.status_code == 422


def test_rechaza_celular_invalido(client):
    respuesta = client.post("/v1/clientes", json=_payload(celular="12345"))
    assert respuesta.status_code == 422


def test_rechaza_documento_invalido(client):
    respuesta = client.post("/v1/clientes", json=_payload(numero_documento="#"))
    assert respuesta.status_code == 422


def test_exige_aceptar_terminos(client):
    respuesta = client.post("/v1/clientes", json=_payload(acepto_terminos=False))
    assert respuesta.status_code == 422


def test_cuenta_duplicada_no_revela_cual_dato_existe(client):
    primera = client.post("/v1/clientes", json=_payload())
    assert primera.status_code == 201

    segunda = client.post(
        "/v1/clientes",
        json=_payload(email="otro.correo@example.com"),
    )

    assert segunda.status_code == 409
    detalle = segunda.json()["detail"].lower()
    assert "documento" not in detalle
    assert "correo" not in detalle
    assert "email" not in detalle


def test_idempotencia_misma_clave_no_crea_dos_cuentas(client):
    headers = {"Idempotency-Key": "intento-123"}

    primera = client.post("/v1/clientes", json=_payload(), headers=headers)
    segunda = client.post("/v1/clientes", json=_payload(), headers=headers)

    assert primera.status_code == 201
    assert segunda.status_code == 201
    assert primera.json()["id"] == segunda.json()["id"]

    conteo = client.post("/v1/clientes", json=_payload())
    assert conteo.status_code == 409


def test_idempotencia_tambien_cachea_la_respuesta_de_duplicado(client):
    client.post("/v1/clientes", json=_payload())
    headers = {"Idempotency-Key": "intento-duplicado"}

    primera = client.post(
        "/v1/clientes",
        json=_payload(numero_documento="999888777"),
        headers=headers,
    )
    segunda = client.post(
        "/v1/clientes",
        json=_payload(numero_documento="999888777"),
        headers=headers,
    )

    assert primera.status_code == 409
    assert segunda.status_code == 409
    assert primera.json()["detail"] == segunda.json()["detail"]
