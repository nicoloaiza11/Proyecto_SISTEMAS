"""
Entidades de turnos según el diagrama de clases:
CupoProductor, Rampa, Turno, RegistroOperador y Comprobante.
"""
from datetime import datetime, time
from decimal import Decimal

from django.db import models
from django.utils import timezone

from accounts.models import EmpresaAcopio, OperadorAcopio, Productor, Recorredor
from .choices import (
    DURACION_FRANJA_MIN,
    FRANJAS_HORARIAS,
    INICIO_FRANJA,
    CorteTabaco,
    EstadoTurno,
    texto_duracion,
)


class CupoProductor(models.Model):
    """
    Kilos totales que un productor puede entregar a una empresa durante la
    temporada. Lo asigna un recorredor; la empresa es la del recorredor.
    """
    cupo_maximo = models.PositiveBigIntegerField(verbose_name='Cupo máximo de temporada (kg)')
    productor = models.ForeignKey(
        Productor, on_delete=models.CASCADE, related_name='cupos', verbose_name='Productor'
    )
    recorredor = models.ForeignKey(
        Recorredor, on_delete=models.PROTECT, related_name='cupos', verbose_name='Recorredor'
    )

    class Meta:
        verbose_name = 'Cupo de productor'
        verbose_name_plural = 'Cupos de productores'

    def __str__(self):
        return f'{self.productor.get_nombre_completo()} · {self.empresa} · {self.cupo_maximo:,} kg'

    @property
    def empresa(self):
        return self.recorredor.empresa_asignada


class Rampa(models.Model):
    """
    Rampa (puerta) de descarga del centro de acopio. Cada rampa recibe un solo
    turno por franja y la pueden usar solo las empresas habilitadas en ella.
    """
    numero = models.PositiveSmallIntegerField(unique=True, verbose_name='Número')
    empresas = models.ManyToManyField(
        EmpresaAcopio, related_name='rampas', verbose_name='Empresas habilitadas',
        help_text='Empresas que pueden descargar en esta rampa.',
    )
    activa = models.BooleanField(default=True, verbose_name='¿Activa?')

    class Meta:
        verbose_name = 'Rampa de descarga'
        verbose_name_plural = 'Rampas de descarga'
        ordering = ['numero']

    def __str__(self):
        return f'Rampa {self.numero}'


