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


# Ventana operativa del centro de acopio de Perico: 06:00 a 22:00 hs.
# Las 8 franjas de 2 horas ya no limitan los turnos (ahora duran según los
# kilos, ver más abajo); se conservan para los turnos anteriores, la columna
# Turno.franja_horaria y el formato anterior de la API.
DURACION_FRANJA_MIN = 120
HORA_APERTURA = 6
HORA_CIERRE = 22

FRANJAS_HORARIAS = [
    (f'{h:02d}:00 - {h + 2:02d}:00', f'{h:02d}:00 a {h + 2:02d}:00 hs')
    for h in range(HORA_APERTURA, HORA_CIERRE, 2)
]

# Hora de inicio de cada franja (ej. '06:00 - 08:00' -> 6)
INICIO_FRANJA = {valor: int(valor[:2]) for valor, _ in FRANJAS_HORARIAS}


class HorarioPreferente(models.TextChoices):
    """Parte del día en la que una empresa tiene prioridad sobre sus rampas."""
    MANANA = 'MANANA', 'Mañana (06:00 a 14:00)'
    TARDE = 'TARDE', 'Tarde (14:00 a 22:00)'


# Hora en la que termina la mañana y empieza la tarde.
HORA_INICIO_TARDE = 14


# ---------------------------------------------------------------------------
# Turnos de duración variable
# ---------------------------------------------------------------------------
# La duración de un turno depende de los kilos y de la velocidad de descarga
# de la empresa. Duración y horarios de inicio se redondean a tramos de 30 min.
INTERVALO_MIN = 30
APERTURA_MIN = HORA_APERTURA * 60   # 06:00 -> 360
CIERRE_MIN = HORA_CIERRE * 60       # 22:00 -> 1320
INICIO_TARDE_MIN = HORA_INICIO_TARDE * 60


def a_minutos(hora) -> int:
    """time(8, 30) -> 510"""
    return hora.hour * 60 + hora.minute


def a_hora(minutos):
    """510 -> time(8, 30)"""
    from datetime import time
    return time(minutos // 60, minutos % 60)


def horario_de_minuto(minuto) -> str:
    """Parte del día (mañana o tarde) de un tramo que empieza en `minuto`."""
    return HorarioPreferente.TARDE if minuto >= INICIO_TARDE_MIN else HorarioPreferente.MANANA


def franja_de_minuto(minuto):
    """Franja de 2 h que contiene el minuto (se guarda en Turno.franja_horaria por compatibilidad)."""
    for valor, _ in FRANJAS_HORARIAS:
        if INICIO_FRANJA[valor] * 60 <= minuto < INICIO_FRANJA[valor] * 60 + DURACION_FRANJA_MIN:
            return valor
    return None


def texto_duracion(minutos) -> str:
    """345 -> '5 h 45 min', 120 -> '2 h', 30 -> '30 min'"""
    if minutos is None:
        return ''
    horas, resto = divmod(int(minutos), 60)
    if horas and resto:
        return f'{horas} h {resto} min'
    return f'{horas} h' if horas else f'{resto} min'
