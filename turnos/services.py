"""
Lógica de negocio de SOLTAT.

- Capacidad: cada empresa tiene una capacidad diaria y una capacidad por
  franja de 2 h (06:00 a 22:00). Los turnos no cancelados ocupan lugar
  (con los kilos reales si ya se descargaron, o los estimados si no).
- Cupo de temporada: kilos totales que un productor puede entregar a una
  empresa (CupoProductor).
- Cumplimiento: premios y castigos según los incumplimientos del productor
  en los últimos 90 días (ver evaluar_cumplimiento).
- Avisos: mensajes de WhatsApp mediante enlaces wa.me (ver whatsapp.py).
"""
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import EmpresaAcopio, Productor
from .choices import FRANJAS_HORARIAS, INICIO_FRANJA, CorteTabaco, EstadoTurno
from .models import Comprobante, CupoProductor, RegistroOperador, Turno
from .whatsapp import armar_aviso


# ---------------------------------------------------------------------------
# Parámetros del modelo de premios y castigos
# ---------------------------------------------------------------------------
VENTANA_CUMPLIMIENTO_DIAS = 90   # Período que se evalúa
UMBRAL_DESVIO_KILOS = 0.20       # Desvío de kilos que cuenta como incumplimiento (20 %)
INCUMPLIMIENTOS_BLOQUEO = 3      # A partir de cuántos incumplimientos se bloquea
DIAS_BLOQUEO = 30                # Duración del bloqueo desde el último incumplimiento

# Nivel -> hora de la primera franja a la que puede acceder el productor
NIVELES = {
    'BUENO': {'display': 'Bueno', 'hora_minima': 6, 'css': 'success'},
    'REGULAR': {'display': 'Regular', 'hora_minima': 12, 'css': 'warning'},
    'MALO': {'display': 'Malo', 'hora_minima': 18, 'css': 'danger'},
    'BLOQUEADO': {'display': 'Bloqueado', 'hora_minima': None, 'css': 'dark'},
}


# ---------------------------------------------------------------------------
# Errores de negocio
# ---------------------------------------------------------------------------
class TurnoError(Exception):
    """Error de negocio con un mensaje apto para mostrar al usuario."""


class CupoExcedidoError(TurnoError):
    pass


class SinCupoProductorError(TurnoError):
    pass


class ProductorBloqueadoError(TurnoError):
    pass


def kg(valor) -> str:
    """Formatea kilos con separador de miles argentino: 12500 -> '12.500 kg'."""
    return f'{int(valor):,} kg'.replace(',', '.')


def _hoy():
    return timezone.localdate()


# ---------------------------------------------------------------------------
# Capacidad de la empresa
# ---------------------------------------------------------------------------
def _turnos_que_ocupan(empresa, fecha):
    return Turno.objects.filter(empresa=empresa, fecha_entrega=fecha).exclude(estado=EstadoTurno.CANCELADO)


def kilos_ocupados(empresa, fecha, franja=None) -> int:
    qs = _turnos_que_ocupan(empresa, fecha)
    if franja:
        qs = qs.filter(franja_horaria=franja)
    return qs.aggregate(total=Coalesce(Sum(Coalesce('kilos_reales', 'kilos_estimados')), 0))['total']


def ocupacion_por_franja(empresa, fecha) -> list:
    ocupados = dict(
        _turnos_que_ocupan(empresa, fecha)
        .values_list('franja_horaria')
        .annotate(total=Sum(Coalesce('kilos_reales', 'kilos_estimados')))
    )
    capacidad = empresa.capacidad_franja_kg
    franjas = []
    for valor, etiqueta in FRANJAS_HORARIAS:
        usados = ocupados.get(valor, 0) or 0
        franjas.append({
            'valor': valor,
            'etiqueta': etiqueta,
            'ocupados': usados,
            'capacidad': capacidad,
            'disponibles': max(0, capacidad - usados),
            'porcentaje': min(100, round(usados * 100 / capacidad)) if capacidad else 100,
        })
    return franjas


