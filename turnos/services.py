"""
Lógica de negocio de SOLTAT.

- Capacidad: cada empresa tiene una capacidad diaria. Los turnos no
  cancelados ocupan lugar (con los kilos reales si ya se descargaron, o los
  estimados si no).
- Duración: cada turno dura kilos ÷ velocidad de descarga de la empresa,
  redondeado hacia arriba a tramos de 30 min, dentro de 06:00 a 22:00.
- Rampas: cada turno ocupa una rampa durante todo su horario, en un bloque
  continuo (OcupacionRampa impide superposiciones). Cada empresa usa solo sus
  rampas y tiene prioridad en su horario preferente; fuera de él toma tramos
  libres solo el mismo día (prioridad por tramo, ver _tramo_disponible).
- Cupo de temporada: kilos totales que un productor puede entregar a una
  empresa (CupoProductor).
- Cumplimiento: premios y castigos según los incumplimientos del productor
  en los últimos 90 días (ver evaluar_cumplimiento).
- Avisos: mensajes de WhatsApp mediante enlaces wa.me (ver whatsapp.py).
"""
import functools
import logging
import math
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import EmpresaAcopio, Productor
from .choices import (
    APERTURA_MIN, CIERRE_MIN, FRANJAS_HORARIAS, HORA_APERTURA, HORA_CIERRE, INICIO_FRANJA, INICIO_TARDE_MIN,
    INTERVALO_MIN,
    CorteTabaco, EstadoTurno, HorarioPreferente, a_hora, a_minutos, franja_de_minuto,
    horario_de_minuto, texto_duracion,
)
from .models import ORDEN_RECIENTES, Comprobante, CupoProductor, OcupacionRampa, Rampa, RegistroOperador, Turno
from .whatsapp import armar_aviso


# ---------------------------------------------------------------------------
# Parámetros del modelo de premios y castigos
# ---------------------------------------------------------------------------
VENTANA_CUMPLIMIENTO_DIAS = 90   # Período que se evalúa
UMBRAL_DESVIO_KILOS = 0.20       # Desvío de kilos que cuenta como incumplimiento (20 %)
INCUMPLIMIENTOS_BLOQUEO = 3      # A partir de cuántos incumplimientos se bloquea
DIAS_BLOQUEO = 30                # Duración del bloqueo desde el último incumplimiento
DIAS_DEMORA_MALO = 5             # Días mínimos de demora para productores en nivel MALO

# Rampas: días antes de la entrega en que una rampa se libera para una empresa
# fuera de su horario preferente (0 = el mismo día de la entrega).
DIAS_LIBERACION_RAMPAS = 0

