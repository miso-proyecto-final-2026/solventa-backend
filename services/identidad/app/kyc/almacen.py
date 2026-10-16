"""Almacén de evidencias KYC: S3 cifrado en el clúster, disco local solo en desarrollo.

Las claves no contienen datos personales: `kyc/<cliente_id>/<verificacion_id>/<parte>.<ext>`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.kyc.config import ConfigKyc
from app.kyc.dominio import AlmacenEvidencias

log = logging.getLogger("identidad.kyc.almacen")


class AlmacenLocal:
    """Solo desarrollo. Los archivos se crean con permisos 0600."""

    def __init__(self, directorio: str) -> None:
        self._raiz = Path(directorio).resolve()

    def guardar(self, clave: str, contenido: bytes, content_type: str) -> None:
        destino = (self._raiz / clave).resolve()
        if self._raiz not in destino.parents:
            raise ValueError("Clave de evidencia inválida")
        destino.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(contenido)


class AlmacenS3:
    def __init__(self, bucket: str, cliente=None) -> None:
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


def construir_almacen(config: ConfigKyc) -> AlmacenEvidencias:
    if config.bucket_evidencias:
        return AlmacenS3(config.bucket_evidencias)
    log.warning(
        "KYC_EVIDENCIAS_BUCKET no está definido: las evidencias se guardan en disco local (solo desarrollo)"
    )
    return AlmacenLocal(config.directorio_local)