def consultar_disponibilidad(empresa, fecha, kilos_solicitados=0) -> dict:
    capacidad = empresa.capacidad_diaria_kg
    ocupados = kilos_ocupados(empresa, fecha)
    disponibles = max(0, capacidad - ocupados)
    porcentaje = min(100.0, round(ocupados * 100 / capacidad, 1)) if capacidad else 100.0
    if disponibles == 0:
        semaforo = 'completo'
    elif porcentaje >= 80:
        semaforo = 'alerta'
    else:
        semaforo = 'normal'
    return {
        'empresa_id': empresa.pk,
        'empresa_nombre': empresa.nombre,
        'fecha': fecha.isoformat(),
        'capacidad_diaria': capacidad,
        'capacidad_franja': empresa.capacidad_franja_kg,
        'kilos_ocupados': ocupados,
        'kilos_disponibles': disponibles,
        'porcentaje_ocupacion': porcentaje,
        'tiene_cupo': kilos_solicitados <= disponibles,
        'semaforo': semaforo,
        'franjas': ocupacion_por_franja(empresa, fecha),
    }


def buscar_franja(empresa, fecha, kilos, hora_minima=6, ahora=None):
    """
    Devuelve la primera franja (desde hora_minima) con lugar para `kilos`,
    sin superar la capacidad diaria. Si la fecha es hoy, saltea las franjas
    que ya empezaron. Devuelve None si no hay lugar.
    """
    if kilos > empresa.capacidad_diaria_kg - kilos_ocupados(empresa, fecha):
        return None
    ahora = ahora or timezone.localtime()
    for franja in ocupacion_por_franja(empresa, fecha):
        hora = INICIO_FRANJA[franja['valor']]
        if hora < hora_minima:
            continue
        if fecha == ahora.date() and hora <= ahora.hour:
            continue
        if kilos <= franja['disponibles']:
            return franja['valor']
    return None


# ---------------------------------------------------------------------------
# Cupo de temporada del productor
# ---------------------------------------------------------------------------
def _cupos(productor, empresa):
    return CupoProductor.objects.filter(productor=productor, recorredor__empresa_asignada=empresa)


def kilos_usados_cupo(productor, empresa) -> int:
    return (
        Turno.objects.filter(productor=productor, empresa=empresa)
        .exclude(estado=EstadoTurno.CANCELADO)
        .aggregate(total=Coalesce(Sum(Coalesce('kilos_reales', 'kilos_estimados')), 0))['total']
    )


def remanente_cupo(productor, empresa):
    """Kilos que le quedan al productor con esa empresa, o None si no tiene cupo."""
    cupos = _cupos(productor, empresa)
    if not cupos.exists():
        return None
    total = cupos.aggregate(total=Sum('cupo_maximo'))['total']
    return max(0, total - kilos_usados_cupo(productor, empresa))


def empresas_con_cupo(productor):
    return EmpresaAcopio.objects.filter(recorredores__cupos__productor=productor, activa=True).distinct()


def resumen_cupos(productor) -> list:
    resumen = []
    for empresa in empresas_con_cupo(productor):
        total = _cupos(productor, empresa).aggregate(total=Sum('cupo_maximo'))['total']
        usados = kilos_usados_cupo(productor, empresa)
        resumen.append({
            'empresa': empresa,
            'cupo_maximo': total,
            'usados': usados,
            'remanente': max(0, total - usados),
            'porcentaje': min(100, round(usados * 100 / total)) if total else 100,
        })
    return resumen


