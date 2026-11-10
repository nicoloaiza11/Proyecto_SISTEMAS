from django.contrib.auth.models import AbstractUser
from django.db import models


class CustomUser(AbstractUser):
    """
    Modelo de usuario con roles diferenciados para el sistema SOLTAT:
    - 'productor': Productor tabacalero que solicita turnos con su DNI/CUIT.
    - 'empresa': Operador o encargado del centro de acopio.
    """
    ROL_PRODUCTOR = 'productor'
    ROL_EMPRESA = 'empresa'

    ROLES_CHOICES = [
        (ROL_PRODUCTOR, 'Productor Tabacalero'),
        (ROL_EMPRESA, 'Empresa de Acopio'),
    ]

    cuit_dni = models.CharField(
        max_length=20,
        unique=True,
        verbose_name='DNI / CUIT',
        help_text='Identificador tributario o documento del productor o representante'
    )
    rol = models.CharField(
        max_length=20,
        choices=ROLES_CHOICES,
        default=ROL_PRODUCTOR,
        verbose_name='Rol en la plataforma'
    )
    razon_social = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name='Nombre / Razón Social',
        help_text='Nombre del productor o de la empresa tabacalera'
    )
    telefono = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        verbose_name='Teléfono de contacto'
    )
    empresa_asociada = models.ForeignKey(
        'turnos.EmpresaAcopio',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='usuarios_empresa',
        verbose_name='Empresa de Acopio Asociada',
        help_text='Aplica exclusivamente para usuarios con rol de empresa de acopio'
    )

    class Meta:
        verbose_name = 'Usuario SOLTAT'
        verbose_name_plural = 'Usuarios SOLTAT'
        ordering = ['cuit_dni']

    def __str__(self):
        nombre_display = self.razon_social or self.get_full_name() or self.username
        return f"{nombre_display} ({self.get_rol_display()} - {self.cuit_dni})"

    @property
    def es_productor(self):
        return self.rol == self.ROL_PRODUCTOR

    @property
    def es_empresa(self):
        return self.rol == self.ROL_EMPRESA

