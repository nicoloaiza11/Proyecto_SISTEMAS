from django.contrib import admin

from .models import Comprobante, CupoProductor, RegistroOperador, Turno


@admin.register(CupoProductor)
class CupoProductorAdmin(admin.ModelAdmin):
    list_display = ('productor', 'empresa', 'recorredor', 'cupo_maximo')
    list_filter = ('recorredor__empresa_asignada',)
    search_fields = ('productor__cuit_dni', 'productor__first_name', 'productor__last_name')
    autocomplete_fields = ('productor', 'recorredor')


@admin.register(Turno)
class TurnoAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'productor', 'empresa', 'fecha_entrega', 'franja_horaria',
        'corte_tabaco', 'kilos_estimados', 'kilos_reales', 'estado',
    )
    list_filter = ('estado', 'corte_tabaco', 'empresa', 'fecha_entrega')
    search_fields = ('codigo', 'productor__cuit_dni', 'productor__username', 'patente_vehiculo')
    date_hierarchy = 'fecha_entrega'
    readonly_fields = ('codigo',)


@admin.register(RegistroOperador)
class RegistroOperadorAdmin(admin.ModelAdmin):
    list_display = ('turno', 'operador', 'peso_acordado', 'peso_real', 'corte_tabaco')
    search_fields = ('turno__codigo',)


@admin.register(Comprobante)
class ComprobanteAdmin(admin.ModelAdmin):
    list_display = ('codigo_turno', 'empresa', 'fecha_entrega', 'corte_tabaco', 'peso_real', 'precio')
    list_filter = ('empresa', 'corte_tabaco')
    search_fields = ('codigo_turno',)
