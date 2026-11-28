from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import (
    empresa_required, operador_required, productor_required, recorredor_required,
)
from accounts.models import EmpresaAcopio
from . import graficos, services
from . import weather as weather_service
from .choices import CorteTabaco, EstadoTurno
from .forms import AsignarTurnoForm, CancelacionForm, ConvocatoriaForm, SolicitudTurnoForm
from .models import ORDEN_AGENDA, ORDEN_HORARIO, ORDEN_RECIENTES, Turno
from .whatsapp import armar_aviso

ACTIVOS = [EstadoTurno.PENDIENTE, EstadoTurno.CONFIRMADO, EstadoTurno.EN_PLAYA]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _fecha_param(request):
    try:
        return datetime.strptime(request.GET.get('fecha', ''), '%Y-%m-%d').date()
    except ValueError:
        return timezone.localdate()


def _contexto_fecha(fecha):
    hoy = timezone.localdate()
    return {
        'fecha': fecha,
        'fecha_iso': fecha.isoformat(),
        'fecha_anterior': (fecha - timedelta(days=1)).isoformat(),
        'fecha_siguiente': (fecha + timedelta(days=1)).isoformat(),
        'hoy_iso': hoy.isoformat(),
        'es_hoy': fecha == hoy,
    }


def _mostrar_avisos(request, avisos, titulo, volver):
    """Guarda los avisos de WhatsApp en la sesión y lleva a la pantalla para enviarlos."""
    if not avisos:
        return redirect(volver)
    request.session['avisos_whatsapp'] = {'titulo': titulo, 'avisos': avisos, 'volver': volver}
    return redirect('turnos:avisos')


def _volver(request, por_defecto):
    destino = request.POST.get('next', '')
    return destino if destino.startswith('/') else por_defecto


# ---------------------------------------------------------------------------
# Productor
# ---------------------------------------------------------------------------
@productor_required
def mis_turnos_productor_view(request):
    services.cancelar_turnos_vencidos()
    productor = request.perfil
    turnos = list(productor.turnos.filter(estado__in=ACTIVOS).select_related('empresa', 'rampa'))

    # --- Lazy Refresh climático: actualiza en background, sin bloquear la vista ---
    for t in turnos:
        try:
            weather_service.refrescar_si_necesario(t)
        except Exception:
            pass  # Nunca debe bloquear la carga de la tabla

    # Adjuntamos el dict de display climático a cada turno para el template
    for t in turnos:
        t.clima_display = weather_service.clima_display(t)

    return render(request, 'turnos/mis_turnos.html', {
        'turnos': turnos,
        'cumplimiento': services.evaluar_cumplimiento(productor),
        'cupos': services.resumen_cupos(productor),
    })


@productor_required
def registros_historicos_view(request):
    services.cancelar_turnos_vencidos()
    productor = request.perfil
    base = productor.turnos.select_related('empresa', 'rampa')
    estado = request.GET.get('estado', '')
    turnos_qs = (base.filter(estado=estado) if estado in EstadoTurno.values else base).order_by(*ORDEN_RECIENTES)
    paginator = Paginator(turnos_qs, 10)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'turnos/registros_historicos.html', {
        'turnos': page_obj,
        'page_obj': page_obj,
        'total_turnos': base.count(),
        'turnos_activos': base.filter(estado__in=ACTIVOS).count(),
        'turnos_descargados': base.filter(estado=EstadoTurno.DESCARGADO).count(),
        'estado_filtro': estado,
        'estados_choices': EstadoTurno.choices,
    })


@productor_required
def solicitar_turno_view(request):
    productor = request.perfil
    empresas = services.empresas_con_cupo(productor)
    cumplimiento = services.evaluar_cumplimiento(productor)
    form = SolicitudTurnoForm(request.POST or None, empresas=empresas)

    if request.method == 'POST' and form.is_valid() and not cumplimiento['bloqueado']:
        datos = form.cleaned_data
        try:
            turno = productor.solicitar_turno(
                datos['empresa'], datos['kilos_estimados'], datos['corte_tabaco'],
                datos['patente_vehiculo'],
            )
        except services.TurnoError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request,
                f'Solicitud de turno {turno.codigo} enviada con éxito. '
                f'La fecha y el horario (estimado en {turno.duracion_estimada_display}) serán asignados automáticamente por el sistema cuando la empresa confirme la solicitud.'
            )
            return redirect('turnos:comprobante', codigo=turno.codigo)

    return render(request, 'turnos/solicitar_turno.html', {
        'form': form,
        'velocidades': {str(e.pk): e.velocidad_descarga_kg_hora for e in empresas},
        'cumplimiento': cumplimiento,
        'cupos': services.resumen_cupos(productor),
        'sin_empresas': not empresas.exists(),
    })


