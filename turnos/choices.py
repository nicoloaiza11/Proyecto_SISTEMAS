"""
Enumeraciones y constantes del dominio SOLTAT (diagrama de clases).

Se ubican en un módulo propio, sin modelos, para que tanto `accounts`
(EmpresaAcopio.corte_solicitado) como `turnos` puedan importarlas sin
dependencias circulares.
"""
from django.db import models


class EstadoTurno(models.TextChoices):
    """<<enumeration>> EstadoTurno"""
    PENDIENTE = 'PENDIENTE', 'Pendiente'
    CONFIRMADO = 'CONFIRMADO', 'Confirmado'
    EN_PLAYA = 'EN_PLAYA', 'En playa'
    DESCARGADO = 'DESCARGADO', 'Descargado'
    CANCELADO = 'CANCELADO', 'Cancelado'


class CorteTabaco(models.TextChoices):
    """<<enumeration>> CorteTabaco"""
    BAJERA = 'BAJERA', 'Bajera'
    MEDIANA = 'MEDIANA', 'Mediana'
    SUPERIOR = 'SUPERIOR', 'Superior'


# Ventana operativa del centro de acopio de Perico: 06:00 a 22:00 hs,
# dividida en 8 franjas de 2 horas.
DURACION_FRANJA_MIN = 120
HORA_APERTURA = 6
HORA_CIERRE = 22

FRANJAS_HORARIAS = [
    (f'{h:02d}:00 - {h + 2:02d}:00', f'{h:02d}:00 a {h + 2:02d}:00 hs')
    for h in range(HORA_APERTURA, HORA_CIERRE, 2)
]

# Hora de inicio de cada franja (ej. '06:00 - 08:00' -> 6)
INICIO_FRANJA = {valor: int(valor[:2]) for valor, _ in FRANJAS_HORARIAS}