# Nivel -> configuración de visualización y demora en días (sin restricción horaria)
NIVELES = {
    'BUENO': {'display': 'Bueno', 'hora_minima': 6, 'dias_demora': 0, 'css': 'success'},
    'REGULAR': {'display': 'Regular', 'hora_minima': 6, 'dias_demora': 0, 'css': 'warning'},
    'MALO': {'display': 'Malo', 'hora_minima': 6, 'dias_demora': DIAS_DEMORA_MALO, 'css': 'danger'},
    'BLOQUEADO': {'display': 'Bloqueado', 'hora_minima': None, 'dias_demora': None, 'css': 'dark'},
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


class HorarioOcupadoError(TurnoError):
    """Otra reserva tomó la rampa en ese horario al mismo tiempo."""


class AgendaOcupadaError(TurnoError):
    """La base de datos siguió bloqueada por otras reservas más allá del tiempo de espera."""


logger = logging.getLogger(__name__)


def _es_bloqueo(error) -> bool:
    return 'locked' in str(error).lower()


def sin_bloqueos(funcion):
    """
    Convierte un bloqueo de la base de datos (SQLite: 'database is locked')
    en un error de negocio con un mensaje para el usuario, en vez de un
    error 500. Con transaction_mode IMMEDIATE y timeout 20 s solo ocurre si
    hay muchas reservas simultáneas durante más de 20 segundos.
    """
    @functools.wraps(funcion)
    def envoltura(*args, **kwargs):
        try:
            return funcion(*args, **kwargs)
        except OperationalError as error:
            if not _es_bloqueo(error):
                raise
            logger.warning('Agenda bloqueada en %s: %s', funcion.__name__, error)
            raise AgendaOcupadaError(
                'El sistema está procesando otras reservas en este momento. Intente nuevamente en unos segundos.'
            ) from error
    return envoltura


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


def kilos_ocupados(empresa, fecha) -> int:
    return _turnos_que_ocupan(empresa, fecha).aggregate(
        total=Coalesce(Sum(Coalesce('kilos_reales', 'kilos_estimados')), 0)
    )['total']


def capacidad_por_hora(empresa, fecha, hoy=None) -> dict:
    """
    {minuto_de_la_hora: kilos} que la empresa puede descargar en cada hora
    según las rampas que realmente tiene a su disposición: cuenta los tramos
    de 30 min de sus rampas que están libres y disponibles para ella (sin
    turnos de otras empresas ni prioridad de otra) o que ocupa con sus propios
    turnos. Como las rampas se comparten, puede cambiar de hora en hora.
    """
    rampas = _rampas_de(empresa)
    ocupadas = rampas_ocupadas(fecha)
    liberada = rampa_liberada(fecha, hoy)
    capacidad = {}
    for hora in range(APERTURA_MIN, CIERRE_MIN, 60):
        tramos = 0
        for rampa in rampas:
            usados = ocupadas.get(rampa.pk, {})
            for m in (hora, hora + INTERVALO_MIN):
                propio = m in usados and usados[m].empresa_id == empresa.pk
                if propio or _tramo_disponible(rampa, empresa, m, usados, liberada):
                    tramos += 1
        capacidad[hora] = round(empresa.velocidad_descarga_kg_hora * tramos * INTERVALO_MIN / 60)
    return capacidad


def ocupacion_por_hora(empresa, fecha) -> list:
    """
    Kilos que la empresa tiene programados en cada hora de 06:00 a 22:00.
    Los kilos de un turno se reparten en forma pareja durante su duración.
    La capacidad de cada hora sale de las rampas que la empresa tiene
    realmente disponibles en esa hora (ver capacidad_por_hora).
    """
    capacidades = capacidad_por_hora(empresa, fecha)
    horas = {m: 0.0 for m in range(APERTURA_MIN, CIERRE_MIN, 60)}
    for t in _turnos_que_ocupan(empresa, fecha):
        inicio, fin, duracion = t.hora_inicio_efectiva, t.hora_fin_efectiva, t.duracion_efectiva
        if not inicio or not fin or not duracion:
            continue
        ini, fn = a_minutos(inicio), a_minutos(fin)
        for m in horas:
            solapa = max(0, min(fn, m + 60) - max(ini, m))
            horas[m] += t.kilos_efectivos * solapa / duracion
    resultado = []
    for m, kilos in horas.items():
        usados = round(kilos)
        capacidad = capacidades[m]
        resultado.append({
            'valor': f'{a_hora(m):%H:%M}',
            'etiqueta': f'{a_hora(m):%H:%M} a {a_hora(m + 60):%H:%M} hs',
            'ocupados': usados,
            'capacidad': capacidad,
            'porcentaje': min(100, round(usados * 100 / capacidad)) if capacidad else 100,
        })
    return resultado


def _franjas_compatibles(horas) -> list:
    """
    Formato anterior de la API (franjas de 2 h), armado sumando las horas de
    cada franja. Se mantiene para no romper a quien ya consume la API.
    """
    franjas = []
    for valor, etiqueta in FRANJAS_HORARIAS:
        de_la_franja = [h for h in horas if INICIO_FRANJA[valor] <= int(h['valor'][:2]) < INICIO_FRANJA[valor] + 2]
        ocupados = sum(h['ocupados'] for h in de_la_franja)
        capacidad = sum(h['capacidad'] for h in de_la_franja)
        franjas.append({
            'valor': valor,
            'etiqueta': etiqueta,
            'ocupados': ocupados,
            'capacidad': capacidad,
            'disponibles': max(0, capacidad - ocupados),
            'porcentaje': min(100, round(ocupados * 100 / capacidad)) if capacidad else 100,
        })
    return franjas


def consultar_disponibilidad(empresa, fecha, kilos_solicitados=0) -> dict:
    """
    Disponibilidad del día (también es la respuesta de /api/disponibilidad/).
    Claves nuevas: 'velocidad_descarga' y 'horas' (ocupación y capacidad por hora).
    Claves anteriores, conservadas por compatibilidad y marcadas como obsoletas:
    'capacidad_franja' (dato de referencia) y 'franjas' (suma de las horas de
    cada franja de 2 h). Se podrán quitar cuando nadie las use.
    """
    capacidad = empresa.capacidad_diaria_kg
    ocupados = kilos_ocupados(empresa, fecha)
    disponibles = max(0, capacidad - ocupados)
    porcentaje = min(100.0, round(ocupados * 100 / capacidad, 1)) if capacidad else 100.0
    horas = ocupacion_por_hora(empresa, fecha)
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
        'velocidad_descarga': empresa.velocidad_descarga_kg_hora,
        'kilos_ocupados': ocupados,
        'kilos_disponibles': disponibles,
        'porcentaje_ocupacion': porcentaje,
        'tiene_cupo': kilos_solicitados <= disponibles,
        'semaforo': semaforo,
        'horas': horas,
        # Obsoletas (formato anterior), conservadas por compatibilidad:
        'capacidad_franja': empresa.capacidad_franja_kg,
        'franjas': _franjas_compatibles(horas),
    }


