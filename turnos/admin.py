from django.contrib import admin

from .models import Comprobante, CupoProductor, OcupacionRampa, Rampa, RegistroOperador, Turno


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
        'codigo', 'productor', 'empresa', 'fecha_entrega', 'hora_inicio', 'hora_fin', 'duracion_min', 'rampa',
        'corte_tabaco', 'kilos_estimados', 'kilos_reales', 'estado',
    )
    list_filter = ('estado', 'corte_tabaco', 'empresa', 'rampa', 'fecha_entrega')
    search_fields = ('codigo', 'productor__cuit_dni', 'productor__username', 'patente_vehiculo')
    date_hierarchy = 'fecha_entrega'

    # Campos que definen la agenda: cambiarlos a mano dejaría la reserva de la
    # rampa (OcupacionRampa) desincronizada. Se modifican solo con los flujos
    # del sistema (confirmar, asignar, cancelar), que actualizan la ocupación.
    CAMPOS_AGENDA = ('estado', 'fecha_entrega', 'franja_horaria', 'hora_inicio', 'hora_fin',
                     'duracion_min', 'rampa')

    def get_readonly_fields(self, request, obj=None):
        campos = ('codigo',) + self.CAMPOS_AGENDA
        if obj is not None and obj.fecha_entrega:
            # Con horario asignado, la empresa y los kilos definen la rampa y la duración reservadas.
            campos += ('empresa', 'kilos_estimados')
        return campos

    def get_deleted_objects(self, objs, request):
        """
        Borrar un turno borra también sus tramos de OcupacionRampa (se libera
        la rampa). Esos tramos no se pueden borrar sueltos, pero no deben
        impedir borrar el turno completo.
        """
        borrados, conteo, permisos, protegidos = super().get_deleted_objects(objs, request)
        permisos = {p for p in permisos if p != OcupacionRampa._meta.verbose_name}
        return borrados, conteo, permisos, protegidos


@admin.register(OcupacionRampa)
class OcupacionRampaAdmin(admin.ModelAdmin):
    """Tramos de 30 min reservados (solo lectura: los maneja el sistema)."""
    list_display = ('rampa', 'fecha', 'inicio', 'turno')
    list_filter = ('rampa', 'fecha')
    search_fields = ('turno__codigo',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Borrar un tramo suelto liberaría la rampa sin cancelar el turno.
        return False


@admin.register(RegistroOperador)
class RegistroOperadorAdmin(admin.ModelAdmin):
    list_display = ('turno', 'operador', 'peso_acordado', 'peso_real', 'corte_tabaco')
    search_fields = ('turno__codigo',)


@admin.register(Comprobante)
class ComprobanteAdmin(admin.ModelAdmin):
    list_display = ('codigo_turno', 'empresa', 'fecha_entrega', 'corte_tabaco', 'peso_real', 'precio')
    list_filter = ('empresa', 'corte_tabaco')
    search_fields = ('codigo_turno',)
