from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Clasificación comercial del tabaco y precio de referencia de liquidación
# (USD por kg neto). Se congela en cada turno al cerrar la descarga.
# ---------------------------------------------------------------------------
CLASES_TABACO = [
    ('A', 'Clase A · Superior'),
    ('B', 'Clase B · Buena'),
    ('C', 'Clase C · Regular'),
    ('D', 'Clase D · Inferior'),
]

PRECIOS_CLASE_USD = {
    'A': Decimal('4.20'),
    'B': Decimal('3.60'),
    'C': Decimal('2.90'),
    'D': Decimal('2.10'),
}

PRECIO_MAXIMO_USD = max(PRECIOS_CLASE_USD.values())

# Duración de cada banda horaria de arribo (minutos)
DURACION_FRANJA_MIN = 120


class EmpresaAcopio(models.Model):
    """
    Entidad compradora o centro de acopio tabacalero (ej. Cooperativa, Alliance One, Massalin).
    """
    nombre = models.CharField(max_length=150, unique=True, verbose_name="Nombre de la Empresa")
    cuit = models.CharField(max_length=20, unique=True, verbose_name="CUIT")
    direccion = models.CharField(max_length=200, blank=True, verbose_name="Dirección de la Planta / Acopio")
    capacidad_diaria_kg = models.PositiveIntegerField(
        default=250000,
        verbose_name="Capacidad Diaria (Kg)",
        help_text="Límite operativo estricto de recepción por jornada (por defecto 250.000 kg)"
    )
    activa = models.BooleanField(default=True, verbose_name="¿Activa para recepción?")

    class Meta:
        verbose_name = "Empresa de Acopio"
        verbose_name_plural = "Empresas de Acopio"
        ordering = ['nombre']

    def __str__(self):
        return f"{self.nombre} (Capacidad: {self.capacidad_diaria_kg:,} kg/día)"


