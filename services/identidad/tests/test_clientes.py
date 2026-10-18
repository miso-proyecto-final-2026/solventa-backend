import hashlib
from datetime import UTC, datetime, timedelta

from app import idempotencia
from app.models import IdempotenciaCliente

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


# -- idempotencia compartida entre réplicas (HU01 criterio 6) -----------------------------------
# Antes vivía en la memoria de cada proceso: con dos pods el reintento podía recibir un 409.


def _clave_hash(clave: str) -> str:
    return hashlib.sha256(clave.encode()).hexdigest()


def test_el_reintento_devuelve_el_mismo_cliente_con_una_sesion_nueva_y_valida(client):
    headers = {"Idempotency-Key": "reintento-sesion"}

    primera = client.post("/v1/clientes", json=_payload(), headers=headers).json()
    segunda = client.post("/v1/clientes", json=_payload(), headers=headers)

    assert segunda.status_code == 201
    repetida = segunda.json()
    assert repetida["id"] == primera["id"]
    assert repetida["creado_en"] == primera["creado_en"]
    # El token no se guarda: el reintento recibe una sesión nueva, y las dos funcionan.
    assert repetida["access_token"] != primera["access_token"]
    for token in (primera["access_token"], repetida["access_token"]):
        otorgado = client.post(
            f"/v1/clientes/{primera['id']}/consentimientos",
            json={"version_texto": "v1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert otorgado.status_code in (200, 201)


def test_la_idempotencia_queda_en_la_base_de_datos_sin_la_clave_ni_el_token(client, sesion_bd):
    clave = "clave-que-no-debe-guardarse-en-claro"
    primera = client.post("/v1/clientes", json=_payload(), headers={"Idempotency-Key": clave}).json()

    filas = sesion_bd.query(IdempotenciaCliente).all()

    assert len(filas) == 1
    fila = filas[0]
    assert fila.clave_hash == _clave_hash(clave)
    assert fila.status_code == 201
    assert fila.cliente_id == primera["id"]
    assert fila.expira_en - fila.creado_en == timedelta(hours=24)
    # Ningún valor persistido contiene la clave ni el access_token.
    valores = [str(v) for v in vars(fila).values()]
    assert not any(clave in v or primera["access_token"] in v for v in valores)


def test_el_409_de_duplicado_se_repite_desde_la_base_de_datos(client, sesion_bd):
    client.post("/v1/clientes", json=_payload())
    headers = {"Idempotency-Key": "duplicado-persistido"}

    primera = client.post("/v1/clientes", json=_payload(), headers=headers)
    segunda = client.post("/v1/clientes", json=_payload(), headers=headers)

    assert primera.status_code == segunda.status_code == 409
    fila = sesion_bd.get(IdempotenciaCliente, _clave_hash("duplicado-persistido"))
    assert fila is not None and fila.status_code == 409 and fila.cliente_id is None


def test_una_clave_vencida_deja_de_aplicar(client, sesion_bd, monkeypatch):
    headers = {"Idempotency-Key": "clave-vencida"}
    client.post("/v1/clientes", json=_payload(), headers=headers)

    en_25_horas = datetime.now(UTC) + timedelta(hours=25)
    monkeypatch.setattr(idempotencia, "_ahora", lambda: en_25_horas)
    # Vencida la clave, el mismo registro ya no se trata como reintento: es un duplicado real.
    despues = client.post("/v1/clientes", json=_payload(), headers=headers)

    assert despues.status_code == 409


def test_al_escribir_se_purgan_las_claves_vencidas(client, sesion_bd, monkeypatch):
    client.post("/v1/clientes", json=_payload(), headers={"Idempotency-Key": "vieja-1"})
    en_25_horas = datetime.now(UTC) + timedelta(hours=25)
    monkeypatch.setattr(idempotencia, "_ahora", lambda: en_25_horas)

    client.post(
        "/v1/clientes",
        json=_payload(numero_documento="200300400", email="otra@example.com"),
        headers={"Idempotency-Key": "nueva-1"},
    )

    sesion_bd.expire_all()
    claves = {f.clave_hash for f in sesion_bd.query(IdempotenciaCliente).all()}
    assert claves == {_clave_hash("nueva-1")}