# ---------------------------------------------------------------------------
# Modelo de premios y castigos
# ---------------------------------------------------------------------------
def incumplimientos(productor, hoy=None) -> list:
    """
    Incumplimientos de los últimos 90 días:
    - No se presentó: turno CONFIRMADO cuya fecha ya pasó sin ingreso en garita.
    - Canceló un turno confirmado: turno CANCELADO que conserva su comprobante
      (cuando cancela el recorredor, el comprobante se anula y no penaliza).
    - Gran desvío de kilos: más de 20 % de diferencia entre reales y estimados.
    """
    hoy = hoy or _hoy()
    desde = hoy - timedelta(days=VENTANA_CUMPLIMIENTO_DIAS)
    turnos = Turno.objects.filter(productor=productor).filter(
        Q(estado=EstadoTurno.CONFIRMADO, fecha_entrega__gte=desde, fecha_entrega__lt=hoy)
        | Q(estado=EstadoTurno.CANCELADO, fue_confirmado=True)
        | Q(estado=EstadoTurno.DESCARGADO, fecha_entrega__gte=desde)
    )
    resultado = []
    for t in turnos:
        if t.estado == EstadoTurno.CONFIRMADO:
            motivo = 'No se presentó'
        elif t.estado == EstadoTurno.CANCELADO:
            motivo = 'Canceló un turno confirmado'
        elif t.desvio_pct is not None and abs(t.desvio_pct) > UMBRAL_DESVIO_KILOS * 100:
            motivo = f'Desvío de kilos de {t.desvio_pct:+.1f} %'
        else:
            continue
        fecha_incump = min(t.fecha_entrega, hoy) if t.fecha_entrega else hoy
        resultado.append({'turno': t, 'fecha': fecha_incump, 'motivo': motivo})
    return sorted(resultado, key=lambda i: i['fecha'], reverse=True)


def evaluar_cumplimiento(productor, hoy=None) -> dict:
    """
    0 incumplimientos -> BUENO   (franjas desde las 06:00)
    1                 -> REGULAR (desde las 12:00)
    2                 -> MALO    (desde las 18:00)
    3 o más           -> BLOQUEADO durante 30 días desde el último
                         incumplimiento; pasado ese plazo, MALO.
    """
    hoy = hoy or _hoy()
    lista = incumplimientos(productor, hoy)
    cantidad = len(lista)
    bloqueado_hasta = None
    if cantidad == 0:
        nivel = 'BUENO'
    elif cantidad == 1:
        nivel = 'REGULAR'
    elif cantidad == 2:
        nivel = 'MALO'
    else:
        fin_bloqueo = lista[0]['fecha'] + timedelta(days=DIAS_BLOQUEO)
        if hoy < fin_bloqueo:
            nivel = 'BLOQUEADO'
            bloqueado_hasta = fin_bloqueo
        else:
            nivel = 'MALO'
    datos = NIVELES[nivel]
    return {
        'nivel': nivel,
        'nivel_display': datos['display'],
        'css': datos['css'],
        'hora_minima': datos['hora_minima'],
        'cantidad': cantidad,
        'detalle': lista,
        'bloqueado': nivel == 'BLOQUEADO',
        'bloqueado_hasta': bloqueado_hasta,
        'ventana_dias': VENTANA_CUMPLIMIENTO_DIAS,
    }


# ---------------------------------------------------------------------------
# Creación, confirmación y cancelación de turnos
# ---------------------------------------------------------------------------
def generar_codigo_turno(fecha) -> str:
    """Ej. 'SLT-20261108-A7B2'"""
    while True:
        codigo = f"SLT-{fecha:%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"
        if not Turno.objects.filter(codigo=codigo).exists():
            return codigo


def emitir_comprobante(turno, peso_real, precio, corte_tabaco) -> Comprobante:
    """
    Crea el comprobante oficial del turno una vez finalizado el pesaje y descarga.
    Exige obligatoriamente peso real, corte real y precio acordado.
    """
    comprobante, _ = Comprobante.objects.update_or_create(
        turno=turno,
        defaults={
            'codigo_turno': turno.codigo,
            'empresa': turno.empresa,
            'fecha_entrega': turno.fecha_entrega or _hoy(),
            'peso_real': peso_real,
            'precio': precio,
            'corte_tabaco': corte_tabaco,
        },
    )
    return comprobante


def buscar_proxima_disponibilidad(empresa, kilos, hora_minima=6, desde_fecha=None, max_dias=30):
    """
    Devuelve la primera tupla (fecha, franja) que tenga capacidad suficiente
    para `kilos` en la `empresa`, respetando la `hora_minima` del productor.
    """
    ahora = timezone.localtime()
    inicio = desde_fecha or ahora.date()
    for offset in range(max_dias):
        f = inicio + timedelta(days=offset)
        franja = buscar_franja(empresa, f, kilos, hora_minima=hora_minima, ahora=ahora)
        if franja is not None:
            return f, franja
    return None, None


