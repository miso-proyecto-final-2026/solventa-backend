"""Catálogo de productos y campos que acepta el motor (HU38).

Se proyecta desde las reglas versionadas una sola vez al arrancar: el front arma el formulario
con esto y deja de copiar rangos del YAML. Solo lectura y público; web y móvil pueden usarlo.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.cotizacion.reglas import ReglasTarifarias

router = APIRouter(prefix="/v1/productos", tags=["productos"])


class CampoOut(BaseModel):
    nombre: str
    tipo: Literal["entero", "texto"]
    requerido: bool
    min: int | None = None
    max: int | None = None
    valores: list[str] | None = None


class ProductoOut(BaseModel):
    codigo: str
    nombre: str
    campos: list[CampoOut]


class CatalogoOut(BaseModel):
    version_reglas: str
    moneda: str
    productos: list[ProductoOut]


def construir_catalogo(reglas: ReglasTarifarias) -> CatalogoOut:
    return CatalogoOut(
        version_reglas=reglas.version,
        moneda=reglas.moneda,
        productos=[
            ProductoOut(
                codigo=codigo,
                nombre=producto.nombre,
                campos=[
                    CampoOut(
                        nombre=nombre,
                        tipo=campo.tipo,
                        requerido=campo.requerido,
                        min=campo.min,
                        max=campo.max,
                        valores=campo.valores,
                    )
                    for nombre, campo in producto.campos.items()
                ],
            )
            for codigo, producto in reglas.productos.items()
        ],
    )


@router.get(
    "",
    response_model=CatalogoOut,
    summary="Productos cotizables y los campos de cada uno",
    responses={304: {"description": "El catálogo no cambió (If-None-Match)."}},
)
async def listar_productos(
    request: Request, response: Response
) -> CatalogoOut | Response:
    catalogo: CatalogoOut = request.app.state.catalogo
    etag = f'"{catalogo.version_reglas}"'
    cabeceras = {"ETag": etag, "Cache-Control": "public, max-age=300"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cabeceras)
    response.headers.update(cabeceras)
    return catalogo
