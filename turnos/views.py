import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
from django.db.models import Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST

from accounts.views import productor_required, empresa_required
from .models import Turno, EmpresaAcopio, CLASES_TABACO, PRECIOS_CLASE_USD
from .forms import SolicitudTurnoForm
from . import services
from . import analytics


@productor_required
def solicitar_turno_view(request):
    """
    Vista del formulario para que el productor tabacalero reserve un turno de entrega.
    Verifica de forma estricta los cupos diarios mediante services.validar_y_crear_turno.
    """
    cupo_error = None

    if request.method == 'POST':
        form = SolicitudTurnoForm(request.POST)
        if form.is_valid():
            empresa = form.cleaned_data['empresa']
            kilos_estimados = form.cleaned_data['kilos_estimados']
            patente_vehiculo = form.cleaned_data['patente_vehiculo']
            fecha_entrega = form.cleaned_data['fecha_entrega']
            franja_horaria = form.cleaned_data['franja_horaria']
            observaciones = form.cleaned_data.get('observaciones', '')

            try:
                turno = services.validar_y_crear_turno(
                    productor=request.user,
                    empresa=empresa,
                    kilos_estimados=kilos_estimados,
                    patente_vehiculo=patente_vehiculo,
                    fecha_entrega=fecha_entrega,
                    franja_horaria=franja_horaria,
                    observaciones=observaciones
                )
                messages.success(
                    request,
                    f"¡Turno registrado con éxito! Código asignado: {turno.codigo}"
                )
                return redirect('turnos:comprobante', codigo=turno.codigo)

            except services.CupoExcedidoException as ex:
                cupo_error = {
                    'mensaje': str(ex),
                    'remanente': ex.remanente_disponible,
                    'solicitados': ex.kilos_solicitados,
                    'capacidad': ex.capacidad_maxima,
                    'ocupados': ex.kilos_ocupados,
                }
                messages.error(
                    request,
                    f"No se pudo registrar el turno: El cupo diario está superado. Remanente disponible: {ex.remanente_disponible:,} kg."
                )
    else:
        form = SolicitudTurnoForm(initial={'fecha_entrega': date.today()})

    turnos_recientes = Turno.objects.filter(
        productor=request.user
    ).select_related('empresa').order_by('-fecha_entrega', '-creado_el')[:6]

    context = {
        'form': form,
        'cupo_error': cupo_error,
        'turnos_recientes': turnos_recientes,
        'hoy': date.today().strftime('%Y-%m-%d'),
    }
    return render(request, 'turnos/solicitar_turno.html', context)


@productor_required
@require_POST
def informar_salida_view(request, turno_id):
    """
    El productor registra que el camión salió de finca rumbo a la planta,
    iniciando el cronómetro de recorrido (hora_salida_origen).
    """
    turno = get_object_or_404(Turno, id=turno_id)
    try:
        services.registrar_salida_origen(turno, request.user)
        messages.success(
            request,
            f"Salida de finca registrada para el turno [{turno.codigo}]. Cronómetro de recorrido en curso."
        )
    except Exception as exc:
        messages.error(request, str(exc))

    next_url = request.POST.get('next')
    if next_url:
        return redirect(next_url)
    return redirect('turnos:solicitar_turno')


@login_required
def comprobante_view(request, codigo):
    """
    Muestra la confirmación, trazabilidad de tiempos y liquidación del turno.
    """
    if request.user.es_productor and not request.user.is_superuser:
        turno = get_object_or_404(Turno, codigo=codigo, productor=request.user)
    else:
        turno = get_object_or_404(Turno, codigo=codigo)

    context = {
        'turno': turno,
    }
    return render(request, 'turnos/comprobante.html', context)


