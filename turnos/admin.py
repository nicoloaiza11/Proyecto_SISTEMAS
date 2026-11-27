from django.contrib import admin

from .models import Comprobante, CupoProductor, Rampa, RegistroOperador, Turno


@admin.register(CupoProductor)
class CupoProductorAdmin(admin.ModelAdmin):
    list_display = ('productor', 'empresa', 'recorredor', 'cupo_maximo')
    list_filter = ('recorredor__empresa_asignada',)
    search_fields = ('productor__cuit_dni', 'productor__first_name', 'productor__last_name')
    autocomplete_fields = ('productor', 'recorredor')


@admin.register(Rampa)
class RampaAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'empresas_habilitadas', 'activa')
    list_editable = ('activa',)
    filter_horizontal = ('empresas',)

    @admin.display(description='Empresas habilitadas')
    def empresas_habilitadas(self, obj):
        return ', '.join(e.nombre for e in obj.empresas.all()) or '-'


@admin.register(Turno)
class TurnoAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'productor', 'empresa', 'fecha_entrega', 'franja_horaria', 'rampa',
        'corte_tabaco', 'kilos_estimados', 'kilos_reales', 'estado',
    )
    list_filter = ('estado', 'corte_tabaco', 'empresa', 'rampa', 'fecha_entrega')
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