def _validar_datos_turno(empresa, kilos, corte_tabaco, patente_vehiculo, fecha_entrega):
    if not empresa.activa:
        raise TurnoError(f'{empresa.nombre} no está recibiendo tabaco en este momento.')
    if kilos is None or kilos <= 0:
        raise TurnoError('Los kilos estimados deben ser mayores a cero.')
    if kilos > empresa.capacidad_franja_kg:
        raise TurnoError(
            f'Un turno no puede superar la capacidad de una franja de {empresa.nombre} '
            f'({kg(empresa.capacidad_franja_kg)}). Divida la entrega en varios turnos.'
        )
    if corte_tabaco not in CorteTabaco.values:
        raise TurnoError('Debe indicar un corte de tabaco válido.')
    if not (patente_vehiculo or '').strip():
        raise TurnoError('Debe indicar la patente del vehículo.')
    if fecha_entrega < _hoy():
        raise TurnoError('La fecha de entrega no puede ser anterior a hoy.')


def _crear_turno(productor, empresa, kilos, corte_tabaco, patente_vehiculo, fecha_entrega,
                 estado, hora_minima, recorredor=None) -> Turno:
    _validar_datos_turno(empresa, kilos, corte_tabaco, patente_vehiculo, fecha_entrega)

    with transaction.atomic():
        # Bloqueo de la fila de la empresa: serializa reservas simultáneas.
        empresa = EmpresaAcopio.objects.select_for_update().get(pk=empresa.pk)

        remanente = remanente_cupo(productor, empresa)
        if remanente is None:
            raise SinCupoProductorError(
                f'{productor.get_nombre_completo()} no tiene cupo de temporada asignado con {empresa.nombre}.'
            )
        if kilos > remanente:
            raise SinCupoProductorError(
                f'Los kilos solicitados ({kg(kilos)}) superan el cupo de temporada disponible '
                f'con {empresa.nombre} ({kg(remanente)}).'
            )

        franja = buscar_franja(empresa, fecha_entrega, kilos, hora_minima)
        if franja is None:
            disponible_dia = max(0, empresa.capacidad_diaria_kg - kilos_ocupados(empresa, fecha_entrega))
            desde = f' a partir de las {hora_minima:02d}:00' if hora_minima > 6 else ''
            raise CupoExcedidoError(
                f'No hay una franja con lugar para {kg(kilos)}{desde} el '
                f'{fecha_entrega:%d/%m/%Y} en {empresa.nombre}. '
                f'Quedan {kg(disponible_dia)} libres en el día. Pruebe con otra fecha o menos kilos.'
            )

        turno = Turno.objects.create(
            codigo=generar_codigo_turno(fecha_entrega),
            productor=productor,
            empresa=empresa,
            recorredor=recorredor,
            kilos_estimados=kilos,
            patente_vehiculo=patente_vehiculo.strip().upper(),
            fecha_entrega=fecha_entrega,
            franja_horaria=franja,
            estado=estado,
            corte_tabaco=corte_tabaco,
        )
        if estado == EstadoTurno.CONFIRMADO:
            turno.fue_confirmado = True
            turno.save(update_fields=['fue_confirmado'])
        return turno