@productor_required
def cancelar_turno_productor_view(request, turno_id):
    productor = request.perfil
    turno = get_object_or_404(Turno, id=turno_id, productor=productor)
    form = CancelacionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            if turno.estado == EstadoTurno.CONFIRMADO:
                avisos = productor.cancelar_turno_confirmado(turno, form.cleaned_data['mensaje'])
            else:
                avisos = productor.cancelar_turno(turno)
        except services.TurnoError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, f'Turno {turno.codigo} cancelado.')
            return _mostrar_avisos(request, avisos, 'Avisar a la empresa de la cancelación',
                                   reverse('turnos:mis_turnos'))
    return render(request, 'turnos/cancelar_turno.html', {
        'turno': turno,
        'form': form,
        'requiere_motivo': turno.estado == EstadoTurno.CONFIRMADO,
        'volver': reverse('turnos:mis_turnos'),
    })


@productor_required
def rendimiento_productor_view(request):
    contexto = request.perfil.consultar_rendimiento()
    contexto['graficos'] = graficos.graficos_productor(request.perfil)
    return render(request, 'turnos/rendimiento.html', contexto)


# ---------------------------------------------------------------------------
# Comprobante (todos los tipos de usuario, según permisos)
# ---------------------------------------------------------------------------
@login_required
def comprobante_view(request, codigo):
    turno = get_object_or_404(
        Turno.objects.select_related('empresa', 'productor', 'recorredor', 'rampa'), codigo=codigo
    )
    user = request.user
    permitido = (
        user.es_administrador
        or user.es_operador
        or (user.es_productor and turno.productor_id == user.pk)
        or (user.es_empresa and turno.empresa_id == user.pk)
        or (user.es_recorredor and turno.empresa_id == user.perfil.empresa_asignada_id)
    )
    if not permitido:
        raise Http404
    return render(request, 'turnos/comprobante.html', {
        'turno': turno,
        'comprobante': getattr(turno, 'comprobante', None),
        'registro': getattr(turno, 'registro_operador', None),
    })


# ---------------------------------------------------------------------------
# Recorredor
# ---------------------------------------------------------------------------
@recorredor_required
def panel_recorredor_view(request):
    services.cancelar_turnos_vencidos()
    recorredor = request.perfil
    empresa = recorredor.empresa_asignada
    hoy = timezone.localdate()
    turnos = Turno.objects.filter(empresa=empresa).select_related('productor', 'rampa')
    pendientes = list(turnos.filter(estado=EstadoTurno.PENDIENTE).order_by(*ORDEN_AGENDA))
    for t in pendientes:
        t.cumplimiento = services.evaluar_cumplimiento(t.productor)
    return render(request, 'turnos/recorredor_panel.html', {
        'empresa': empresa,
        'pendientes': pendientes,
        'confirmados': turnos.filter(estado=EstadoTurno.CONFIRMADO, fecha_entrega__gte=hoy)
                             .order_by(*ORDEN_AGENDA),
        'cupos': recorredor.cupos.select_related('productor'),
    })


