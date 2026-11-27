"""
Usuarios del sistema SOLTAT según el diagrama de clases.

    Usuario  (clase base, con login)
      ├── Productor
      ├── OperadorAcopio
      ├── Recorredor        -> empresa_asignada : EmpresaAcopio
      ├── Administrador
      └── EmpresaAcopio

Se implementa con herencia multitabla de Django: cada subclase tiene su
propia tabla enlazada uno a uno con la tabla de Usuario.

Los métodos de negocio delegan en `turnos.services` (importado de forma
diferida para evitar dependencias circulares entre aplicaciones).
"""
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from turnos.choices import CorteTabaco, HorarioPreferente


CAPACIDAD_DIARIA_MIN_KG = 200_000
CAPACIDAD_DIARIA_MAX_KG = 300_000


class Usuario(AbstractUser):
    """
    Clase base de todos los usuarios.
    Atributos del diagrama: id, username, password, cuit_dni, telefono.
    """
    cuit_dni = models.CharField(
        max_length=20,
        unique=True,
        verbose_name='DNI / CUIT',
    )
    telefono = models.CharField(
        max_length=30,
        blank=True,
        verbose_name='Teléfono (WhatsApp)',
        help_text='Con código de país y de área, ej. +54 388 4912345',
    )

    # Orden en que se busca la subclase concreta de un Usuario.
    TIPOS = ('productor', 'recorredor', 'operadoracopio', 'empresaacopio', 'administrador')
    TIPO = None  # Lo redefine cada subclase

    TIPOS_DISPLAY = {
        'productor': 'Productor',
        'recorredor': 'Recorredor',
        'operadoracopio': 'Operador de Acopio',
        'empresaacopio': 'Empresa de Acopio',
        'administrador': 'Administrador',
    }

    class Meta:
        verbose_name = 'Usuario'
        verbose_name_plural = 'Usuarios'
        ordering = ['cuit_dni']

    def __str__(self):
        return f'{self.get_nombre_completo()} ({self.cuit_dni})'

    # ------------------------------------------------------------------
    # Tipo concreto de usuario
    # ------------------------------------------------------------------
    @property
    def tipo(self):
        """Nombre de la subclase concreta ('productor', 'recorredor', ...) o None."""
        if self.TIPO:
            return self.TIPO
        if not hasattr(self, '_tipo_cache'):
            self._tipo_cache = next((t for t in self.TIPOS if hasattr(self, t)), None)
        return self._tipo_cache

    @property
    def perfil(self):
        """Instancia de la subclase concreta (Productor, Recorredor, ...)."""
        if self.TIPO or self.tipo is None:
            return self
        return getattr(self, self.tipo)

    @property
    def tipo_display(self):
        if self.tipo:
            return self.TIPOS_DISPLAY[self.tipo]
        return 'Superusuario' if self.is_superuser else 'Sin perfil'

    @property
    def es_productor(self):
        return self.tipo == 'productor'

    @property
    def es_recorredor(self):
        return self.tipo == 'recorredor'

    @property
    def es_operador(self):
        return self.tipo == 'operadoracopio'

    @property
    def es_empresa(self):
        return self.tipo == 'empresaacopio'

    @property
    def es_administrador(self):
        return self.tipo == 'administrador' or self.is_superuser

    # ------------------------------------------------------------------
    # Métodos del diagrama
    # ------------------------------------------------------------------
    def iniciar_sesion(self, request, password) -> bool:
        user = authenticate(request, username=self.username, password=password)
        if user is None or not user.is_active:
            return False
        login(request, user)
        return True

    def cerrar_sesion(self, request) -> None:
        logout(request)

    def get_nombre_completo(self) -> str:
        if self.tipo == 'empresaacopio':
            return self.perfil.nombre
        return self.get_full_name() or self.username


class Productor(Usuario):
    TIPO = 'productor'

    class Meta:
        verbose_name = 'Productor'
        verbose_name_plural = 'Productores'

    def solicitar_turno(self, empresa, kg, corte_tabaco, patente_vehiculo=''):
        from turnos import services
        return services.solicitar_turno(self, empresa, kg, corte_tabaco, patente_vehiculo)

    def consultar_disponibilidad(self, empresa, fecha):
        from turnos import services
        return services.consultar_disponibilidad(empresa, fecha)

    def cancelar_turno(self, turno):
        from turnos import services
        return services.cancelar_turno_productor(self, turno)

    def cancelar_turno_confirmado(self, turno, mensaje):
        from turnos import services
        return services.cancelar_turno_productor(self, turno, mensaje)

    def consultar_mis_turnos(self):
        return list(self.turnos.select_related('empresa').order_by('-fecha_entrega', 'franja_horaria'))

    def consultar_rendimiento(self):
        from turnos import services
        return services.rendimiento_productor(self)

    def obtener_remanente(self, turno) -> int:
        """Kilos que le quedan del cupo de temporada con la empresa del turno."""
        from turnos import services
        return services.remanente_cupo(self, turno.empresa)


