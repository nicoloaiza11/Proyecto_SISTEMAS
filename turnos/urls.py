from django.urls import path
from . import views

app_name = 'turnos'

urlpatterns = [
    # Productor: Solicitud, salida de finca, comprobante y tablero de decisiones
    path('turnos/solicitar/', views.solicitar_turno_view, name='solicitar_turno'),
    path('turnos/turno/<int:turno_id>/informar-salida/', views.informar_salida_view, name='informar_salida'),
    path('turnos/comprobante/<str:codigo>/', views.comprobante_view, name='comprobante'),
    path('turnos/rendimiento/', views.gerencia_productor_view, name='gerencia_productor'),

    # Empresa / Centro de Acopio: Mesa operativa, tablero gerencial y exportación
    path('acopio/panel/', views.panel_empresa_view, name='panel_empresa'),
    path('acopio/gerencia/', views.gerencia_empresa_view, name='gerencia_empresa'),
    path('acopio/turno/<int:turno_id>/cambiar-estado/', views.transicionar_estado_view, name='cambiar_estado'),
    path('acopio/exportar-csv/', views.exportar_turnos_csv_view, name='exportar_csv'),

    # API interna para consulta dinámica de cupos
    path('api/cupo-disponible/', views.api_consultar_cupo_view, name='api_consultar_cupo'),
]