@recorredor_required
def asignar_turno_view(request):
    recorredor = request.perfil
    empresa = recorredor.empresa_asignada
    form = AsignarTurnoForm(request.POST or None, empresa=empresa,
                            initial={'fecha_entrega': timezone.localdate()})
    if request.method == 'POST' and form.is_valid():
        datos = form.cleaned_data
        try:
            turno = recorredor.asignar_turno(
                datos['productor'], datos['kilos_estimados'], datos['corte_tabaco'],
                datos['patente_vehiculo'], datos['fecha_entrega'],
            )
        except services.TurnoError as error:
            form.add_error(None, str(error))
        else:
            # --- Consulta inicial de clima (no bloquea si falla) ---
            try:
                weather_service.refrescar_si_necesario(turno)
            except Exception:
                pass
            messages.success(
                request,
                f'Turno {turno.codigo} asignado y confirmado: {turno.fecha_entrega:%d/%m/%Y}, '
                f'{turno.horario_display} hs ({turno.duracion_display}), {turno.rampa}.'
            )
            aviso = armar_aviso(
                turno.productor,
                f'SOLTAT · {empresa.nombre} le asignó el turno {turno.codigo} para el '
                f'{turno.fecha_entrega:%d/%m/%Y} de {turno.horario_display} hs, {turno.rampa} '
                f'({services.kg(turno.kilos_estimados)}, corte {turno.get_corte_tabaco_display()}).'
            )
            return _mostrar_avisos(request, [aviso], 'Avisar al productor del turno asignado',
                                   reverse('turnos:panel_recorredor'))
    return render(request, 'turnos/asignar_turno.html', {'form': form, 'empresa': empresa})


@recorredor_required
@require_POST
def confirmar_turno_view(request, turno_id):
    recorredor = request.perfil
    turno = get_object_or_404(Turno, id=turno_id)
    try:
        recorredor.confirmar_turno(turno)
    except services.TurnoError as error:
        messages.error(request, str(error))
        return redirect('turnos:panel_recorredor')

    # --- Consulta inicial de clima (no bloquea si falla) ---
    try:
        weather_service.refrescar_si_necesario(turno)
    except Exception:
        pass

    messages.success(
        request,
        f'Turno {turno.codigo} confirmado y asignado por sistema para el '
        f'{turno.fecha_entrega:%d/%m/%Y} de {turno.horario_display} hs ({turno.duracion_display}), {turno.rampa}.'
    )
    aviso = armar_aviso(
        turno.productor,
        f'SOLTAT · {turno.empresa.nombre} confirmó su turno {turno.codigo} del '
        f'{turno.fecha_entrega:%d/%m/%Y} de {turno.horario_display} hs, {turno.rampa}.'
    )
    return _mostrar_avisos(request, [aviso], 'Avisar al productor de la confirmación',
                           reverse('turnos:panel_recorredor'))


@recorredor_required
@require_POST
def cancelar_turno_recorredor_view(request, turno_id):
    recorredor = request.perfil
    turno = get_object_or_404(Turno, id=turno_id)
    try:
        avisos = recorredor.cancelar_turno(turno, request.POST.get('mensaje', ''))
    except services.TurnoError as error:
        messages.error(request, str(error))
        return redirect('turnos:panel_recorredor')
    messages.success(request, f'Turno {turno.codigo} cancelado.')
    return _mostrar_avisos(request, avisos, 'Avisar al productor de la cancelación',
                           reverse('turnos:panel_recorredor'))


# ---------------------------------------------------------------------------
# Operador de acopio (garita y báscula, todas las empresas)
# ---------------------------------------------------------------------------
@operador_required
def panel_operador_view(request):
    services.cancelar_turnos_vencidos()
    fecha = _fecha_param(request)
    turnos = (
        Turno.objects.filter(fecha_entrega=fecha)
        .exclude(estado__in=[EstadoTurno.PENDIENTE, EstadoTurno.CANCELADO])
        .select_related('empresa', 'productor', 'rampa')
        .order_by(*ORDEN_HORARIO, 'rampa__numero', 'id')
    )
    empresa_id = request.GET.get('empresa', '')
    if empresa_id.isdigit():
        turnos = turnos.filter(empresa_id=int(empresa_id))
    return render(request, 'turnos/operador_panel.html', {
        **_contexto_fecha(fecha),
        'turnos': turnos,
        'empresas': EmpresaAcopio.objects.filter(activa=True),
        'empresa_filtro': empresa_id,
        'cortes': CorteTabaco.choices,
        'en_playa': turnos.filter(estado=EstadoTurno.EN_PLAYA).count(),
        'por_ingresar': turnos.filter(estado=EstadoTurno.CONFIRMADO).count(),
        'descargados': turnos.filter(estado=EstadoTurno.DESCARGADO).count(),
        'rampas': services.ocupacion_rampas_centro(fecha),
    })


