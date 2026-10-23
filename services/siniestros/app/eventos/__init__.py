"""Cliente de eventos de Solventa (HS2). Plantilla: cada servicio la copia en su propio `app/eventos/` (ADR-0001, ADR-0009)."""

from .conexion import ProveedorTokenIam, brokers_desde_entorno, parametros_seguridad
from .consumidor import ConsumidorEventos, PoliticaReintentos
from .publicador import PublicadorEventos, PublicadorKafka, PublicadorMemoria
from .registro import RegistroEventos, tabla_eventos_procesados
from .sobre import Evento, EventoInvalido

__all__ = [
    "ConsumidorEventos",
    "Evento",
    "EventoInvalido",
    "PoliticaReintentos",
    "ProveedorTokenIam",
    "PublicadorEventos",
    "PublicadorKafka",
    "PublicadorMemoria",
    "RegistroEventos",
    "brokers_desde_entorno",
    "parametros_seguridad",
    "tabla_eventos_procesados",
]