@empresa_required
def panel_empresa_view(request):
    """
    Dashboard operativo del centro de acopio con semáforo de capacidad, selector de fecha,
    cierre de báscula con peso/clase y métricas en vivo de la jornada.
    """
    empresa_usuario = request.user.empresa_asociada
    empresas_disponibles = EmpresaAcopio.objects.filter(activa=True)

    empresa_id_param = request.GET.get('empresa')
    if empresa_usuario:
        empresa_actual = empresa_usuario
    elif empresa_id_param:
        empresa_actual = get_object_or_404(EmpresaAcopio, id=empresa_id_param)
    else:
        empresa_actual = empresas_disponibles.first()

    fecha_param = request.GET.get('fecha')
    if fecha_param:
        try:
            fecha_seleccionada = datetime.strptime(fecha_param, '%Y-%m-%d').date()
        except ValueError:
            fecha_seleccionada = date.today()
    else:
        fecha_seleccionada = date.today()

    metricas_cupo = None
    turnos_del_dia = Turno.objects.none()

    if empresa_actual:
        metricas_cupo = services.consultar_disponibilidad_cupo(empresa_actual, fecha_seleccionada)
        turnos_del_dia = Turno.objects.filter(
            empresa=empresa_actual,
            fecha_entrega=fecha_seleccionada
        ).select_related('productor').order_by('franja_horaria', 'creado_el')

    activos_qs = turnos_del_dia.exclude(estado=Turno.ESTADO_CANCELADO)
    total_turnos = activos_qs.count()

    confirmados_qs = turnos_del_dia.filter(estado=Turno.ESTADO_CONFIRMADO)
    en_playa_qs = turnos_del_dia.filter(estado=Turno.ESTADO_EN_PLAYA)
    descargados_qs = turnos_del_dia.filter(estado=Turno.ESTADO_DESCARGADO)

    confirmados_count = confirmados_qs.count()
    en_playa_count = en_playa_qs.count()
    descargados_count = descargados_qs.count()

    kilos_confirmados = confirmados_qs.aggregate(t=Sum('kilos_estimados'))['t'] or 0
    kilos_en_playa = en_playa_qs.aggregate(t=Sum('kilos_estimados'))['t'] or 0
    kilos_descargados = sum(
        (t.kilos_reales if t.kilos_reales is not None else t.kilos_estimados)
        for t in descargados_qs
    )
    monto_jornada_usd = sum((t.importe_usd for t in descargados_qs), Decimal('0'))

    esperas_hoy = [t.minutos_espera for t in descargados_qs if t.minutos_espera is not None]
    espera_media_hoy = round(sum(esperas_hoy) / len(esperas_hoy)) if esperas_hoy else None

    fecha_anterior = (fecha_seleccionada - timedelta(days=1)).strftime('%Y-%m-%d')
    fecha_siguiente = (fecha_seleccionada + timedelta(days=1)).strftime('%Y-%m-%d')
    hoy_iso = date.today().strftime('%Y-%m-%d')

    context = {
        'empresas_disponibles': empresas_disponibles,
        'empresa_actual': empresa_actual,
        'fecha_seleccionada': fecha_seleccionada.strftime('%Y-%m-%d'),
        'fecha_display': fecha_seleccionada.strftime('%d/%m/%Y'),
        'fecha_anterior': fecha_anterior,
        'fecha_siguiente': fecha_siguiente,
        'hoy_iso': hoy_iso,
        'es_hoy': fecha_seleccionada == date.today(),
        'metricas_cupo': metricas_cupo,
        'turnos': turnos_del_dia,
        'total_turnos': total_turnos,
        'confirmados_count': confirmados_count,
        'en_playa_count': en_playa_count,
        'descargados_count': descargados_count,
        'kilos_confirmados': kilos_confirmados,
        'kilos_en_playa': kilos_en_playa,
        'kilos_descargados': kilos_descargados,
        'monto_jornada_usd': monto_jornada_usd,
        'espera_media_hoy': espera_media_hoy,
        'clases_tabaco': CLASES_TABACO,
        'precios_clase': {k: str(v) for k, v in PRECIOS_CLASE_USD.items()},
    }
    return render(request, 'acopio/panel_empresa.html', context)


@empresa_required
@require_POST
def transicionar_estado_view(request, turno_id):
    """
    Endpoint para cambiar el estado operativo de un turno:
    'Confirmado' -> 'En playa' -> 'Descargado' (registrando peso neto y clase comercial).
    """
    nuevo_estado = request.POST.get('nuevo_estado')
    fecha_redireccion = request.POST.get('fecha', '')
    empresa_redireccion = request.POST.get('empresa_id', '')
    kilos_reales = request.POST.get('kilos_reales')
    clase_tabaco = request.POST.get('clase_tabaco')

    try:
        turno = services.transicionar_estado_turno(
            turno_id=turno_id,
            nuevo_estado=nuevo_estado,
            kilos_reales=kilos_reales,
            clase_tabaco=clase_tabaco,
        )
        if nuevo_estado == Turno.ESTADO_DESCARGADO:
            messages.success(
                request,
                f"Descarga cerrada [{turno.codigo}]: {turno.kilos_reales:,} kg netos · "
                f"Clase {turno.clase_tabaco} (USD {turno.importe_usd:,.2f})."
            )
        else:
            messages.success(
                request,
                f"Turno [{turno.codigo}] actualizado exitosamente al estado: {turno.get_estado_display()}."
            )
    except Exception as e:
        messages.error(request, f"Error al actualizar estado: {str(e)}")

    url_destino = f"/acopio/panel/?fecha={fecha_redireccion}&empresa={empresa_redireccion}"
    return redirect(url_destino)