def solicitar_turno(productor, empresa, kilos, corte_tabaco, patente_vehiculo) -> Turno:
    """
    El productor solicita un turno sin elegir fecha: queda PENDIENTE hasta que lo confirme la empresa/recorredor.
    El sistema asignará automáticamente la fecha y franja al momento de la confirmación.
    """
    cumplimiento = evaluar_cumplimiento(productor)
    if cumplimiento['bloqueado']:
        raise ProductorBloqueadoError(
            f"Por incumplimientos recientes no puede solicitar turnos hasta el "
            f"{cumplimiento['bloqueado_hasta']:%d/%m/%Y}. Comuníquese con el recorredor de su empresa."
        )
    if not empresa.activa:
        raise TurnoError(f'{empresa.nombre} no está recibiendo tabaco en este momento.')
    if kilos is None or kilos <= 0:
        raise TurnoError('Los kilos estimados deben ser mayores a cero.')
    if kilos > empresa.capacidad_franja_kg:
        raise TurnoError(
            f'Un turno no puede superar la capacidad de una franja de {empresa.nombre} '
            f'({kg(empresa.capacidad_franja_kg)}). Divida la entrega en varios turnos.'
        )
    if corte_tabaco not in CorteTabaco.values:
        raise TurnoError('Debe indicar un corte de tabaco válido.')
    if not (patente_vehiculo or '').strip():
        raise TurnoError('Debe indicar la patente del vehículo.')

    with transaction.atomic():
        empresa = EmpresaAcopio.objects.select_for_update().get(pk=empresa.pk)
        remanente = remanente_cupo(productor, empresa)
        if remanente is None:
            raise SinCupoProductorError(
                f'{productor.get_nombre_completo()} no tiene cupo de temporada asignado con {empresa.nombre}.'
            )
        if kilos > remanente:
            raise SinCupoProductorError(
                f'Los kilos solicitados ({kg(kilos)}) superan el cupo de temporada disponible '
                f'con {empresa.nombre} ({kg(remanente)}).'
            )

        turno = Turno.objects.create(
            codigo=generar_codigo_turno(_hoy()),
            productor=productor,
            empresa=empresa,
            kilos_estimados=kilos,
            patente_vehiculo=patente_vehiculo.strip().upper(),
            corte_tabaco=corte_tabaco,
            fecha_entrega=None,
            franja_horaria=None,
            estado=EstadoTurno.PENDIENTE,
        )
        return turno


def asignar_turno(recorredor, productor, kilos, corte_tabaco, patente_vehiculo, fecha_entrega) -> Turno:
    """
    El recorredor asigna un turno: nace CONFIRMADO (sin comprobante hasta descarga).
    El recorredor puede asignar a productores bloqueados y en cualquier franja.
    """
    return _crear_turno(
        productor, recorredor.empresa_asignada, kilos, corte_tabaco, patente_vehiculo, fecha_entrega,
        estado=EstadoTurno.CONFIRMADO, hora_minima=6, recorredor=recorredor,
    )


def _verificar_empresa_recorredor(recorredor, turno):
    if turno.empresa_id != recorredor.empresa_asignada_id:
        raise TurnoError('El turno pertenece a otra empresa.')


def confirmar_turno(recorredor, turno) -> Turno:
    _verificar_empresa_recorredor(recorredor, turno)
    with transaction.atomic():
        empresa = EmpresaAcopio.objects.select_for_update().get(pk=turno.empresa_id)
        if not turno.cambiar_estado(EstadoTurno.CONFIRMADO):
            raise TurnoError(f'Solo se pueden confirmar turnos pendientes (estado actual: {turno.get_estado_display()}).')

        cumplimiento = evaluar_cumplimiento(turno.productor)
        hora_minima = cumplimiento['hora_minima'] if cumplimiento['hora_minima'] is not None else 6

        # Si el turno no tiene fecha o franja fijada, el sistema la busca y asigna automáticamente
        fecha_asignada = turno.fecha_entrega
        franja_asignada = turno.franja_horaria
        if not fecha_asignada or not franja_asignada:
            fecha_asignada, franja_asignada = buscar_proxima_disponibilidad(
                empresa, turno.kilos_estimados, hora_minima=hora_minima
            )
            if not fecha_asignada or not franja_asignada:
                raise CupoExcedidoError(
                    f'No se encontró una franja con lugar para {kg(turno.kilos_estimados)} '
                    f'en los próximos 30 días en {empresa.nombre}.'
                )

        if fecha_asignada < _hoy():
            raise TurnoError('No se puede confirmar un turno con fecha pasada.')

        turno.fecha_entrega = fecha_asignada
        turno.franja_horaria = franja_asignada
        turno.recorredor = recorredor
        turno.fue_confirmado = True
        turno.save(update_fields=['estado', 'recorredor', 'fecha_entrega', 'franja_horaria', 'fue_confirmado'])
        # NOTA: NO se emite comprobante al confirmar el turno. El comprobante solo se crea en la descarga.
    return turno


