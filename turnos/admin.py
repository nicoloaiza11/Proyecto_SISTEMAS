from django.contrib import admin
from .models import EmpresaAcopio, Turno


@admin.register(EmpresaAcopio)
class EmpresaAcopioAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'cuit', 'capacidad_diaria_kg', 'activa')
    list_filter = ('activa',)
    search_fields = ('nombre', 'cuit')


@admin.register(Turno)
class TurnoAdmin(admin.ModelAdmin):
    list_display = (
        'codigo',
        'productor',
        'empresa',
        'kilos_estimados',
        'kilos_reales',
        'clase_tabaco',
        'fecha_entrega',
        'franja_horaria',
        'patente_vehiculo',
        'estado',
    )
    list_filter = ('estado', 'clase_tabaco', 'empresa', 'fecha_entrega')
    search_fields = ('codigo', 'productor__username', 'productor__cuit_dni', 'productor__razon_social', 'patente_vehiculo')
    date_hierarchy = 'fecha_entrega'
    readonly_fields = ('codigo', 'creado_el', 'actualizado_el')
