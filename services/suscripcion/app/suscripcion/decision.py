"""Decisión automática de suscripción (HU15): aceptar, ajustar, rechazar o derivar a una persona.

Función pura: depende solo del dominio y de las reglas versionadas, para poder cambiar umbrales sin tocar el cobro ni el
motor de rating. Orden de las reglas: datos insuficientes → rechazo → derivación por rango → ajuste → aprobación.
"""

from __future__ import annotations

from app.suscripcion.dominio import Decision, ResultadoDecision, SolicitudDecision
from app.suscripcion.reglas import Reglas


def _pesos(monto: int) -> str:
    return f"${monto:,.0f}".replace(",", ".")


def _porcentaje(fraccion: float) -> str:
    return f"{fraccion * 100:.0f} %"


def _motivos_de_datos_insuficientes(s: SolicitudDecision, r: Reglas) -> list[str]:
    p = s.perfil
    if not p.disponible:
        return [f"El perfil del cliente no está disponible ({p.motivo_indisponible or 'sin respuesta del servicio de perfilamiento'})."]
    if not p.con_consentimiento:
        return ["El cliente no tiene un consentimiento vigente para usar sus datos financieros."]
    motivos = []
    if p.degradado or p.completitud < r.completitud_minima:
        motivos.append(f"El perfil está incompleto (completitud {_porcentaje(p.completitud)}).")
    motivos += [f"Falta el dato requerido «{campo}» en el perfil." for campo in r.campos_requeridos if campo not in p.campos]
    return motivos


def decidir(solicitud: SolicitudDecision, reglas: Reglas) -> ResultadoDecision:
    def resultado(decision: Decision, motivos: list[str], ajuste: dict | None = None) -> ResultadoDecision:
        return ResultadoDecision(decision=decision, motivos=motivos, ajuste=ajuste, reglas_version=reglas.version)

    faltantes = _motivos_de_datos_insuficientes(solicitud, reglas)
    if faltantes:
        return resultado(Decision.DERIVADA, faltantes)

    ratio = float(solicitud.perfil.campos["ratio_endeudamiento"])
    reportes = int(solicitud.perfil.campos["reportes_negativos"])
    monto = solicitud.monto_asegurado

    rechazo = []
    if ratio > reglas.endeudamiento_maximo_con_ajuste:
        rechazo.append(
            f"El nivel de endeudamiento ({_porcentaje(ratio)}) supera el máximo admitido ({_porcentaje(reglas.endeudamiento_maximo_con_ajuste)})."
        )
    if reportes > reglas.reportes_maximo_derivable:
        rechazo.append(f"Registra {reportes} reportes negativos, más de los {reglas.reportes_maximo_derivable} que se admiten revisar.")
    if rechazo:
        return resultado(Decision.RECHAZADA, rechazo)

    derivacion = []
    if monto > reglas.monto_maximo_automatico:
        derivacion.append(
            f"El monto asegurado ({_pesos(monto)}) supera el límite de aprobación automática ({_pesos(reglas.monto_maximo_automatico)})."
        )
    if reglas.reportes_aprobacion < reportes <= reglas.reportes_maximo_derivable:
        derivacion.append(f"Registra {reportes} reporte(s) negativo(s): sale del rango estándar y lo revisa una persona.")
    if derivacion:
        return resultado(Decision.DERIVADA, derivacion)

    ajuste_motivos = []
    if ratio > reglas.endeudamiento_aprobacion:
        ajuste_motivos.append(
            f"El nivel de endeudamiento ({_porcentaje(ratio)}) supera el de aprobación directa ({_porcentaje(reglas.endeudamiento_aprobacion)})."
        )
    if monto > reglas.monto_aprobacion_automatica:
        ajuste_motivos.append(
            f"El monto asegurado ({_pesos(monto)}) supera el de aprobación directa ({_pesos(reglas.monto_aprobacion_automatica)})."
        )
    if ajuste_motivos:
        nuevo = min(monto, reglas.monto_ajustado_a)
        return resultado(Decision.AJUSTADA, ajuste_motivos, {"monto_original": monto, "monto_asegurado": nuevo})

    return resultado(Decision.APROBADA, [])