# ---------------------------------------------------------------------------
# Duración del turno
# ---------------------------------------------------------------------------
def duracion_turno(empresa, kilos) -> int:
    """
    Minutos que lleva descargar `kilos` en una rampa de la empresa:
    kilos ÷ velocidad, redondeado hacia arriba a tramos de 30 min.
    Ej. con 17.500 kg/h: 35.000 kg -> 120 min; 100.000 kg -> 360 min.
    """
    minutos = math.ceil(kilos * 60 / empresa.velocidad_descarga_kg_hora)
    return max(INTERVALO_MIN, math.ceil(minutos / INTERVALO_MIN) * INTERVALO_MIN)


# ---------------------------------------------------------------------------
# Rampas de descarga
# ---------------------------------------------------------------------------
def rampas_ocupadas(fecha) -> dict:
    """{rampa_id: {minuto_del_tramo: turno}} con los tramos reservados ese día."""
    ocupadas = {}
    tramos = (
        OcupacionRampa.objects.filter(fecha=fecha)
        .select_related('turno__empresa', 'turno__productor')
    )
    for o in tramos:
        ocupadas.setdefault(o.rampa_id, {})[a_minutos(o.inicio)] = o.turno
    return ocupadas


def rampa_liberada(fecha, hoy=None) -> bool:
    """True si en esa fecha cualquier empresa habilitada puede usar una rampa libre."""
    return fecha <= (hoy or _hoy()) + timedelta(days=DIAS_LIBERACION_RAMPAS)


def _rampas_de(empresa):
    return list(empresa.rampas.filter(activa=True).prefetch_related('empresas').order_by('numero'))


def _reservada_para_otra(rampa, empresa, horario) -> bool:
    """La rampa tiene, en ese horario, prioridad para otra empresa habilitada."""
    return any(
        e.pk != empresa.pk and e.activa and e.horario_preferente == horario
        for e in rampa.empresas.all()
    )


def _tramo_disponible(rampa, empresa, minuto, usados, liberada) -> bool:
    """
    Un tramo de 30 min de la rampa está disponible para la empresa si nadie
    lo reservó y, cuando cae fuera del horario preferente de la empresa, no
    es preferente para otra empresa (salvo que la rampa ya esté liberada).
    """
    if minuto in usados:
        return False
    horario = horario_de_minuto(minuto)
    return (empresa.horario_preferente == horario or liberada
            or not _reservada_para_otra(rampa, empresa, horario))


def _posicion(minuto) -> float:
    """Posición (%) de un minuto del día en la línea de tiempo de 06:00 a 22:00."""
    return round((minuto - APERTURA_MIN) * 100 / (CIERRE_MIN - APERTURA_MIN), 3)


def _bloques_de_turnos(tramos_de_rampa) -> list:
    """Agrupa los tramos de una rampa por turno: un bloque por turno."""
    turnos = {}
    for turno in tramos_de_rampa.values():
        turnos[turno.pk] = turno
    bloques = []
    for turno in sorted(turnos.values(), key=lambda t: a_minutos(t.hora_inicio_efectiva)):
        ini, fin = a_minutos(turno.hora_inicio_efectiva), a_minutos(turno.hora_fin_efectiva)
        bloques.append({'turno': turno, 'izq': _posicion(ini), 'ancho': _posicion(fin) - _posicion(ini)})
    return bloques