class Turno(models.Model):
    """
    Turno programado de entrega de tabaco por un productor.
    Registra el circuito completo: salida de finca → arribo a garita/playón → descarga en báscula.
    """
    ESTADO_CONFIRMADO = 'CONFIRMADO'
    ESTADO_EN_PLAYA = 'EN_PLAYA'
    ESTADO_DESCARGADO = 'DESCARGADO'
    ESTADO_CANCELADO = 'CANCELADO'

    CLASE_A = 'A'
    CLASE_B = 'B'
    CLASE_C = 'C'
    CLASE_D = 'D'

    ESTADOS_CHOICES = [
        (ESTADO_CONFIRMADO, 'Confirmado'),
        (ESTADO_EN_PLAYA, 'En playa'),
        (ESTADO_DESCARGADO, 'Descargado'),
        (ESTADO_CANCELADO, 'Cancelado'),
    ]

    FRANJAS_HORARIAS = [
        ('08:00 - 10:00', '08:00 a 10:00 hs'),
        ('10:00 - 12:00', '10:00 a 12:00 hs'),
        ('12:00 - 14:00', '12:00 a 14:00 hs'),
        ('14:00 - 16:00', '14:00 a 16:00 hs'),
        ('16:00 - 18:00', '16:00 a 18:00 hs'),
    ]

    codigo = models.CharField(
        max_length=20,
        unique=True,
        editable=False,
        verbose_name="Código de Turno"
    )
    productor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='turnos_solicitados',
        limit_choices_to={'rol': 'productor'},
        verbose_name="Productor"
    )
    empresa = models.ForeignKey(
        EmpresaAcopio,
        on_delete=models.PROTECT,
        related_name='turnos_programados',
        verbose_name="Empresa Compradora"
    )
    kilos_estimados = models.PositiveIntegerField(
        verbose_name="Kilos Estimados",
        help_text="Estimación de carga en kilogramos a entregar"
    )
    patente_vehiculo = models.CharField(
        max_length=20,
        verbose_name="Vehículo / Patente",
        help_text="Dominio del camión, acoplado o camioneta"
    )
    fecha_entrega = models.DateField(
        verbose_name="Fecha de Entrega"
    )
    franja_horaria = models.CharField(
        max_length=30,
        choices=FRANJAS_HORARIAS,
        verbose_name="Franja Horaria"
    )
    estado = models.CharField(
        max_length=20,
        choices=ESTADOS_CHOICES,
        default=ESTADO_CONFIRMADO,
        verbose_name="Estado del Turno"
    )
    observaciones = models.TextField(
        blank=True,
        null=True,
        verbose_name="Observaciones adicionales"
    )

    # --- Trazabilidad temporal del circuito (para analítica gerencial) ---
    hora_salida_origen = models.DateTimeField(
        null=True, blank=True,
        verbose_name="Salida de finca",
        help_text="Momento en que el productor informa que inició el viaje"
    )
    hora_arribo_playa = models.DateTimeField(
        null=True, blank=True,
        verbose_name="Arribo a garita / playón"
    )
    hora_descarga = models.DateTimeField(
        null=True, blank=True,
        verbose_name="Cierre de descarga en báscula"
    )

    # --- Resultado comercial de la descarga ---
    kilos_reales = models.PositiveIntegerField(
        null=True, blank=True,
        verbose_name="Kilos netos en báscula"
    )
    clase_tabaco = models.CharField(
        max_length=1,
        choices=CLASES_TABACO,
        null=True, blank=True,
        verbose_name="Clase asignada"
    )
    precio_kg = models.DecimalField(
        max_digits=8, decimal_places=2,
        null=True, blank=True,
        verbose_name="Precio liquidado (USD/kg)"
    )

    creado_el = models.DateTimeField(auto_now_add=True, verbose_name="Fecha de Creación")
    actualizado_el = models.DateTimeField(auto_now=True, verbose_name="Última Actualización")

    class Meta:
        verbose_name = "Turno de Acopio"
        verbose_name_plural = "Turnos de Acopio"
        ordering = ['fecha_entrega', 'franja_horaria', 'creado_el']
        indexes = [
            models.Index(fields=['fecha_entrega', 'empresa', 'estado'], name='turnos_turn_fecha_e_52419c_idx'),
        ]

    def __str__(self):
        return f"[{self.codigo}] {self.productor.razon_social or self.productor.cuit_dni} - {self.kilos_estimados} kg ({self.fecha_entrega})"

    # ------------------------------------------------------------------
    # Presentación
    # ------------------------------------------------------------------
    @property
    def badge_class(self):
        """Devuelve clase CSS para el badge de estado"""
        if self.estado == self.ESTADO_CONFIRMADO:
            return 'badge-confirmado'
        elif self.estado == self.ESTADO_EN_PLAYA:
            return 'badge-en-playa'
        elif self.estado == self.ESTADO_DESCARGADO:
            return 'badge-descargado'
        return 'badge-cancelado'

    # ------------------------------------------------------------------
    # Métricas de tiempo (minutos)
    # ------------------------------------------------------------------
    @property
    def inicio_franja(self):
        """Datetime (con zona horaria) del inicio de la banda horaria asignada."""
        try:
            hora = int(self.franja_horaria.split(':')[0])
        except (ValueError, AttributeError):
            return None
        return timezone.make_aware(datetime.combine(self.fecha_entrega, time(hora, 0)))

    @staticmethod
    def _minutos_entre(inicio, fin):
        if inicio and fin:
            return round((fin - inicio).total_seconds() / 60, 1)
        return None

    @property
    def minutos_viaje(self):
        """Recorrido externo: salida de finca → arribo a garita."""
        return self._minutos_entre(self.hora_salida_origen, self.hora_arribo_playa)

    @property
    def minutos_espera(self):
        """Permanencia en planta: arribo a garita → cierre de descarga."""
        return self._minutos_entre(self.hora_arribo_playa, self.hora_descarga)

    @property
    def minutos_ciclo(self):
        """Recorrido total puerta a puerta: salida de finca (o arribo) → descarga."""
        return self._minutos_entre(self.hora_salida_origen or self.hora_arribo_playa, self.hora_descarga)

    @property
    def minutos_desvio_arribo(self):
        """Minutos de diferencia entre el arribo y el inicio de la banda (negativo = anticipado)."""
        return self._minutos_entre(self.inicio_franja, self.hora_arribo_playa)

    @property
    def arribo_en_banda(self):
        """True si el camión llegó antes de que cierre su banda horaria de 2 hs."""
        desvio = self.minutos_desvio_arribo
        return desvio is not None and desvio <= DURACION_FRANJA_MIN

    # ------------------------------------------------------------------
    # Métricas comerciales
    # ------------------------------------------------------------------
    @property
    def diferencia_kg(self):
        if self.kilos_reales is None:
            return None
        return self.kilos_reales - self.kilos_estimados

    @property
    def desvio_pct(self):
        if self.kilos_reales is None or not self.kilos_estimados:
            return None
        return round((self.kilos_reales - self.kilos_estimados) * 100.0 / self.kilos_estimados, 1)

    @property
    def importe_usd(self):
        if self.kilos_reales is None or self.precio_kg is None:
            return Decimal('0')
        return (Decimal(self.kilos_reales) * self.precio_kg).quantize(Decimal('0.01'))

    @property
    def perdida_merma_usd(self):
        """Valor de los kilos declarados que no llegaron a báscula."""
        if self.kilos_reales is None or self.precio_kg is None:
            return Decimal('0')
        faltante = max(0, self.kilos_estimados - self.kilos_reales)
        return (Decimal(faltante) * self.precio_kg).quantize(Decimal('0.01'))

    @property
    def perdida_clase_usd(self):
        """Valor dejado de percibir respecto de la clase máxima (A)."""
        if self.kilos_reales is None or self.precio_kg is None:
            return Decimal('0')
        return (Decimal(self.kilos_reales) * (PRECIO_MAXIMO_USD - self.precio_kg)).quantize(Decimal('0.01'))