def _texto_turno(turno) -> str:
    if turno.fecha_entrega and turno.franja_horaria:
        return f'{turno.codigo} del {turno.fecha_entrega:%d/%m/%Y} ({turno.franja_horaria} hs)'
    return f'{turno.codigo} (pendiente de asignación)'


def cancelar_turno_productor(productor, turno, mensaje='') -> list:
    """
    PENDIENTE: se cancela sin más.
    CONFIRMADO: exige un mensaje, que se envía por WhatsApp a la empresa;
    el comprobante o la marca fue_confirmado cuenta como incumplimiento.
    Devuelve la lista de avisos de WhatsApp a enviar.
    """
    if turno.productor_id != productor.pk:
        raise TurnoError('Solo el productor titular puede cancelar este turno.')
    mensaje = (mensaje or '').strip()
    era_confirmado = turno.fue_confirmado or turno.estado == EstadoTurno.CONFIRMADO
    if era_confirmado and not mensaje:
        raise TurnoError('Para cancelar un turno confirmado debe indicar el motivo.')
    if not turno.cambiar_estado(EstadoTurno.CANCELADO):
        raise TurnoError(f'No se puede cancelar un turno en estado {turno.get_estado_display()}.')
    turno.save(update_fields=['estado'])

    if not era_confirmado:
        return []
    texto = (
        f'SOLTAT · {productor.get_nombre_completo()} canceló el turno confirmado '
        f'{_texto_turno(turno)}. Motivo: {mensaje}'
    )
    destinatarios = [turno.recorredor] if turno.recorredor else list(turno.empresa.recorredores.all())
    destinatarios.append(turno.empresa)
    return [armar_aviso(d, texto) for d in destinatarios]


def cancelar_turno_recorredor(recorredor, turno, mensaje='') -> list:
    """
    El recorredor rechaza un turno pendiente o cancela uno confirmado.
    El comprobante se anula y fue_confirmado se apaga para que no penalice al productor.
    """
    _verificar_empresa_recorredor(recorredor, turno)
    if turno.estado not in (EstadoTurno.PENDIENTE, EstadoTurno.CONFIRMADO):
        raise TurnoError(f'No se puede cancelar un turno en estado {turno.get_estado_display()}.')
    with transaction.atomic():
        Comprobante.objects.filter(turno=turno).delete()
        turno.fue_confirmado = False
        turno.cambiar_estado(EstadoTurno.CANCELADO)
        turno.save(update_fields=['estado', 'fue_confirmado'])
    texto = f'SOLTAT · {turno.empresa.nombre} canceló su turno {_texto_turno(turno)}.'
    if (mensaje or '').strip():
        texto += f' Motivo: {mensaje.strip()}'
    return [armar_aviso(turno.productor, texto)]


# ---------------------------------------------------------------------------
# Operación en garita y báscula
# ---------------------------------------------------------------------------
def registrar_ingreso_garita(operador, turno, momento=None) -> Turno:
    momento = momento or timezone.now()
    if turno.fecha_entrega != timezone.localtime(momento).date():
        raise TurnoError(f'El turno {turno.codigo} es para el {turno.fecha_entrega:%d/%m/%Y}, no para hoy.')
    if not turno.cambiar_estado(EstadoTurno.EN_PLAYA):
        raise TurnoError(f'Solo pueden ingresar turnos confirmados (estado actual: {turno.get_estado_display()}).')
    turno.hora_arribo_playa = momento
    turno.save(update_fields=['estado', 'hora_arribo_playa'])
    return turno


