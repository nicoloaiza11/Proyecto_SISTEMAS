"""
Panel /admin: el Administrador crea, modifica y elimina usuarios de cada tipo.
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .forms import crear_formularios_usuario
from .models import Administrador, EmpresaAcopio, OperadorAcopio, Productor, Recorredor

DATOS_BASICOS = ('Datos SOLTAT', {'fields': ('cuit_dni', 'telefono')})


def registrar_tipo_usuario(modelo, campos_extra=(), list_extra=(), list_filter=()):
    alta, edicion = crear_formularios_usuario(modelo, campos_extra)
    fieldsets_extra = (('Datos específicos', {'fields': campos_extra}),) if campos_extra else ()

    class Admin(UserAdmin):
        add_form = alta
        form = edicion
        list_display = ('username', 'cuit_dni', 'first_name', 'last_name', 'telefono') + tuple(list_extra) + ('is_active',)
        search_fields = ('username', 'cuit_dni', 'first_name', 'last_name', 'email')
        ordering = ('cuit_dni',)
        fieldsets = (
            (None, {'fields': ('username', 'password')}),
            ('Datos personales', {'fields': ('first_name', 'last_name', 'email')}),
            DATOS_BASICOS,
        ) + fieldsets_extra + (
            ('Estado', {'fields': ('is_active', 'last_login', 'date_joined')}),
        )
        add_fieldsets = (
            (None, {
                'classes': ('wide',),
                'fields': ('username', 'cuit_dni', 'first_name', 'last_name', 'email', 'telefono')
                          + tuple(campos_extra) + ('password1', 'password2'),
            }),
        )

    Admin.list_filter = tuple(list_filter) + ('is_active',)
    admin.site.register(modelo, Admin)


registrar_tipo_usuario(Productor)
registrar_tipo_usuario(OperadorAcopio)
registrar_tipo_usuario(Administrador)
registrar_tipo_usuario(
    Recorredor,
    campos_extra=('empresa_asignada',),
    list_extra=('empresa_asignada',),
    list_filter=('empresa_asignada',),
)
registrar_tipo_usuario(
    EmpresaAcopio,
    campos_extra=('nombre', 'capacidad_diaria_kg', 'velocidad_descarga_kg_hora', 'capacidad_franja_kg',
                  'horario_preferente', 'activa',
                  'corte_solicitado'),
    list_extra=('nombre', 'capacidad_diaria_kg', 'velocidad_descarga_kg_hora', 'horario_preferente', 'activa'),
)
