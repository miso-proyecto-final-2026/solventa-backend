"""Textos de las notificaciones (HU22), por idioma. Es la plantilla propia del servicio.

- **Push:** lleva el aviso de «Suscripción completada» y, aparte, una versión pública para la pantalla de bloqueo sin
  datos de la póliza (criterio 8): el número de póliza y los importes solo se ven dentro de la app.
- **Correo:** lleva los detalles de la póliza y el enlace de descarga del certificado (criterio 3), que caduca.
Un producto sin traducción no hace fallar el envío: se muestra con su clave.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
        "pago_titulo": "Indemnización pagada",
        "pago_texto": "Te pagamos {monto} de tu indemnización. Toca para ver tu póliza.",
        "pago_texto_sin_monto": "Te pagamos tu indemnización. Toca para ver tu póliza.",
        "firma_asunto": "Revisa y firma tu póliza",
        "firma_intro": "Tu asesor te envió tu póliza para que la revises y la firmes.",
        "firma_boton": "Revisar y firmar",
        "firma_vence": "El enlace vence el {vence}. Para firmar necesitarás el código que te enviaremos. Si no pediste esta póliza, ignora este correo.",
        "asunto": "Tu póliza está activa",
        "saludo": "Hola, {nombre}:",
        "saludo_sin_nombre": "Hola:",
        "intro": "Tu suscripción se completó y tu cobertura ya está activa. Estos son los detalles de tu póliza.",
        "numero": "Número de póliza",
        "producto": "Producto",
        "vigencia": "Vigencia",
        "vigencia_valor": "Desde el {desde} hasta el {hasta}",
        "descarga": "Descarga tu certificado de cobertura:",
        "boton": "Descargar certificado",
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
        "pago_titulo": "Indemnity paid",
        "pago_texto": "We paid you {monto} of your indemnity. Tap to see your policy.",
        "pago_texto_sin_monto": "We paid your indemnity. Tap to see your policy.",
        "firma_asunto": "Review and sign your policy",
        "firma_intro": "Your advisor sent you your policy to review and sign.",
        "firma_boton": "Review and sign",
        "firma_vence": "The link expires on {vence}. To sign, you will need the code we will send you. If you did not request this policy, ignore this email.",
        "asunto": "Your policy is active",
        "saludo": "Hello, {nombre}:",
        "saludo_sin_nombre": "Hello:",
        "intro": "Your subscription is complete and your coverage is now active. Here are the details of your policy.",
        "numero": "Policy number",
        "producto": "Product",
        "vigencia": "Term",
        "vigencia_valor": "From {desde} to {hasta}",
        "descarga": "Download your certificate of coverage:",
        "boton": "Download certificate",
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


def monto_legible(monto: str | None, moneda: str | None, idioma: str) -> str | None:
    """`1500000.00` + `COP` → `$ 1.500.000 COP` (es-CO) o `COP 1,500,000` (en-US). Nulo si el monto no es un número."""
    if not monto:
        return None
    try:
        valor = Decimal(monto)
    except InvalidOperation:
        return None
    moneda = (moneda or "COP").upper()
    entero = f"{valor:,.0f}" if valor == valor.to_integral_value() else f"{valor:,.2f}"
    if idioma == "es-CO":
        return "$ " + entero.replace(",", "X").replace(".", ",").replace("X", ".") + f" {moneda}"
    return f"{moneda} {entero}"


def construir_push_indemnizacion(
    *, envio_id: str, cliente_id: str, poliza_id: str, monto: str | None, moneda: str | None, idioma: str
) -> Push:
    """La versión pública (pantalla de bloqueo) no lleva monto ni póliza; el monto se ve al desbloquear (HU30, criterio 3)."""
    t = TEXTOS[idioma]
    legible = monto_legible(monto, moneda, idioma)
    return Push(
        envio_id=envio_id,
        cliente_id=cliente_id,
        titulo=t["pago_titulo"],
        texto=t["pago_texto"].format(monto=legible) if legible else t["pago_texto_sin_monto"],
        titulo_publico=t["push_titulo_publico"],
        texto_publico=t["push_texto_publico"],
        prioridad=Prioridad.ALTA,
        tipo=TipoNotificacion.INDEMNIZACION_PAGADA,
        suscripcion_id=None,
        poliza_id=poliza_id,
        idioma=idioma,
    )


_PEQUENO = "font-size:13px;color:#555b6b"


def _boton(texto: str, enlace: str) -> str:
    """Botón «a prueba de clientes de correo»: una tabla con el color de fondo y el enlace con estilos en línea.

    Los clientes de correo ignoran casi todo el CSS, así que el botón no depende de clases ni de hojas de estilo. Blanco
    sobre #0a4fd6 tiene un contraste de ~7:1.
    """
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:16px 0"><tr>'
        '<td align="center" bgcolor="#0a4fd6" style="border-radius:8px">'
        f'<a href="{escape(enlace, quote=True)}" target="_blank" rel="noopener" '
        'style="display:inline-block;padding:14px 28px;font-family:Arial,Helvetica,sans-serif;font-size:16px;'
        f'font-weight:bold;color:#ffffff;text-decoration:none;border-radius:8px">{escape(texto)}</a>'
        "</td></tr></table>"
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
        bloque_descarga = f"<p>{escape(t['descarga'])}</p>{_boton(t['boton'], enlace)}"
        if enlace_vence:
            bloque_descarga += f'<p style="{_PEQUENO}">{escape(t["descarga_vence"].format(vence=fecha(enlace_vence, idioma)))}</p>'
    else:
        bloque_descarga = f"<p>{escape(t['sin_enlace'])}</p>"
    html = (
        f'<html lang="{idioma}"><body style="margin:0;padding:24px;background:#f4f6fa;font-family:Arial,Helvetica,sans-serif;'
        f'color:#1b1f2a;font-size:16px;line-height:1.5"><div style="max-width:560px;margin:0 auto;background:#ffffff;'
        f'border-radius:12px;padding:24px">'
        f"<p>{escape(saludo)}</p><p>{escape(t['intro'])}</p>"
        f"<ul><li><b>{escape(t['numero'])}:</b> {escape(numero_poliza)}</li>"
        f"<li><b>{escape(t['producto'])}:</b> {escape(nombre_producto)}</li>"
        f"<li><b>{escape(t['vigencia'])}:</b> {escape(vigencia)}</li></ul>"
        f"{bloque_descarga}<p>{escape(t['cierre'])}</p></div></body></html>"
    )
    return Correo(destinatario=destinatario, asunto=t["asunto"], texto=texto, html=html)


def construir_correo_firma(
    *,
    destinatario: str,
    nombre: str | None,
    idioma: str,
    numero_poliza: str,
    producto: str,
    enlace: str,
    vence: datetime,
) -> Correo:
    """El enlace para firmar (HU39). Mismo formato que la confirmación, con su botón."""
    t = TEXTOS[idioma]
    nombre_producto = t["productos"].get(producto, producto)
    saludo = t["saludo"].format(nombre=nombre) if nombre else t["saludo_sin_nombre"]
    aviso = t["firma_vence"].format(vence=fecha(vence, idioma))
    texto = "\n".join(
        [
            saludo,
            "",
            t["firma_intro"],
            "",
            f"{t['numero']}: {numero_poliza}",
            f"{t['producto']}: {nombre_producto}",
            "",
            f"{t['firma_boton']}: {enlace}",
            aviso,
            "",
            t["cierre"],
        ]
    )
    html = (
        f'<html lang="{idioma}"><body style="margin:0;padding:24px;background:#f4f6fa;font-family:Arial,Helvetica,sans-serif;'
        f'color:#1b1f2a;font-size:16px;line-height:1.5"><div style="max-width:560px;margin:0 auto;background:#ffffff;'
        f'border-radius:12px;padding:24px">'
        f"<p>{escape(saludo)}</p><p>{escape(t['firma_intro'])}</p>"
        f"<ul><li><b>{escape(t['numero'])}:</b> {escape(numero_poliza)}</li>"
        f"<li><b>{escape(t['producto'])}:</b> {escape(nombre_producto)}</li></ul>"
        f"{_boton(t['firma_boton'], enlace)}"
        f'<p style="{_PEQUENO}">{escape(aviso)}</p><p>{escape(t["cierre"])}</p></div></body></html>'
    )
    return Correo(destinatario=destinatario, asunto=t["firma_asunto"], texto=texto, html=html)