@operador_required
@require_POST
def registrar_ingreso_view(request, turno_id):
    turno = get_object_or_404(Turno, id=turno_id)
    try:
        request.perfil.registrar_ingreso_garita(
            turno,
            patente_vehiculo=request.POST.get('patente_vehiculo'),
        )
        messages.success(request, f'Ingreso a playa registrado para {turno.codigo}.')
    except services.TurnoError as error:
        messages.error(request, str(error))
    return redirect(_volver(request, reverse('turnos:panel_operador')))


@operador_required
@require_POST
def registrar_pesaje_view(request, turno_id):
    turno = get_object_or_404(Turno, id=turno_id)
    try:
        request.perfil.registrar_pesaje_y_descarga(
            turno,
            request.POST.get('peso_real'),
            request.POST.get('corte_tabaco'),
            request.POST.get('precio'),
        )
        messages.success(
            request,
            f'Descarga y liquidación cerradas para {turno.codigo}: {services.kg(turno.kilos_reales)}. Se emitió el comprobante oficial.'
        )
    except services.TurnoError as error:
        messages.error(request, str(error))
    return redirect(_volver(request, reverse('turnos:panel_operador')))


# ---------------------------------------------------------------------------
# Empresa de acopio
# ---------------------------------------------------------------------------
@empresa_required
def panel_empresa_view(request):
    services.cancelar_turnos_vencidos()
    empresa = request.perfil
    fecha = _fecha_param(request)
    form = ConvocatoriaForm(request.POST or None,
                            initial={'corte_tabaco': empresa.corte_solicitado or None})
    if request.method == 'POST' and form.is_valid():
        datos = form.cleaned_data
        try:
            avisos = empresa.enviar_aviso(datos['corte_tabaco'], datos['kilos'], datos['mensaje'])
        except services.TurnoError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, f'Convocatoria publicada para {len(avisos)} productores con cupo.')
            if not avisos:
                return redirect('turnos:panel_empresa')
            return _mostrar_avisos(request, avisos, 'Enviar la convocatoria a los productores',
                                   reverse('turnos:panel_empresa'))

    turnos = (
        Turno.objects.filter(empresa=empresa, fecha_entrega=fecha)
        .select_related('productor', 'rampa').order_by(*ORDEN_HORARIO, 'rampa__numero', 'id')
    )
    disponibilidad = services.consultar_disponibilidad(empresa, fecha)
    return render(request, 'turnos/empresa_panel.html', {
        **_contexto_fecha(fecha),
        'empresa': empresa,
        'disponibilidad': disponibilidad,
        'graficos': graficos.graficos_empresa(empresa, fecha, disponibilidad),
        'rampas': services.ocupacion_rampas(empresa, fecha),
        'turnos': turnos,
        'pendientes': Turno.objects.filter(empresa=empresa, estado=EstadoTurno.PENDIENTE).count(),
        'form': form,
    })


@empresa_required
def historial_empresa_view(request):
    services.cancelar_turnos_vencidos()
    empresa = request.perfil
    base = Turno.objects.filter(empresa=empresa).select_related('productor', 'comprobante', 'rampa')
    estado = request.GET.get('estado', '')
    turnos = base.filter(estado=estado) if estado in EstadoTurno.values else base
    return render(request, 'turnos/empresa_historial.html', {
        'empresa': empresa,
        'turnos': turnos.order_by(*ORDEN_RECIENTES),
        'total_turnos': base.count(),
        'turnos_descargados': base.filter(estado=EstadoTurno.DESCARGADO).count(),
        'turnos_activos': base.filter(estado__in=ACTIVOS).count(),
        'estado_filtro': estado,
        'estados_choices': EstadoTurno.choices,
    })


# ---------------------------------------------------------------------------
# Avisos de WhatsApp y API
# ---------------------------------------------------------------------------
@login_required
def avisos_view(request):
    datos = request.session.pop('avisos_whatsapp', None)
    if not datos:
        return redirect('login_redirect')
    return render(request, 'turnos/avisos.html', datos)


@login_required
def api_disponibilidad_view(request):
    try:
        empresa = EmpresaAcopio.objects.get(pk=int(request.GET.get('empresa_id', '')), activa=True)
        fecha = datetime.strptime(request.GET.get('fecha', ''), '%Y-%m-%d').date()
    except (ValueError, EmpresaAcopio.DoesNotExist):
        return JsonResponse({'error': 'Empresa o fecha inválida'}, status=400)
    return JsonResponse(services.consultar_disponibilidad(empresa, fecha))