@empresa_required
def gerencia_empresa_view(request):
    """
    Sistema de Apoyo a la Toma de Decisiones (SAD) dirigido al Gerente del Centro de Acopio.
    Analiza ganancias/liquidaciones, pérdidas de capacidad, tiempos de recorrido/espera y recomendaciones.
    """
    empresa_usuario = request.user.empresa_asociada
    empresas_disponibles = EmpresaAcopio.objects.filter(activa=True)

    empresa_id_param = request.GET.get('empresa')
    if empresa_usuario:
        empresa_actual = empresa_usuario
    elif empresa_id_param:
        empresa_actual = get_object_or_404(EmpresaAcopio, id=empresa_id_param)
    else:
        empresa_actual = empresas_disponibles.first()

    dias, desde, hasta = analytics.resolver_periodo(request.GET.get('periodo'), por_defecto=30)
    datos = analytics.analitica_empresa(empresa_actual, dias, desde, hasta) if empresa_actual else {}

    context = {
        'empresas_disponibles': empresas_disponibles,
        'empresa_actual': empresa_actual,
        'periodo_actual': dias,
        'periodos_disponibles': analytics.PERIODOS_VALIDOS,
        'desde': desde,
        'hasta': hasta,
        **datos,
    }
    return render(request, 'acopio/gerencia_empresa.html', context)


@productor_required
def gerencia_productor_view(request):
    """
    Sistema de Apoyo a la Toma de Decisiones (SAD) dirigido al Productor / Gerente Agropecuario.
    Analiza ingresos liquidados, valor no percibido (merma y calidad), tiempos de viaje/espera
    frente al promedio de planta y recomendaciones operativas.
    """
    dias, desde, hasta = analytics.resolver_periodo(request.GET.get('periodo'), por_defecto=30)
    datos = analytics.analitica_productor(request.user, dias, desde, hasta)

    context = {
        'periodo_actual': dias,
        'periodos_disponibles': analytics.PERIODOS_VALIDOS,
        'desde': desde,
        'hasta': hasta,
        **datos,
    }
    return render(request, 'turnos/gerencia_productor.html', context)


def api_consultar_cupo_view(request):
    """
    API JSON para comprobación en tiempo real de cupos desde el formulario del productor.
    """
    empresa_id = request.GET.get('empresa_id')
    fecha_str = request.GET.get('fecha')

    if not empresa_id or not fecha_str:
        return JsonResponse({'error': 'Faltan parámetros empresa_id o fecha'}, status=400)

    try:
        empresa = EmpresaAcopio.objects.get(id=empresa_id, activa=True)
        fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
    except (EmpresaAcopio.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Empresa o fecha inválida'}, status=400)

    datos = services.consultar_disponibilidad_cupo(empresa, fecha)
    return JsonResponse(datos)


@empresa_required
def exportar_turnos_csv_view(request):
    """
    Exporta la grilla de turnos asignados del día seleccionado en formato CSV compatible con Excel.
    """
    empresa_usuario = request.user.empresa_asociada
    empresa_id_param = request.GET.get('empresa')
    if empresa_usuario:
        empresa_actual = empresa_usuario
    elif empresa_id_param:
        empresa_actual = get_object_or_404(EmpresaAcopio, id=empresa_id_param)
    else:
        empresa_actual = EmpresaAcopio.objects.filter(activa=True).first()

    fecha_param = request.GET.get('fecha')
    try:
        fecha_seleccionada = datetime.strptime(fecha_param, '%Y-%m-%d').date() if fecha_param else date.today()
    except ValueError:
        fecha_seleccionada = date.today()

    turnos = Turno.objects.filter(
        empresa=empresa_actual,
        fecha_entrega=fecha_seleccionada
    ).select_related('productor').order_by('franja_horaria', 'creado_el')

    nombre_archivo = f"planilla_acopio_{fecha_seleccionada.strftime('%Y%m%d')}.csv"
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{nombre_archivo}"'
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=';')
    writer.writerow([
        'Código de Turno',
        'Productor / Razón Social',
        'DNI / CUIT',
        'Kilos Declarados (Kg)',
        'Kilos Reales Báscula (Kg)',
        'Clase Tabaco',
        'Precio USD/kg',
        'Importe Liquidado (USD)',
        'Fecha de Entrega',
        'Franja Horaria',
        'Patente Vehículo',
        'Viaje Finca->Garita (min)',
        'Espera en Planta (min)',
        'Recorrido Total (min)',
        'Estado Operativo',
        'Observaciones',
    ])

    for t in turnos:
        writer.writerow([
            t.codigo,
            t.productor.razon_social or t.productor.get_full_name() or t.productor.username,
            t.productor.cuit_dni,
            t.kilos_estimados,
            t.kilos_reales if t.kilos_reales is not None else '',
            t.clase_tabaco or '',
            t.precio_kg if t.precio_kg is not None else '',
            t.importe_usd if t.kilos_reales else '',
            t.fecha_entrega.strftime('%d/%m/%Y'),
            t.franja_horaria,
            t.patente_vehiculo,
            t.minutos_viaje if t.minutos_viaje is not None else '',
            t.minutos_espera if t.minutos_espera is not None else '',
            t.minutos_ciclo if t.minutos_ciclo is not None else '',
            t.get_estado_display(),
            t.observaciones or '',
        ])

    return response