def _marcas_horarias() -> list:
    return [{'hora': f'{h:02d}:00', 'izq': _posicion(h * 60)} for h in range(HORA_APERTURA, HORA_CIERRE + 1, 2)]


def ocupacion_rampas(empresa, fecha, hoy=None) -> dict:
    """Línea de tiempo de las rampas de la empresa (panel de la empresa)."""
    rampas = _rampas_de(empresa)
    ocupadas = rampas_ocupadas(fecha)
    liberada = rampa_liberada(fecha, hoy)
    filas = []
    for r in rampas:
        bloques = _bloques_de_turnos(ocupadas.get(r.pk, {}))
        for b in bloques:
            b['estado'] = 'propia' if b['turno'].empresa_id == empresa.pk else 'otra'
        reservas = []
        if not liberada:
            for horario, ini, fin in ((HorarioPreferente.MANANA, APERTURA_MIN, INICIO_TARDE_MIN),
                                      (HorarioPreferente.TARDE, INICIO_TARDE_MIN, CIERRE_MIN)):
                if horario != empresa.horario_preferente and _reservada_para_otra(r, empresa, horario):
                    reservas.append({'izq': _posicion(ini), 'ancho': _posicion(fin) - _posicion(ini)})
        filas.append({'rampa': r, 'bloques': bloques, 'reservas': reservas})
    preferente = (INICIO_TARDE_MIN, CIERRE_MIN) if empresa.horario_preferente == HorarioPreferente.TARDE \
        else (APERTURA_MIN, INICIO_TARDE_MIN)
    return {
        'rampas': rampas,
        'filas': filas,
        'liberada': liberada,
        'marcas': _marcas_horarias(),
        'preferente': {'izq': _posicion(preferente[0]), 'ancho': _posicion(preferente[1]) - _posicion(preferente[0])},
    }


def ocupacion_rampas_centro(fecha) -> dict:
    """Línea de tiempo de las 5 rampas del centro (panel del operador)."""
    rampas = list(Rampa.objects.filter(activa=True).order_by('numero'))
    ocupadas = rampas_ocupadas(fecha)
    filas = [{'rampa': r, 'bloques': _bloques_de_turnos(ocupadas.get(r.pk, {}))} for r in rampas]
    return {'rampas': rampas, 'filas': filas, 'marcas': _marcas_horarias()}


def fin_de_la_agenda(empresa, fecha):
    """Minuto en que termina el último turno de la empresa ese día (o None si no tiene turnos)."""
    fines = [a_minutos(t.hora_fin_efectiva) for t in _turnos_que_ocupan(empresa, fecha)
             if t.hora_fin_efectiva]
    return max(fines) if fines else None


