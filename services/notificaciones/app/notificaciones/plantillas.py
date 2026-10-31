"""Textos de las notificaciones (HU22), por idioma. Es la plantilla propia del servicio.

- **Push:** lleva el aviso de «Suscripción completada» y, aparte, una versión pública para la pantalla de bloqueo sin
  datos de la póliza (criterio 8): el número de póliza y los importes solo se ven dentro de la app.
- **Correo:** lleva los detalles de la póliza y el enlace de descarga del certificado (criterio 3), que caduca.
Un producto sin traducción no hace fallar el envío: se muestra con su clave.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from app.notificaciones.dominio import (
    IDIOMA_POR_DEFECTO,
    IDIOMAS,
    Correo,
    Prioridad,
    Push,
    TipoNotificacion,
)

# Colombia no tiene horario de verano: un desfase fijo evita depender de la base de zonas horarias del sistema.
BOGOTA = timezone(timedelta(hours=-5))
MESES = {
    "es-CO": (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ),
    "en-US": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
}

TEXTOS: dict[str, dict[str, Any]] = {
    "es-CO": {
        "push_titulo": "Suscripción completada",
        "push_texto": "Tu póliza ya está activa. Toca para ver tu certificado.",
        "push_titulo_publico": "Solventa",
        "push_texto_publico": "Tienes una notificación nueva.",
        "asunto": "Tu póliza está activa",
        "saludo": "Hola, {nombre}:",
        "saludo_sin_nombre": "Hola:",
        "intro": "Tu suscripción se completó y tu cobertura ya está activa. Estos son los detalles de tu póliza.",
        "numero": "Número de póliza",
        "producto": "Producto",
        "vigencia": "Vigencia",
        "vigencia_valor": "Desde el {desde} hasta el {hasta}",
        "descarga": "Descarga tu certificado de cobertura:",
        "descarga_vence": "El enlace vence el {vence}. Después puedes descargarlo desde la aplicación.",
        "sin_enlace": "Puedes descargar tu certificado de cobertura desde la aplicación.",
        "cierre": "Gracias por confiar en Solventa.",
        "productos": {
            "microseguro_vida": "Microseguro de vida",
            "proteccion_dispositivos": "Protección de dispositivos",
        },
    },
    "en-US": {
        "push_titulo": "Subscription completed",
        "push_texto": "Your policy is now active. Tap to see your certificate.",
        "push_titulo_publico": "Solventa",
        "push_texto_publico": "You have a new notification.",
        "asunto": "Your policy is active",
        "saludo": "Hello, {nombre}:",
        "saludo_sin_nombre": "Hello:",
        "intro": "Your subscription is complete and your coverage is now active. Here are the details of your policy.",
        "numero": "Policy number",
        "producto": "Product",
        "vigencia": "Term",
        "vigencia_valor": "From {desde} to {hasta}",
        "descarga": "Download your certificate of coverage:",
        "descarga_vence": "The link expires on {vence}. After that you can download it from the app.",
        "sin_enlace": "You can download your certificate of coverage from the app.",
        "cierre": "Thank you for trusting Solventa.",
        "productos": {
            "microseguro_vida": "Micro life insurance",
            "proteccion_dispositivos": "Device protection",
        },
    },
}


def normalizar_idioma(valor: str | None) -> str:
    return valor if valor in IDIOMAS else IDIOMA_POR_DEFECTO


def fecha(valor: datetime, idioma: str) -> str:
    """`26 de octubre de 2026` o `October 26, 2026`, en hora de Colombia."""
    local = valor.astimezone(BOGOTA) if valor.tzinfo else valor
    mes = MESES[idioma][local.month - 1]
    return f"{local.day} de {mes} de {local.year}" if idioma == "es-CO" else f"{mes} {local.day}, {local.year}"


def construir_push(*, envio_id: str, cliente_id: str, suscripcion_id: str, idioma: str) -> Push:
    t = TEXTOS[idioma]
    return Push(
        envio_id=envio_id,
        cliente_id=cliente_id,
        titulo=t["push_titulo"],
        texto=t["push_texto"],
        titulo_publico=t["push_titulo_publico"],
        texto_publico=t["push_texto_publico"],
        prioridad=Prioridad.ALTA,
        tipo=TipoNotificacion.POLIZA_EMITIDA,
        suscripcion_id=suscripcion_id,
        idioma=idioma,
    )


def construir_correo(
    *,
    destinatario: str,
    nombre: str | None,
    idioma: str,
    numero_poliza: str,
    producto: str,
    vigencia_desde: datetime,
    vigencia_hasta: datetime,
    enlace: str | None,
    enlace_vence: datetime | None,
) -> Correo:
    t = TEXTOS[idioma]
    nombre_producto = t["productos"].get(producto, producto)
    saludo = t["saludo"].format(nombre=nombre) if nombre else t["saludo_sin_nombre"]
    vigencia = t["vigencia_valor"].format(desde=fecha(vigencia_desde, idioma), hasta=fecha(vigencia_hasta, idioma))
    if enlace:
        descarga = [t["descarga"], enlace]
        if enlace_vence:
            descarga.append(t["descarga_vence"].format(vence=fecha(enlace_vence, idioma)))
    else:
        descarga = [t["sin_enlace"]]
    texto = "\n".join(
        [
            saludo,
            "",
            t["intro"],
            "",
            f"{t['numero']}: {numero_poliza}",
            f"{t['producto']}: {nombre_producto}",
            f"{t['vigencia']}: {vigencia}",
            "",
            *descarga,
            "",
            t["cierre"],
        ]
    )
    if enlace:
        bloque_descarga = f'<p>{escape(t["descarga"])}<br><a href="{escape(enlace, quote=True)}">{escape(enlace)}</a>'
        if enlace_vence:
            bloque_descarga += f'<br><small>{escape(t["descarga_vence"].format(vence=fecha(enlace_vence, idioma)))}</small>'
        bloque_descarga += "</p>"
    else:
        bloque_descarga = f'<p>{escape(t["sin_enlace"])}</p>'
    html = (
        f'<html lang="{idioma}"><body>'
        f"<p>{escape(saludo)}</p><p>{escape(t['intro'])}</p>"
        f"<ul><li><b>{escape(t['numero'])}:</b> {escape(numero_poliza)}</li>"
        f"<li><b>{escape(t['producto'])}:</b> {escape(nombre_producto)}</li>"
        f"<li><b>{escape(t['vigencia'])}:</b> {escape(vigencia)}</li></ul>"
        f"{bloque_descarga}<p>{escape(t['cierre'])}</p></body></html>"
    )
    return Correo(destinatario=destinatario, asunto=t["asunto"], texto=texto, html=html)