class Turno(models.Model):
    """Turno de entrega de tabaco. Lleva un único corte de tabaco."""

    # Transiciones permitidas entre estados
    TRANSICIONES = {
        EstadoTurno.PENDIENTE: [EstadoTurno.CONFIRMADO, EstadoTurno.CANCELADO],
        EstadoTurno.CONFIRMADO: [EstadoTurno.EN_PLAYA, EstadoTurno.CANCELADO],
        EstadoTurno.EN_PLAYA: [EstadoTurno.DESCARGADO],
        EstadoTurno.DESCARGADO: [],
        EstadoTurno.CANCELADO: [],
    }

    codigo = models.CharField(max_length=20, unique=True, editable=False, verbose_name='Código')
    productor = models.ForeignKey(
        Productor, on_delete=models.PROTECT, related_name='turnos', verbose_name='Productor'
    )
    empresa = models.ForeignKey(
        EmpresaAcopio, on_delete=models.PROTECT, related_name='turnos', verbose_name='Empresa'
    )
    # Asociación Recorredor 0..* Turno: quien asignó o confirmó el turno.
    recorredor = models.ForeignKey(
        Recorredor, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='turnos', verbose_name='Recorredor'
    )
    kilos_estimados = models.PositiveIntegerField(verbose_name='Kilos estimados')
    patente_vehiculo = models.CharField(max_length=20, blank=True, default='', verbose_name='Patente del vehículo')
    fecha_entrega = models.DateField(null=True, blank=True, verbose_name='Fecha de entrega')
    franja_horaria = models.CharField(max_length=20, choices=FRANJAS_HORARIAS, null=True, blank=True, verbose_name='Franja horaria')
    # Turnos de duración variable. Los turnos anteriores no los tienen y se
    # interpretan como la franja completa de 2 h (ver hora_inicio_efectiva).
    hora_inicio = models.TimeField(null=True, blank=True, verbose_name='Hora de inicio')
    hora_fin = models.TimeField(null=True, blank=True, verbose_name='Hora de fin')
    duracion_min = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='Duración (min)')
    rampa = models.ForeignKey(
        Rampa, on_delete=models.PROTECT, null=True, blank=True,
        related_name='turnos', verbose_name='Rampa de descarga',
    )
    fue_confirmado = models.BooleanField(default=False, verbose_name='Fue confirmado')
    cancelado_por_ausencia = models.BooleanField(default=False, verbose_name='Cancelado por ausencia')
    estado = models.CharField(
        max_length=12, choices=EstadoTurno.choices, default=EstadoTurno.PENDIENTE, verbose_name='Estado'
    )
    hora_arribo_playa = models.DateTimeField(null=True, blank=True, verbose_name='Arribo a playa')
    hora_descarga = models.DateTimeField(null=True, blank=True, verbose_name='Descarga')
    kilos_reales = models.PositiveIntegerField(null=True, blank=True, verbose_name='Kilos reales')
    corte_tabaco = models.CharField(max_length=10, choices=CorteTabaco.choices, verbose_name='Corte de tabaco')

    # -----------------------------------------------------------
    # Campos de pronóstico climático (Open-Meteo · Ciudad Perico)
    # -----------------------------------------------------------
    clima_datos = models.JSONField(
        null=True, blank=True,
        verbose_name='Datos climáticos (Open-Meteo)',
        help_text='Snapshot del pronóstico para la fecha/hora del turno.'
    )
    clima_actualizado_el = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Clima actualizado el',
        help_text='Timestamp de la última consulta exitosa a Open-Meteo.'
    )

    class Meta:
        verbose_name = 'Turno'
        verbose_name_plural = 'Turnos'
        ordering = ['fecha_entrega', 'franja_horaria', 'hora_inicio', 'id']
        indexes = [models.Index(fields=['empresa', 'fecha_entrega', 'estado'])]
        # La no superposición en una rampa la garantiza OcupacionRampa.

    def __str__(self):
        fecha = self.fecha_entrega.strftime('%d/%m/%Y') if self.fecha_entrega else 'Pendiente de asignación'
        return f'[{self.codigo}] {self.productor.get_nombre_completo()} · {self.kilos_estimados:,} kg ({fecha})'

    # ------------------------------------------------------------------
    # Métodos del diagrama
    # ------------------------------------------------------------------
    def cambiar_estado(self, nuevo_estado) -> bool:
        """Cambia el estado si la transición es válida. No guarda en la base."""
        if nuevo_estado not in self.TRANSICIONES.get(self.estado, []):
            return False
        self.estado = nuevo_estado
        return True

    def liquidar_descarga(self, kg_reales, clase):
        """
        Registra los kilos reales y el corte (clase) del lote descargado.
        Devuelve el importe si el operador cargó un precio en el comprobante.
        """
        self.kilos_reales = kg_reales
        self.corte_tabaco = clase
        comprobante = getattr(self, 'comprobante', None)
        if comprobante is None or comprobante.precio is None:
            return None
        return (Decimal(kg_reales) * comprobante.precio).quantize(Decimal('0.01'))

    def calcular_tiempo_espera(self):
        """Minutos en planta: arribo a playa -> descarga."""
        return self._minutos_entre(self.hora_arribo_playa, self.hora_descarga)

    def calcular_tiempo_ciclo_total(self):
        """Minutos desde el inicio de la franja asignada (o el arribo, si fue antes) hasta la descarga."""
        inicio = self.inicio_franja
        if self.hora_arribo_playa and (inicio is None or self.hora_arribo_playa < inicio):
            inicio = self.hora_arribo_playa
        return self._minutos_entre(inicio, self.hora_descarga)

    def calcular_perdida_merma(self):
        """Valor de los kilos declarados que no llegaron a báscula (requiere precio cargado)."""
        comprobante = getattr(self, 'comprobante', None)
        if self.kilos_reales is None or comprobante is None or comprobante.precio is None:
            return Decimal('0')
        return (Decimal(self.kilos_merma) * comprobante.precio).quantize(Decimal('0.01'))

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------
    @staticmethod
    def _minutos_entre(inicio, fin):
        if inicio and fin:
            return round((fin - inicio).total_seconds() / 60, 1)
        return None

    @property
    def hora_inicio_efectiva(self):
        """Hora de inicio del turno (o de su franja, en turnos anteriores a la duración variable)."""
        if self.hora_inicio:
            return self.hora_inicio
        hora = INICIO_FRANJA.get(self.franja_horaria)
        return time(hora, 0) if hora is not None else None

    @property
    def hora_fin_efectiva(self):
        if self.hora_fin:
            return self.hora_fin
        hora = INICIO_FRANJA.get(self.franja_horaria)
        return time(hora + DURACION_FRANJA_MIN // 60, 0) if hora is not None else None

    @property
    def duracion_efectiva(self):
        """Duración en minutos."""
        if self.duracion_min:
            return self.duracion_min
        return DURACION_FRANJA_MIN if self.franja_horaria else None

    @property
    def horario_display(self):
        """'08:00 a 13:45' (o '' si todavía no tiene horario)."""
        inicio, fin = self.hora_inicio_efectiva, self.hora_fin_efectiva
        if not inicio or not fin:
            return ''
        return f'{inicio:%H:%M} a {fin:%H:%M}'

    @property
    def duracion_display(self):
        return texto_duracion(self.duracion_efectiva)

    @property
    def duracion_estimada_display(self):
        """Duración que tendrá el turno según los kilos y la velocidad de la empresa."""
        from .services import duracion_turno
        return texto_duracion(duracion_turno(self.empresa, self.kilos_estimados))

    @property
    def inicio_franja(self):
        """Fecha y hora de inicio del turno."""
        inicio = self.hora_inicio_efectiva
        if inicio is None or self.fecha_entrega is None:
            return None
        return timezone.make_aware(datetime.combine(self.fecha_entrega, inicio))

    @property
    def fin_franja(self):
        """Fecha y hora de fin del turno."""
        fin = self.hora_fin_efectiva
        if fin is None or self.fecha_entrega is None:
            return None
        return timezone.make_aware(datetime.combine(self.fecha_entrega, fin))

    @property
    def kilos_efectivos(self):
        return self.kilos_reales if self.kilos_reales is not None else self.kilos_estimados

    @property
    def kilos_merma(self):
        if self.kilos_reales is None:
            return 0
        return max(0, self.kilos_estimados - self.kilos_reales)

    @property
    def desvio_pct(self):
        if self.kilos_reales is None or not self.kilos_estimados:
            return None
        return round((self.kilos_reales - self.kilos_estimados) * 100 / self.kilos_estimados, 1)

    @property
    def badge_class(self):
        return {
            EstadoTurno.PENDIENTE: 'badge-pendiente',
            EstadoTurno.CONFIRMADO: 'badge-confirmado',
            EstadoTurno.EN_PLAYA: 'badge-en-playa',
            EstadoTurno.DESCARGADO: 'badge-descargado',
        }.get(self.estado, 'badge-cancelado')


class OcupacionRampa(models.Model):
    """
    Tramo de 30 minutos de una rampa reservado por un turno. Un turno de
    5 h 30 min crea 11 tramos. La restricción única (rampa, fecha, inicio)
    impide en la base de datos que dos turnos se superpongan en una rampa,
    aun si dos reservas se procesan al mismo tiempo. Al cancelar el turno
    se borran sus tramos y la rampa queda libre.
    """
    turno = models.ForeignKey(Turno, on_delete=models.CASCADE, related_name='ocupaciones')
    rampa = models.ForeignKey(Rampa, on_delete=models.CASCADE, related_name='ocupaciones')
    fecha = models.DateField()
    inicio = models.TimeField(verbose_name='Inicio del tramo')

    class Meta:
        verbose_name = 'Ocupación de rampa'
        verbose_name_plural = 'Ocupaciones de rampas'
        ordering = ['fecha', 'rampa', 'inicio']
        constraints = [
            models.UniqueConstraint(fields=['rampa', 'fecha', 'inicio'], name='rampa_sin_superposicion'),
        ]

    def __str__(self):
        return f'{self.rampa} · {self.fecha:%d/%m/%Y} {self.inicio:%H:%M} · {self.turno.codigo}'


class RegistroOperador(models.Model):
    """Registro del pesaje que hace el operador en báscula."""
    turno = models.OneToOneField(
        Turno, on_delete=models.CASCADE, related_name='registro_operador', verbose_name='Turno'
    )
    operador = models.ForeignKey(
        OperadorAcopio, on_delete=models.PROTECT, related_name='registros', verbose_name='Operador'
    )
    peso_acordado = models.PositiveIntegerField(verbose_name='Peso acordado (kg)')
    peso_real = models.PositiveIntegerField(verbose_name='Peso real (kg)')
    corte_tabaco = models.CharField(max_length=10, choices=CorteTabaco.choices, verbose_name='Corte de tabaco')

    class Meta:
        verbose_name = 'Registro de operador'
        verbose_name_plural = 'Registros de operador'

    def __str__(self):
        return f'{self.turno.codigo} · {self.peso_real:,} kg'


class Comprobante(models.Model):
    """
    Comprobante oficial del turno. Se emite únicamente cuando la empresa de
    acopio finaliza el pesaje y procesamiento en báscula, registrando el peso real,
    el corte real percibido y el precio acordado de forma obligatoria.
    """
    turno = models.OneToOneField(
        Turno, on_delete=models.CASCADE, related_name='comprobante', verbose_name='Turno'
    )
    codigo_turno = models.CharField(max_length=20, verbose_name='Código de turno')
    empresa = models.ForeignKey(
        EmpresaAcopio, on_delete=models.PROTECT, related_name='comprobantes', verbose_name='Empresa'
    )
    fecha_entrega = models.DateField(verbose_name='Fecha de entrega')
    peso_real = models.PositiveIntegerField(default=0, verbose_name='Peso real (kg)')
    precio = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Precio acordado por kg', help_text='Precio acordado cargado en báscula.'
    )
    corte_tabaco = models.CharField(max_length=10, choices=CorteTabaco.choices, verbose_name='Corte real de tabaco')

    class Meta:
        verbose_name = 'Comprobante'
        verbose_name_plural = 'Comprobantes'

    def __str__(self):
        return f'Comprobante {self.codigo_turno}'

    @property
    def importe(self):
        if self.peso_real is None or self.precio is None:
            return None
        return (Decimal(self.peso_real) * self.precio).quantize(Decimal('0.01'))