def registrar_pesaje_y_descarga(operador, turno, peso_real, corte_tabaco, precio, momento=None) -> RegistroOperador:
    try:
        peso_real = int(peso_real)
    except (TypeError, ValueError):
        raise TurnoError('Debe informar el peso real en kilos.')
    if peso_real <= 0:
        raise TurnoError('El peso real debe ser mayor a cero.')
    if corte_tabaco not in CorteTabaco.values:
        raise TurnoError('Debe indicar el corte real de tabaco percibido.')
    if precio in (None, ''):
        raise TurnoError('Debe indicar obligatoriamente el precio acordado por kg antes de emitir el comprobante.')
    try:
        precio = Decimal(str(precio).replace(',', '.'))
    except InvalidOperation:
        raise TurnoError('El precio debe ser un número válido.')
    if precio <= Decimal('0'):
        raise TurnoError('El precio acordado debe ser mayor a cero.')
    if turno.estado != EstadoTurno.EN_PLAYA:
        raise TurnoError(f'Solo se pueden pesar turnos en playa (estado actual: {turno.get_estado_display()}).')

    with transaction.atomic():
        registro = RegistroOperador.objects.create(
            turno=turno,
            operador=operador,
            peso_acordado=turno.kilos_estimados,
            peso_real=peso_real,
            corte_tabaco=corte_tabaco,
        )
        # El comprobante ÚNICAMENTE se emite aquí tras el pesaje, con precio y corte real
        emitir_comprobante(turno, peso_real=peso_real, precio=precio, corte_tabaco=corte_tabaco)

        turno.liquidar_descarga(peso_real, corte_tabaco)
        turno.cambiar_estado(EstadoTurno.DESCARGADO)
        turno.hora_descarga = momento or timezone.now()
        turno.save(update_fields=['kilos_reales', 'corte_tabaco', 'estado', 'hora_descarga'])
    return registro


# ---------------------------------------------------------------------------
# Convocatorias de la empresa
# ---------------------------------------------------------------------------
def enviar_aviso(empresa, corte_tabaco, kilos, mensaje='') -> list:
    """
    La empresa publica qué corte busca recibir y cuántos kilos. Se arma un
    aviso de WhatsApp para cada productor con cupo en la empresa.
    No bloquea turnos de otros cortes.
    """
    if corte_tabaco not in CorteTabaco.values:
        raise TurnoError('Debe indicar un corte de tabaco válido.')
    if not kilos or kilos <= 0:
        raise TurnoError('Los kilos de la convocatoria deben ser mayores a cero.')
    empresa.corte_solicitado = corte_tabaco
    empresa.save(update_fields=['corte_solicitado'])

    texto = (
        f'SOLTAT · {empresa.nombre} convoca entregas de tabaco corte '
        f'{CorteTabaco(corte_tabaco).label.upper()}: {kg(kilos)}.'
    )
    if (mensaje or '').strip():
        texto += f' {mensaje.strip()}'
    productores = Productor.objects.filter(cupos__recorredor__empresa_asignada=empresa).distinct()
    return [armar_aviso(p, texto) for p in productores]


# ---------------------------------------------------------------------------
# Rendimiento del productor
# ---------------------------------------------------------------------------
def _promedio(valores):
    valores = [v for v in valores if v is not None]
    return round(sum(valores) / len(valores), 1) if valores else None


def rendimiento_productor(productor) -> dict:
    hoy = _hoy()
    turnos = list(productor.turnos.select_related('empresa').order_by('-fecha_entrega'))
    descargados = [t for t in turnos if t.estado == EstadoTurno.DESCARGADO]
    # Turnos que debían presentarse: los que llegaron a playa y los confirmados vencidos.
    presentados = [t for t in turnos if t.estado in (EstadoTurno.EN_PLAYA, EstadoTurno.DESCARGADO)]
    ausentes = [t for t in turnos if t.estado == EstadoTurno.CONFIRMADO and t.fecha_entrega < hoy]
    esperados = len(presentados) + len(ausentes)

    return {
        'descargados': descargados,
        'total_entregas': len(descargados),
        'kilos_estimados': sum(t.kilos_estimados for t in descargados),
        'kilos_reales': sum(t.kilos_reales or 0 for t in descargados),
        'desvio_medio': _promedio([t.desvio_pct for t in descargados]),
        'espera_media': _promedio([t.calcular_tiempo_espera() for t in descargados]),
        'ciclo_medio': _promedio([t.calcular_tiempo_ciclo_total() for t in descargados]),
        'presentismo': round(len(presentados) * 100 / esperados) if esperados else None,
        'ausencias': len(ausentes),
        'cumplimiento': evaluar_cumplimiento(productor, hoy),
        'cupos': resumen_cupos(productor),
    }
