"""Almacén de los documentos de póliza: S3 cifrado en el clúster, disco local solo en desarrollo.

Las claves no contienen datos personales: `polizas/<suscripcion_id>/<numero_poliza>.pdf`. Es una copia del patrón de
`services/identidad/app/kyc/almacen.py`: los servicios no comparten código (ADR-0001, ADR-0009).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol


class AlmacenDocumentos(Protocol):
    def guardar(self, clave: str, contenido: bytes, content_type: str) -> None: ...

    def leer(self, clave: str) -> bytes: ...


class DocumentoNoEncontrado(Exception):
    """La clave no existe en el almacén."""


class AlmacenLocal:
    """Solo desarrollo. Los archivos se crean con permisos 0600."""

    def __init__(self, directorio: str) -> None:
        self._raiz = Path(directorio).resolve()

    def _ruta(self, clave: str) -> Path:
        destino = (self._raiz / clave).resolve()
        if self._raiz not in destino.parents:
            raise ValueError("Clave de documento inválida")
        return destino

    def leer(self, clave: str) -> bytes:
        try:
            return self._ruta(clave).read_bytes()
        except FileNotFoundError:
            raise DocumentoNoEncontrado(clave) from None

    def guardar(self, clave: str, contenido: bytes, content_type: str) -> None:
        destino = self._ruta(clave)
        destino.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(contenido)


class AlmacenS3:
    """`put_object` cifrado. Respeta `AWS_ENDPOINT_URL` (Moto en local) y el rol del pod en el clúster."""

    def __init__(self, bucket: str, cliente: Any = None) -> None:
        if cliente is None:
            import boto3

            cliente = boto3.client("s3")
        self._bucket = bucket
        self._s3 = cliente

    def guardar(self, clave: str, contenido: bytes, content_type: str) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=clave,
            Body=contenido,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

    def leer(self, clave: str) -> bytes:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=clave)["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            raise DocumentoNoEncontrado(clave) from None