def _inicios_posibles(empresa, fecha, duracion, hora_minima, ahora, desde_minuto=None):
    """Horarios de inicio (cada 30 min) en que el turno entra completo antes del cierre."""
    primero = max(APERTURA_MIN, hora_minima * 60, desde_minuto or 0)
    if fecha == ahora.date():
        # Hoy solo se ofrecen tramos que todavía no empezaron.
        actual = ahora.hour * 60 + ahora.minute
        primero = max(primero, (actual // INTERVALO_MIN + 1) * INTERVALO_MIN)
    inicios = list(range(primero, CIERRE_MIN - duracion + 1, INTERVALO_MIN))

    def en_su_horario(inicio):
        return all(horario_de_minuto(m) == empresa.horario_preferente
                   for m in range(inicio, inicio + duracion, INTERVALO_MIN))

    # Primero los bloques que caen enteros en el horario preferente de la empresa.
    return sorted(inicios, key=lambda i: (not en_su_horario(i), i))


def buscar_bloque(empresa, fecha, kilos, hora_minima=6, ahora=None, al_final_de_la_agenda=False):
    """
    Busca un bloque continuo para descargar `kilos` el día `fecha`:
    (inicio_min, fin_min, rampa). Durante todo el bloque la rampa debe estar
    libre y disponible para la empresa (prioridad por tramo). Respeta la
    capacidad diaria y el horario de 06:00 a 22:00. Si la fecha es hoy, solo
    usa tramos que todavía no empezaron. Con `al_final_de_la_agenda` (nivel
    MALO), el turno empieza cuando termina el último turno de la empresa ese
    día, nunca antes ni al mismo tiempo. Devuelve (None, None, None) si no hay lugar.
    """
    ahora = ahora or timezone.localtime()
    if fecha < ahora.date():
        return None, None, None
    if kilos > empresa.capacidad_diaria_kg - kilos_ocupados(empresa, fecha):
        return None, None, None
    duracion = duracion_turno(empresa, kilos)
    if duracion > CIERRE_MIN - APERTURA_MIN:
        return None, None, None
    rampas = _rampas_de(empresa)
    ocupadas = rampas_ocupadas(fecha)
    liberada = rampa_liberada(fecha, ahora.date())
    desde_minuto = fin_de_la_agenda(empresa, fecha) if al_final_de_la_agenda else None
    for inicio in _inicios_posibles(empresa, fecha, duracion, hora_minima, ahora, desde_minuto):
        for rampa in rampas:
            usados = ocupadas.get(rampa.pk, {})
            if all(_tramo_disponible(rampa, empresa, m, usados, liberada)
                   for m in range(inicio, inicio + duracion, INTERVALO_MIN)):
                return inicio, inicio + duracion, rampa
    return None, None, None


CAMPOS_AGENDA = ['hora_inicio', 'hora_fin', 'duracion_min', 'franja_horaria', 'rampa']


@sin_bloqueos
def reservar_bloque(turno, inicio, fin, rampa) -> Turno:
    """
    Guarda el horario y la rampa del turno y reserva sus tramos de 30 min,
    todo en una sola transacción: si algún tramo ya está tomado (por ejemplo,
    por otra reserva simultánea), la restricción única de OcupacionRampa lo
    impide, se deshace todo (horario, rampa y tramos) y se informa el conflicto.
    No depende de que quien la llama haya abierto una transacción.
    """
    anteriores = {campo: getattr(turno, campo) for campo in CAMPOS_AGENDA}
    try:
        with transaction.atomic():
            turno.hora_inicio = a_hora(inicio)
            turno.hora_fin = a_hora(fin)
            turno.duracion_min = fin - inicio
            turno.franja_horaria = franja_de_minuto(inicio)
            turno.rampa = rampa
            turno.save(update_fields=CAMPOS_AGENDA)
            OcupacionRampa.objects.bulk_create([
                OcupacionRampa(turno=turno, rampa=rampa, fecha=turno.fecha_entrega, inicio=a_hora(m))
                for m in range(inicio, fin, INTERVALO_MIN)
            ])
    except IntegrityError:
        for campo, valor in anteriores.items():  # el objeto en memoria vuelve a como estaba
            setattr(turno, campo, valor)
        raise HorarioOcupadoError(
            f'Otro turno acaba de reservar la {rampa} en ese horario. Intente nuevamente.'
        )
    return turno


def liberar_rampa(turno) -> None:
    """Libera los tramos de la rampa del turno (al cancelarlo)."""
    OcupacionRampa.objects.filter(turno=turno).delete()


def _texto_horario(fecha, inicio, fin, rampa) -> str:
    return (f'{fecha:%d/%m/%Y} de {a_hora(inicio):%H:%M} a {a_hora(fin):%H:%M} hs '
            f'({texto_duracion(fin - inicio)}), {rampa}')


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
            motivo = 'No se presentó (ausencia)' if t.cancelado_por_ausencia else 'Canceló un turno confirmado'
        elif t.desvio_pct is not None and abs(t.desvio_pct) > UMBRAL_DESVIO_KILOS * 100:
            motivo = f'Desvío de kilos de {t.desvio_pct:+.1f} %'
        else:
            continue
        fecha_incump = min(t.fecha_entrega, hoy) if t.fecha_entrega else hoy
        resultado.append({'turno': t, 'fecha': fecha_incump, 'motivo': motivo})
    return sorted(resultado, key=lambda i: i['fecha'], reverse=True)


def evaluar_cumplimiento(productor, hoy=None) -> dict:
    """
    0 incumplimientos -> BUENO     (sin restricciones)
    1                 -> REGULAR   (sin restricción horaria)
    2                 -> MALO      (turnos asignados >= 5 días después o al final de la agenda)
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
        'dias_demora': datos['dias_demora'],
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


def buscar_proxima_disponibilidad(empresa, kilos, hora_minima=6, desde_fecha=None, max_dias=30,
                                  al_final_de_la_agenda=False):
    """
    Devuelve la primera tupla (fecha, inicio_min, fin_min, rampa) con un bloque
    continuo para `kilos` en la `empresa`, desde `desde_fecha` y por hasta
    `max_dias` días. Devuelve (None, None, None, None) si no hay lugar.
    """
    ahora = timezone.localtime()
    desde = max(desde_fecha or ahora.date(), ahora.date())
    for offset in range(max_dias):
        f = desde + timedelta(days=offset)
        inicio, fin, rampa = buscar_bloque(empresa, f, kilos, hora_minima=hora_minima, ahora=ahora,
                                           al_final_de_la_agenda=al_final_de_la_agenda)
        if inicio is not None:
            return f, inicio, fin, rampa
    return None, None, None, None


def _validar_kilos_de_un_turno(empresa, kilos):
    """
    No hay un máximo de kilos por turno: el límite es que la descarga entre en
    un día (capacidad diaria de la empresa y horario de 06:00 a 22:00).
    """
    if kilos > empresa.capacidad_diaria_kg:
        raise TurnoError(
            f'Los kilos superan la capacidad diaria de {empresa.nombre} '
            f'({kg(empresa.capacidad_diaria_kg)}). Divida la entrega en varios días.'
        )
    duracion = duracion_turno(empresa, kilos)
    if duracion > CIERRE_MIN - APERTURA_MIN:
        raise TurnoError(
            f'Descargar {kg(kilos)} llevaría {texto_duracion(duracion)} y el acopio atiende '
            f'{HORA_CIERRE - HORA_APERTURA} horas por día. Divida la entrega en varios días.'
        )


def _validar_datos_turno(empresa, kilos, corte_tabaco, patente_vehiculo, fecha_entrega):
    if not empresa.activa:
        raise TurnoError(f'{empresa.nombre} no está recibiendo tabaco en este momento.')
    if kilos is None or kilos < 50:
        raise TurnoError('La cantidad mínima para solicitar un turno es de 50 kg.')
    _validar_kilos_de_un_turno(empresa, kilos)
    if corte_tabaco not in CorteTabaco.values:
        raise TurnoError('Debe indicar un corte de tabaco válido.')
    if fecha_entrega < _hoy():
        raise TurnoError('La fecha de entrega no puede ser anterior a hoy.')


def _crear_turno(productor, empresa, kilos, corte_tabaco, patente_vehiculo, fecha_entrega,
                 estado, hora_minima, recorredor=None) -> Turno:
    _validar_datos_turno(empresa, kilos, corte_tabaco, patente_vehiculo, fecha_entrega)

    with transaction.atomic():
        # Bloqueo de la empresa y de las rampas: serializa reservas simultáneas
        # (las rampas se comparten entre empresas).
        empresa = EmpresaAcopio.objects.select_for_update().get(pk=empresa.pk)
        list(Rampa.objects.select_for_update().all())

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

        inicio, fin, rampa = buscar_bloque(empresa, fecha_entrega, kilos, hora_minima)
        if inicio is None:
            disponible_dia = max(0, empresa.capacidad_diaria_kg - kilos_ocupados(empresa, fecha_entrega))
            duracion = texto_duracion(duracion_turno(empresa, kilos))
            mensaje = (
                f'No hay un horario continuo de {duracion} con rampa libre para {kg(kilos)} el '
                f'{fecha_entrega:%d/%m/%Y} en {empresa.nombre} '
                f'(quedan {kg(disponible_dia)} libres en el día).'
            )
            otra_fecha, o_ini, o_fin, o_rampa = buscar_proxima_disponibilidad(
                empresa, kilos, hora_minima, desde_fecha=fecha_entrega + timedelta(days=1)
            )
            if otra_fecha:
                mensaje += f' Próximo horario disponible: {_texto_horario(otra_fecha, o_ini, o_fin, o_rampa)}.'
            raise CupoExcedidoError(mensaje)

        turno = Turno.objects.create(
            codigo=generar_codigo_turno(fecha_entrega),
            productor=productor,
            empresa=empresa,
            recorredor=recorredor,
            kilos_estimados=kilos,
            patente_vehiculo=(patente_vehiculo or '').strip().upper(),
            fecha_entrega=fecha_entrega,
            estado=estado,
            corte_tabaco=corte_tabaco,
        )
        reservar_bloque(turno, inicio, fin, rampa)
        if estado == EstadoTurno.CONFIRMADO:
            turno.fue_confirmado = True
            turno.save(update_fields=['fue_confirmado'])
        return turno


@sin_bloqueos
def solicitar_turno(productor, empresa, kilos, corte_tabaco, patente_vehiculo='') -> Turno:
    """
    El productor solicita un turno sin elegir fecha: queda PENDIENTE hasta que lo confirme la empresa/recorredor.
    El sistema asignará automáticamente la fecha y el horario (según los kilos) al confirmarlo.
    """
    cumplimiento = evaluar_cumplimiento(productor)
    if cumplimiento['bloqueado']:
        raise ProductorBloqueadoError(
            f"Por incumplimientos recientes no puede solicitar turnos hasta el "
            f"{cumplimiento['bloqueado_hasta']:%d/%m/%Y}. Comuníquese con el recorredor de su empresa."
        )
    if not empresa.activa:
        raise TurnoError(f'{empresa.nombre} no está recibiendo tabaco en este momento.')
    if kilos is None or kilos < 50:
        raise TurnoError('La cantidad mínima para solicitar un turno es de 50 kg.')
    _validar_kilos_de_un_turno(empresa, kilos)
    if corte_tabaco not in CorteTabaco.values:
        raise TurnoError('Debe indicar un corte de tabaco válido.')

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
            patente_vehiculo=(patente_vehiculo or '').strip().upper(),
            corte_tabaco=corte_tabaco,
            fecha_entrega=None,
            franja_horaria=None,
            estado=EstadoTurno.PENDIENTE,
        )
        return turno


@sin_bloqueos
def asignar_turno(recorredor, productor, kilos, corte_tabaco, patente_vehiculo, fecha_entrega) -> Turno:
    """
    El recorredor asigna un turno: nace CONFIRMADO (sin comprobante hasta descarga).
    El recorredor puede asignar a productores bloqueados y en cualquier horario disponible.
    """
    return _crear_turno(
        productor, recorredor.empresa_asignada, kilos, corte_tabaco, patente_vehiculo, fecha_entrega,
        estado=EstadoTurno.CONFIRMADO, hora_minima=6, recorredor=recorredor,
    )


def _verificar_empresa_recorredor(recorredor, turno):
    if turno.empresa_id != recorredor.empresa_asignada_id:
        raise TurnoError('El turno pertenece a otra empresa.')


@sin_bloqueos
def confirmar_turno(recorredor, turno) -> Turno:
    _verificar_empresa_recorredor(recorredor, turno)
    with transaction.atomic():
        empresa = EmpresaAcopio.objects.select_for_update().get(pk=turno.empresa_id)
        list(Rampa.objects.select_for_update().all())
        if not turno.cambiar_estado(EstadoTurno.CONFIRMADO):
            raise TurnoError(f'Solo se pueden confirmar turnos pendientes (estado actual: {turno.get_estado_display()}).')

        cumplimiento = evaluar_cumplimiento(turno.productor)
        dias_demora = cumplimiento.get('dias_demora') or 0
        desde_fecha = _hoy() + timedelta(days=dias_demora)

        # Nivel MALO: al menos 5 días después, desde el último día con turnos
        # programados y, ese día, después del último turno de la agenda.
        es_malo = cumplimiento['nivel'] == 'MALO'
        if es_malo:
            ultima_fecha = (
                Turno.objects.filter(empresa=empresa, fecha_entrega__isnull=False)
                .exclude(estado=EstadoTurno.CANCELADO)
                .aggregate(ultima=Max('fecha_entrega'))['ultima']
            )
            if ultima_fecha and ultima_fecha > desde_fecha:
                desde_fecha = ultima_fecha

        # El sistema busca el primer bloque continuo con rampa libre para los kilos del turno.
        fecha_asignada, inicio, fin, rampa = buscar_proxima_disponibilidad(
            empresa, turno.kilos_estimados, hora_minima=6, desde_fecha=desde_fecha,
            al_final_de_la_agenda=es_malo,
        )
        if not fecha_asignada:
            raise CupoExcedidoError(
                f'No se encontró un horario continuo de '
                f'{texto_duracion(duracion_turno(empresa, turno.kilos_estimados))} con rampa libre '
                f'para {kg(turno.kilos_estimados)} en los próximos 30 días en {empresa.nombre}.'
            )

        turno.fecha_entrega = fecha_asignada
        turno.recorredor = recorredor
        turno.fue_confirmado = True
        turno.save(update_fields=['estado', 'recorredor', 'fecha_entrega', 'fue_confirmado'])
        reservar_bloque(turno, inicio, fin, rampa)
        # NOTA: NO se emite comprobante al confirmar el turno. El comprobante solo se crea en la descarga.
    return turno


def cancelar_turnos_vencidos(hoy=None) -> int:
    """
    Marca como CANCELADO los turnos en estado CONFIRMADO cuya fecha_entrega
    sea anterior a `hoy` (ausencia del productor). Marca `cancelado_por_ausencia=True`
    y conserva `fue_confirmado=True` para diferenciarlo de una cancelación manual.
    """
    hoy = hoy or _hoy()
    try:
        with transaction.atomic():
            vencidos = Turno.objects.filter(estado=EstadoTurno.CONFIRMADO, fecha_entrega__lt=hoy)
            OcupacionRampa.objects.filter(turno__in=vencidos).delete()
            return vencidos.update(estado=EstadoTurno.CANCELADO, cancelado_por_ausencia=True)
    except OperationalError as error:
        # Se ejecuta al abrir varias pantallas: si la base está ocupada se
        # reintenta en la próxima carga en lugar de mostrar un error.
        if not _es_bloqueo(error):
            raise
        logger.warning('No se cancelaron turnos vencidos (base bloqueada): %s', error)
        return 0


def _texto_turno(turno) -> str:
    if turno.fecha_entrega and turno.horario_display:
        rampa = f', {turno.rampa}' if turno.rampa_id else ''
        return f'{turno.codigo} del {turno.fecha_entrega:%d/%m/%Y} ({turno.horario_display} hs{rampa})'
    return f'{turno.codigo} (pendiente de asignación)'


@sin_bloqueos
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
    with transaction.atomic():
        turno.save(update_fields=['estado'])
        liberar_rampa(turno)

    if not era_confirmado:
        return []
    texto = (
        f'SOLTAT · {productor.get_nombre_completo()} canceló el turno confirmado '
        f'{_texto_turno(turno)}. Motivo: {mensaje}'
    )
    destinatarios = [turno.recorredor] if turno.recorredor else list(turno.empresa.recorredores.all())
    destinatarios.append(turno.empresa)
    return [armar_aviso(d, texto) for d in destinatarios]


@sin_bloqueos
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
        liberar_rampa(turno)
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
@sin_bloqueos
def registrar_ingreso_garita(operador, turno, momento=None, patente_vehiculo=None) -> Turno:
    momento = momento or timezone.now()
    if turno.fecha_entrega != timezone.localtime(momento).date():
        raise TurnoError(f'El turno {turno.codigo} es para el {turno.fecha_entrega:%d/%m/%Y}, no para hoy.')
    if EstadoTurno.EN_PLAYA not in turno.TRANSICIONES.get(turno.estado, []):
        raise TurnoError(f'Solo pueden ingresar turnos confirmados (estado actual: {turno.get_estado_display()}).')

    update_fields = ['estado', 'hora_arribo_playa']
    if not (turno.patente_vehiculo or '').strip():
        patente_limpia = (patente_vehiculo or '').strip().upper()
        if not patente_limpia:
            raise TurnoError('El turno no tiene patente registrada. Debe ingresar la patente del vehículo para registrar la llegada.')
        turno.patente_vehiculo = patente_limpia
        update_fields.append('patente_vehiculo')

    turno.cambiar_estado(EstadoTurno.EN_PLAYA)
    turno.hora_arribo_playa = momento
    turno.save(update_fields=update_fields)
    return turno


@sin_bloqueos
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
@sin_bloqueos
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
    turnos = list(productor.turnos.select_related('empresa').order_by(*ORDEN_RECIENTES))
    descargados = [t for t in turnos if t.estado == EstadoTurno.DESCARGADO]
    # Turnos que debían presentarse: los que llegaron a playa y los confirmados vencidos.
    presentados = [t for t in turnos if t.estado in (EstadoTurno.EN_PLAYA, EstadoTurno.DESCARGADO)]
    ausentes = [
        t for t in turnos
        if (t.estado == EstadoTurno.CONFIRMADO and t.fecha_entrega and t.fecha_entrega < hoy)
        or t.cancelado_por_ausencia
    ]
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