class OperadorAcopio(Usuario):
    """Opera la garita y la báscula del centro de acopio (todas las empresas)."""
    TIPO = 'operadoracopio'

    class Meta:
        verbose_name = 'Operador de Acopio'
        verbose_name_plural = 'Operadores de Acopio'

    def registrar_ingreso_garita(self, turno, patente_vehiculo=None) -> None:
        from turnos import services
        services.registrar_ingreso_garita(self, turno, patente_vehiculo=patente_vehiculo)

    def registrar_pesaje_y_descarga(self, turno, peso_real, corte_tabaco, precio=None):
        from turnos import services
        return services.registrar_pesaje_y_descarga(self, turno, peso_real, corte_tabaco, precio)


class Administrador(Usuario):
    """
    Gestiona los usuarios desde el panel de administración de Django (/admin).
    Siempre tiene acceso total a ese panel.
    """
    TIPO = 'administrador'

    class Meta:
        verbose_name = 'Administrador'
        verbose_name_plural = 'Administradores'

    def save(self, *args, **kwargs):
        self.is_staff = True
        self.is_superuser = True
        super().save(*args, **kwargs)

    def crear_usuario(self, modelo, password, **datos) -> None:
        modelo.objects.create_user(password=password, **datos)

    def eliminar_usuario(self, usuario) -> None:
        usuario.delete()

    def modificar_usuario(self, usuario, **campos) -> None:
        for campo, valor in campos.items():
            setattr(usuario, campo, valor)
        usuario.save()


class EmpresaAcopio(Usuario):
    """
    Empresa compradora (Cooperativa, Alliance One, Massalin). Inicia sesión
    para ver su ocupación y publicar convocatorias.
    """
    TIPO = 'empresaacopio'

    nombre = models.CharField(max_length=150, unique=True, verbose_name='Nombre de la empresa')
    capacidad_diaria_kg = models.PositiveIntegerField(
        default=250_000,
        validators=[
            MinValueValidator(CAPACIDAD_DIARIA_MIN_KG),
            MaxValueValidator(CAPACIDAD_DIARIA_MAX_KG),
        ],
        verbose_name='Capacidad diaria (kg)',
        help_text='Entre 200.000 y 300.000 kg por día.',
    )
    # Excepción al diagrama acordada: capacidad configurable de cada franja de 2 h.
    capacidad_franja_kg = models.PositiveIntegerField(
        default=40_000,
        validators=[MinValueValidator(1)],
        verbose_name='Capacidad por franja (kg)',
        help_text='Kilos que se pueden recibir en cada franja de 2 horas. '
                  'Nunca se supera la capacidad diaria.',
    )
    activa = models.BooleanField(default=True, verbose_name='¿Activa para recepción?')
    corte_solicitado = models.CharField(
        max_length=10,
        choices=CorteTabaco.choices,
        blank=True,
        verbose_name='Corte solicitado',
        help_text='Corte de tabaco de la última convocatoria publicada.',
    )
    horario_preferente = models.CharField(
        max_length=6,
        choices=HorarioPreferente.choices,
        default=HorarioPreferente.MANANA,
        verbose_name='Horario preferente',
        help_text='Parte del día en la que la empresa tiene prioridad sobre sus rampas. '
                  'Fuera de ese horario solo puede usar rampas libres el mismo día de la entrega.',
    )

    class Meta:
        verbose_name = 'Empresa de Acopio'
        verbose_name_plural = 'Empresas de Acopio'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre

    def clean(self):
        super().clean()
        if self.capacidad_franja_kg and self.capacidad_diaria_kg \
                and self.capacidad_franja_kg > self.capacidad_diaria_kg:
            raise ValidationError({
                'capacidad_franja_kg': 'La capacidad por franja no puede superar la capacidad diaria.'
            })

    def verificar_cupo_disponible(self, fecha, kg) -> bool:
        return kg <= self.obtener_remanente(fecha)

    def calcular_kilos_ocupados(self, fecha) -> int:
        from turnos import services
        return services.kilos_ocupados(self, fecha)

    def obtener_remanente(self, fecha) -> int:
        return max(0, self.capacidad_diaria_kg - self.calcular_kilos_ocupados(fecha))

    def enviar_aviso(self, corte_tabaco, kilos, mensaje):
        """Publica una convocatoria y devuelve los avisos de WhatsApp a enviar."""
        from turnos import services
        return services.enviar_aviso(self, corte_tabaco, kilos, mensaje)


class Recorredor(Usuario):
    """Representante de una empresa: asigna, confirma y cancela turnos."""
    TIPO = 'recorredor'

    empresa_asignada = models.ForeignKey(
        EmpresaAcopio,
        on_delete=models.PROTECT,
        related_name='recorredores',
        verbose_name='Empresa asignada',
    )

    class Meta:
        verbose_name = 'Recorredor'
        verbose_name_plural = 'Recorredores'

    def asignar_turno(self, productor, kg, corte_tabaco, patente_vehiculo, fecha_entrega):
        from turnos import services
        return services.asignar_turno(self, productor, kg, corte_tabaco, patente_vehiculo, fecha_entrega)

    def confirmar_turno(self, turno) -> None:
        from turnos import services
        services.confirmar_turno(self, turno)

    def cancelar_turno(self, turno, mensaje=''):
        from turnos import services
        return services.cancelar_turno_recorredor(self, turno, mensaje)
