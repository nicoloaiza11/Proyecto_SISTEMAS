from django.urls import path

from . import views

app_name = 'turnos'

urlpatterns = [
    # Productor
    path('turnos/mis-turnos/', views.mis_turnos_productor_view, name='mis_turnos'),
    path('turnos/registros-historicos/', views.registros_historicos_view, name='registros_historicos'),
    path('turnos/solicitar/', views.solicitar_turno_view, name='solicitar_turno'),
    path('turnos/turno/<int:turno_id>/cancelar/', views.cancelar_turno_productor_view, name='cancelar_turno'),
    path('turnos/rendimiento/', views.rendimiento_productor_view, name='rendimiento'),

    # Comprobante (según permisos de cada usuario)
    path('turnos/comprobante/<str:codigo>/', views.comprobante_view, name='comprobante'),

    # Recorredor
    path('recorredor/', views.panel_recorredor_view, name='panel_recorredor'),
    path('recorredor/asignar/', views.asignar_turno_view, name='asignar_turno'),
    path('recorredor/turno/<int:turno_id>/confirmar/', views.confirmar_turno_view, name='confirmar_turno'),
    path('recorredor/turno/<int:turno_id>/cancelar/', views.cancelar_turno_recorredor_view, name='cancelar_turno_recorredor'),

    # Operador de acopio
    path('operador/', views.panel_operador_view, name='panel_operador'),
    path('operador/turno/<int:turno_id>/ingreso/', views.registrar_ingreso_view, name='registrar_ingreso'),
    path('operador/turno/<int:turno_id>/pesaje/', views.registrar_pesaje_view, name='registrar_pesaje'),

    # Empresa de acopio
    path('acopio/panel/', views.panel_empresa_view, name='panel_empresa'),

    # Avisos de WhatsApp y API
    path('avisos/', views.avisos_view, name='avisos'),
    path('api/disponibilidad/', views.api_disponibilidad_view, name='api_disponibilidad'),
]
