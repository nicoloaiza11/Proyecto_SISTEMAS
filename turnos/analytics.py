"""
Motor de analítica del Sistema de Apoyo a la Decisión (SAD) de SOLTAT.

Calcula, a partir de la trazabilidad de cada turno, los indicadores que un
gerente de área necesita para decidir:
  · Qué se ganó  → volumen recibido, monto liquidado, precio promedio.
  · Qué se perdió → capacidad ociosa, anulaciones, sobre-reserva, merma y calidad.
  · Qué tan rápido → espera en planta, recorrido finca→garita, ciclo total, ritmo kg/h.
  · Qué conviene hacer → recomendaciones automáticas basadas en reglas.

Toda la lógica vive aquí (y no en las vistas) para poder testearla y reutilizarla.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from statistics import mean

from django.utils import timezone

from .models import Turno, CLASES_TABACO, PRECIO_MAXIMO_USD

DIAS_OPERATIVOS_SEMANA = {0, 1, 2, 3, 4, 5}   # Lunes a sábado
HORAS_OPERATIVAS_DIA = 10                      # 08:00 a 18:00
PERIODOS_VALIDOS = (7, 30, 60, 90)
FRANJAS = [codigo for codigo, _ in Turno.FRANJAS_HORARIAS]
NOMBRE_CLASE = dict(CLASES_TABACO)


# ===========================================================================
# Utilidades de cálculo y formato
# ===========================================================================
def resolver_periodo(param, por_defecto=30):
    """Devuelve (dias, desde, hasta) a partir del parámetro ?periodo= de la URL."""
    try:
        dias = int(param)
    except (TypeError, ValueError):
        dias = por_defecto
    if dias not in PERIODOS_VALIDOS:
        dias = por_defecto
    hasta = timezone.localdate()
    desde = hasta - timedelta(days=dias - 1)
    return dias, desde, hasta


def _promedio(valores):
    v = [x for x in valores if x is not None]
    return round(mean(v), 1) if v else None


def _percentil(valores, p):
    v = sorted(x for x in valores if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return round(v[f] + (v[c] - v[f]) * (k - f), 1)


def _pct(parte, total):
    return round(parte * 100.0 / total, 1) if total else None


def _variacion(actual, anterior):
    if actual is None or anterior in (None, 0):
        return None
    return round((actual - anterior) * 100.0 / abs(anterior), 1)


def _dias_operativos(desde, hasta):
    dias, d = [], desde
    while d <= hasta:
        if d.weekday() in DIAS_OPERATIVOS_SEMANA:
            dias.append(d)
        d += timedelta(days=1)
    return dias


def fmt_num(valor, decimales=0):
    """Formato numérico argentino: 1.234.567,8"""
    if valor is None:
        return '—'
    texto = f"{float(valor):,.{decimales}f}"
    return texto.replace(',', 'X').replace('.', ',').replace('X', '.')


def fmt_min(minutos):
    """Convierte minutos a una lectura humana: '1 h 25 min' / '48 min'."""
    if minutos is None:
        return '—'
    minutos = int(round(minutos))
    if minutos < 60:
        return f"{minutos} min"
    h, m = divmod(minutos, 60)
    return f"{h} h {m:02d} min"


def _kpi(clave, etiqueta, valor, valor_fmt, unidad='', anterior=None, mejor_si='sube',
         icono='bi-graph-up', ayuda='', comparativo=None):
    """Estructura homogénea de tarjeta KPI con variación contra el período anterior."""
    variacion = _variacion(valor, anterior)
    tono = 'neutral'
    if variacion is not None and abs(variacion) >= 0.5:
        sube = variacion > 0
        tono = 'positivo' if (sube and mejor_si == 'sube') or (not sube and mejor_si == 'baja') else 'negativo'
    return {
        'clave': clave,
        'etiqueta': etiqueta,
        'valor': valor,
        'valor_fmt': valor_fmt,
        'unidad': unidad,
        'variacion': variacion,
        'variacion_fmt': (f"{'+' if variacion > 0 else ''}{fmt_num(variacion, 1)}%" if variacion is not None else None),
        'tono': tono,
        'icono': icono,
        'ayuda': ayuda,
        'comparativo': comparativo,
    }


# ===========================================================================
# Métricas base sobre una lista de turnos
# ===========================================================================
def _metricas_basicas(turnos):
    descargados = [t for t in turnos if t.estado == Turno.ESTADO_DESCARGADO and t.kilos_reales]
    anulados = [t for t in turnos if t.estado == Turno.ESTADO_CANCELADO]
    con_arribo = [t for t in turnos if t.hora_arribo_playa]
    a_tiempo = [t for t in con_arribo if t.arribo_en_banda]

    kg_rec = sum(t.kilos_reales for t in descargados)
    kg_est_desc = sum(t.kilos_estimados for t in descargados)
    monto = sum((t.importe_usd for t in descargados), Decimal('0'))
    cerrados = len(descargados) + len(anulados)

    return {
        'total_turnos': len(turnos),
        'entregas': len(descargados),
        'anulados': len(anulados),
        'kg_recibidos': kg_rec,
        'kg_estimados_descargados': kg_est_desc,
        'kg_anulados': sum(t.kilos_estimados for t in anulados),
        'monto_usd': float(monto),
        'precio_promedio': round(float(monto) / kg_rec, 2) if kg_rec else None,
        'desvio_estimacion_pct': _pct(kg_rec - kg_est_desc, kg_est_desc),
        'sobre_reserva_kg': sum(max(0, t.kilos_estimados - t.kilos_reales) for t in descargados),
        'perdida_merma_usd': float(sum((t.perdida_merma_usd for t in descargados), Decimal('0'))),
        'perdida_clase_usd': float(sum((t.perdida_clase_usd for t in descargados), Decimal('0'))),
        'espera_prom': _promedio([t.minutos_espera for t in descargados]),
        'espera_p90': _percentil([t.minutos_espera for t in descargados], 0.9),
        'viaje_prom': _promedio([t.minutos_viaje for t in descargados]),
        'ciclo_prom': _promedio([t.minutos_ciclo for t in descargados]),
        'puntualidad_pct': _pct(len(a_tiempo), len(con_arribo)),
        'ausentismo_pct': _pct(len(anulados), cerrados),
    }


def _por_franja(turnos):
    filas = []
    for franja in FRANJAS:
        grupo = [t for t in turnos if t.franja_horaria == franja]
        desc = [t for t in grupo if t.estado == Turno.ESTADO_DESCARGADO]
        con_arribo = [t for t in grupo if t.hora_arribo_playa]
        filas.append({
            'franja': franja,
            'turnos': len(grupo),
            'kg': sum(t.kilos_reales or 0 for t in desc),
            'espera_prom': _promedio([t.minutos_espera for t in desc]),
            'puntualidad_pct': _pct(sum(1 for t in con_arribo if t.arribo_en_banda), len(con_arribo)),
        })
    return filas


def _por_clase(turnos):
    desc = [t for t in turnos if t.estado == Turno.ESTADO_DESCARGADO and t.clase_tabaco]
    total_kg = sum(t.kilos_reales for t in desc)
    filas = []
    for codigo, nombre in CLASES_TABACO:
        grupo = [t for t in desc if t.clase_tabaco == codigo]
        kg = sum(t.kilos_reales for t in grupo)
        filas.append({
            'clase': codigo,
            'nombre': nombre,
            'kg': kg,
            'pct': _pct(kg, total_kg) or 0,
            'monto': float(sum((t.importe_usd for t in grupo), Decimal('0'))),
        })
    return filas


def _serie_diaria(turnos, dias):
    por_dia = defaultdict(list)
    for t in turnos:
        por_dia[t.fecha_entrega].append(t)
    serie = {'labels': [], 'reservado': [], 'recibido': [], 'espera': [], 'viaje': [], 'monto': []}
    for d in dias:
        grupo = por_dia.get(d, [])
        desc = [t for t in grupo if t.estado == Turno.ESTADO_DESCARGADO]
        serie['labels'].append(d.strftime('%d/%m'))
        serie['reservado'].append(sum(t.kilos_estimados for t in grupo if t.estado != Turno.ESTADO_CANCELADO))
        serie['recibido'].append(sum(t.kilos_reales or 0 for t in desc))
        serie['espera'].append(_promedio([t.minutos_espera for t in desc]))
        serie['viaje'].append(_promedio([t.minutos_viaje for t in desc]))
        serie['monto'].append(round(float(sum((t.importe_usd for t in desc), Decimal('0'))), 2))
    return serie


# ===========================================================================
# TABLERO GERENCIAL · EMPRESA DE ACOPIO
# ===========================================================================
def _turnos_empresa(empresa, desde, hasta):
    return list(
        Turno.objects.filter(empresa=empresa, fecha_entrega__range=(desde, hasta))
        .select_related('productor')
    )


def _uso_capacidad(turnos, empresa, dias):
    """Descompone la capacidad total del período en: recibido, en curso y pérdidas."""
    capacidad = empresa.capacidad_diaria_kg * len(dias)
    recibido = sum(t.kilos_reales or 0 for t in turnos if t.estado == Turno.ESTADO_DESCARGADO)
    en_curso = sum(t.kilos_estimados for t in turnos if t.estado in (Turno.ESTADO_CONFIRMADO, Turno.ESTADO_EN_PLAYA))
    sobre_reserva = sum(
        max(0, t.kilos_estimados - (t.kilos_reales or 0))
        for t in turnos if t.estado == Turno.ESTADO_DESCARGADO
    )
    anulado = sum(t.kilos_estimados for t in turnos if t.estado == Turno.ESTADO_CANCELADO)
    sin_reservar = max(0, capacidad - recibido - en_curso - sobre_reserva - anulado)
    return {
        'capacidad': capacidad,
        'recibido': recibido,
        'en_curso': en_curso,
        'sobre_reserva': sobre_reserva,
        'anulado': anulado,
        'sin_reservar': sin_reservar,
        'perdido_total': sobre_reserva + anulado + sin_reservar,
    }


def _ranking_productores(turnos):
    grupos = defaultdict(list)
    for t in turnos:
        grupos[t.productor].append(t)
    filas = []
    for productor, lista in grupos.items():
        m = _metricas_basicas(lista)
        filas.append({
            'productor': productor,
            'nombre': productor.razon_social or productor.username,
            'cuit': productor.cuit_dni,
            **m,
        })
    filas.sort(key=lambda f: f['kg_recibidos'], reverse=True)
    return filas


def _recomendaciones_empresa(m, uso, franjas, ranking, clases, empresa):
    recs = []
    precio = m['precio_promedio'] or float(PRECIO_MAXIMO_USD) * 0.8

    # 1. Utilización de capacidad
    util = m.get('utilizacion_pct') or 0
    if util < 70:
        recs.append({
            'nivel': 'oportunidad', 'icono': 'bi-box-arrow-in-down',
            'titulo': f'Capacidad ociosa del {fmt_num(100 - util, 1)}%',
            'texto': f"Quedaron {fmt_num(uso['perdido_total'])} kg de capacidad sin aprovechar en el período "
                     f"(≈ USD {fmt_num(uso['perdido_total'] * precio)} de compra potencial).",
            'accion': 'Habilitar sobre-reserva controlada (5-8%) o promover turnos en días de baja demanda.',
        })
    elif util > 92:
        recs.append({
            'nivel': 'riesgo', 'icono': 'bi-exclamation-octagon',
            'titulo': f'Planta al límite ({fmt_num(util, 1)}% de utilización)',
            'texto': 'La recepción opera prácticamente al tope y cualquier demora se traslada a la ruta.',
            'accion': 'Evaluar ampliar el cupo diario o abrir una banda extra 18:00-20:00 en picos de cosecha.',
        })
    else:
        recs.append({
            'nivel': 'positivo', 'icono': 'bi-check2-circle',
            'titulo': f'Utilización saludable ({fmt_num(util, 1)}%)',
            'texto': 'La planta trabaja con margen suficiente para absorber imprevistos sin saturar el playón.',
            'accion': 'Mantener la política actual de cupos.',
        })

    # 2. Congestión por banda horaria
    con_espera = [f for f in franjas if f['espera_prom'] is not None and f['turnos'] >= 3]
    if len(con_espera) >= 2:
        peor = max(con_espera, key=lambda f: f['espera_prom'])
        mejor = min(con_espera, key=lambda f: f['espera_prom'])
        if peor['espera_prom'] > mejor['espera_prom'] * 1.3:
            recs.append({
                'nivel': 'alerta', 'icono': 'bi-hourglass-split',
                'titulo': f"Cuello de botella en la banda {peor['franja']}",
                'texto': f"Espera promedio de {fmt_min(peor['espera_prom'])} frente a {fmt_min(mejor['espera_prom'])} "
                         f"en {mejor['franja']}. Concentra {peor['turnos']} turnos.",
                'accion': f"Reducir el cupo de {peor['franja']} un 15-20% y trasladarlo a {mejor['franja']}.",
            })

    # 3. Sobre-reserva / estimación de productores
    desvio = m['desvio_estimacion_pct']
    if desvio is not None and desvio < -4:
        recs.append({
            'nivel': 'alerta', 'icono': 'bi-arrow-down-up',
            'titulo': f'Los productores sobre-estiman un {fmt_num(abs(desvio), 1)}%',
            'texto': f"Se bloquearon {fmt_num(uso['sobre_reserva'])} kg de cupo que nunca llegaron a báscula, "
                     f"impidiendo que otros productores reservaran.",
            'accion': 'Fijar una tolerancia de ±5% en la declaración y penalizar desvíos reiterados.',
        })

    # 4. Ausentismo
    aus = m['ausentismo_pct'] or 0
    if aus > 6:
        recs.append({
            'nivel': 'riesgo', 'icono': 'bi-calendar-x',
            'titulo': f'Ausentismo del {fmt_num(aus, 1)}%',
            'texto': f"{m['anulados']} turnos anulados liberaron tarde {fmt_num(m['kg_anulados'])} kg de cupo.",
            'accion': 'Exigir confirmación del turno 24 h antes y liberar automáticamente los no confirmados.',
        })

    # 5. Puntualidad
    punt = m['puntualidad_pct']
    if punt is not None and punt < 80:
        recs.append({
            'nivel': 'alerta', 'icono': 'bi-stopwatch',
            'titulo': f'Solo el {fmt_num(punt, 1)}% llega dentro de su banda',
            'texto': 'Los arribos fuera de horario desordenan la secuencia del playón y elevan la espera.',
            'accion': 'Enviar recordatorio 2 h antes y reubicar al final de la cola a quienes lleguen tarde.',
        })

    # 6. Productor crítico
    candidatos = [r for r in ranking if r['entregas'] >= 3 and r['desvio_estimacion_pct'] is not None]
    if candidatos:
        critico = min(candidatos, key=lambda r: r['desvio_estimacion_pct'])
        if critico['desvio_estimacion_pct'] < -8:
            recs.append({
                'nivel': 'info', 'icono': 'bi-person-exclamation',
                'titulo': f"Revisar declaraciones de {critico['nombre']}",
                'texto': f"Entrega en promedio {fmt_num(abs(critico['desvio_estimacion_pct']), 1)}% menos de lo que reserva "
                         f"({critico['entregas']} entregas en el período).",
                'accion': 'Contactar al productor y ajustar su cupo máximo por turno.',
            })

    # 7. Calidad
    baja = sum(c['pct'] for c in clases if c['clase'] in ('C', 'D'))
    if baja > 35:
        recs.append({
            'nivel': 'info', 'icono': 'bi-award',
            'titulo': f'{fmt_num(baja, 1)}% del volumen ingresó en clases C/D',
            'texto': 'Una alta proporción de calidad inferior reduce el valor del stock acopiado.',
            'accion': 'Reforzar asistencia técnica de curado con los productores de mayor volumen C/D.',
        })

    orden = {'riesgo': 0, 'alerta': 1, 'oportunidad': 2, 'info': 3, 'positivo': 4}
    recs.sort(key=lambda r: orden.get(r['nivel'], 9))
    return recs


def analitica_empresa(empresa, dias, desde, hasta):
    turnos = _turnos_empresa(empresa, desde, hasta)
    prev_hasta = desde - timedelta(days=1)
    prev_desde = prev_hasta - timedelta(days=dias - 1)
    previos = _turnos_empresa(empresa, prev_desde, prev_hasta)

    dias_op = _dias_operativos(desde, hasta)
    dias_op_prev = _dias_operativos(prev_desde, prev_hasta)

    m = _metricas_basicas(turnos)
    mp = _metricas_basicas(previos)

    uso = _uso_capacidad(turnos, empresa, dias_op)
    uso_prev = _uso_capacidad(previos, empresa, dias_op_prev)
    m['utilizacion_pct'] = _pct(uso['recibido'], uso['capacidad']) or 0
    mp['utilizacion_pct'] = _pct(uso_prev['recibido'], uso_prev['capacidad'])

    dias_con_descarga = len({t.fecha_entrega for t in turnos if t.estado == Turno.ESTADO_DESCARGADO})
    dias_con_descarga_prev = len({t.fecha_entrega for t in previos if t.estado == Turno.ESTADO_DESCARGADO})
    m['ritmo_kg_h'] = round(m['kg_recibidos'] / (dias_con_descarga * HORAS_OPERATIVAS_DIA)) if dias_con_descarga else None
    mp['ritmo_kg_h'] = round(mp['kg_recibidos'] / (dias_con_descarga_prev * HORAS_OPERATIVAS_DIA)) if dias_con_descarga_prev else None

    precio_ref = m['precio_promedio'] or 0
    perdida_usd = uso['perdido_total'] * precio_ref
    perdida_usd_prev = uso_prev['perdido_total'] * (mp['precio_promedio'] or precio_ref)

    kpis = [
        _kpi('kg', 'Tabaco recibido', m['kg_recibidos'], fmt_num(m['kg_recibidos']), 'kg',
             mp['kg_recibidos'], 'sube', 'bi-box-seam',
             'Kilos netos registrados en báscula en el período.'),
        _kpi('monto', 'Monto liquidado', m['monto_usd'], fmt_num(m['monto_usd']), 'USD',
             mp['monto_usd'], 'sube', 'bi-cash-stack',
             'Valor de compra: kilos netos × precio de la clase asignada.'),
        _kpi('util', 'Utilización de capacidad', m['utilizacion_pct'], fmt_num(m['utilizacion_pct'], 1), '%',
             mp['utilizacion_pct'], 'sube', 'bi-speedometer2',
             f"Recibido sobre la capacidad de {len(dias_op)} jornadas operativas."),
        _kpi('perdida', 'Capacidad no aprovechada', perdida_usd, fmt_num(perdida_usd), 'USD',
             perdida_usd_prev, 'baja', 'bi-graph-down-arrow',
             f"{fmt_num(uso['perdido_total'])} kg perdidos por cupo sin reservar, anulaciones y sobre-reserva."),
        _kpi('espera', 'Espera media en planta', m['espera_prom'], fmt_min(m['espera_prom']), '',
             mp['espera_prom'], 'baja', 'bi-hourglass-split',
             f"Arribo a garita → cierre de descarga. P90: {fmt_min(m['espera_p90'])}."),
        _kpi('ciclo', 'Recorrido total medio', m['ciclo_prom'], fmt_min(m['ciclo_prom']), '',
             mp['ciclo_prom'], 'baja', 'bi-signpost-split',
             f"Salida de finca → descarga. Viaje medio: {fmt_min(m['viaje_prom'])}."),
        _kpi('ritmo', 'Ritmo de descarga', m['ritmo_kg_h'], fmt_num(m['ritmo_kg_h']), 'kg/h',
             mp['ritmo_kg_h'], 'sube', 'bi-lightning-charge',
             'Kilos recibidos por hora de operación de la planta.'),
        _kpi('punt', 'Puntualidad de arribo', m['puntualidad_pct'], fmt_num(m['puntualidad_pct'], 1), '%',
             mp['puntualidad_pct'], 'sube', 'bi-stopwatch',
             'Unidades que llegaron antes del cierre de su banda horaria.'),
    ]

    franjas = _por_franja(turnos)
    clases = _por_clase(turnos)
    ranking = _ranking_productores(turnos)

    movimientos = sorted(
        [t for t in turnos if t.estado != Turno.ESTADO_CONFIRMADO],
        key=lambda t: (t.fecha_entrega, t.franja_horaria),
        reverse=True,
    )[:80]

    graficos = {
        'diaria': {**_serie_diaria(turnos, dias_op), 'capacidad': empresa.capacidad_diaria_kg},
        'uso': {
            'labels': ['Recibido', 'En curso', 'Sobre-reserva no entregada', 'Anulaciones', 'Cupo sin reservar'],
            'valores': [uso['recibido'], uso['en_curso'], uso['sobre_reserva'], uso['anulado'], uso['sin_reservar']],
        },
        'franjas': {
            'labels': [f['franja'] for f in franjas],
            'espera': [f['espera_prom'] for f in franjas],
            'turnos': [f['turnos'] for f in franjas],
        },
        'clases': {
            'labels': [c['nombre'] for c in clases],
            'kg': [c['kg'] for c in clases],
        },
    }

    return {
        'metricas': m,
        'metricas_previas': mp,
        'kpis': kpis,
        'uso': uso,
        'franjas': franjas,
        'clases': clases,
        'ranking': ranking[:10],
        'movimientos': movimientos,
        'recomendaciones': _recomendaciones_empresa(m, uso, franjas, ranking, clases, empresa),
        'graficos': graficos,
        'dias_operativos': len(dias_op),
        'periodo_anterior': (prev_desde, prev_hasta),
    }


# ===========================================================================
# TABLERO DE RESULTADOS · PRODUCTOR
# ===========================================================================
def _recomendaciones_productor(m, bench, franjas_propias, franjas_planta, plantas, clases):
    recs = []

    # 1. Mejor banda horaria según la espera real de la planta
    candidatas = [f for f in franjas_planta if f['espera_prom'] is not None and f['turnos'] >= 3]
    if candidatas:
        mejor = min(candidatas, key=lambda f: f['espera_prom'])
        usadas = [f for f in franjas_propias if f['turnos'] > 0 and f['espera_prom'] is not None]
        habitual = max(usadas, key=lambda f: f['turnos']) if usadas else None
        if habitual and habitual['franja'] != mejor['franja'] and habitual['espera_prom'] > mejor['espera_prom'] * 1.2:
            ahorro = habitual['espera_prom'] - mejor['espera_prom']
            recs.append({
                'nivel': 'oportunidad', 'icono': 'bi-clock-history',
                'titulo': f"Reservar en la banda {mejor['franja']}",
                'texto': f"Su banda habitual ({habitual['franja']}) promedia {fmt_min(habitual['espera_prom'])} de espera; "
                         f"la de {mejor['franja']} solo {fmt_min(mejor['espera_prom'])}.",
                'accion': f"Ahorro estimado de {fmt_min(ahorro)} por viaje en playón.",
            })

    # 2. Precisión de la declaración
    desvio = m['desvio_estimacion_pct']
    if desvio is not None and desvio < -4:
        recs.append({
            'nivel': 'alerta', 'icono': 'bi-arrow-down-up',
            'titulo': f'Declara un {fmt_num(abs(desvio), 1)}% más de lo que entrega',
            'texto': f"Reservó {fmt_num(m['sobre_reserva_kg'])} kg que no llegaron a báscula. "
                     f"Las plantas pueden reducir su cupo por desvíos reiterados.",
            'accion': 'Pesar una muestra de fardos antes de reservar y declarar con margen de ±5%.',
        })
    elif desvio is not None:
        recs.append({
            'nivel': 'positivo', 'icono': 'bi-bullseye',
            'titulo': 'Declaraciones precisas',
            'texto': f"Su desvío entre lo reservado y lo entregado es de {fmt_num(desvio, 1)}%.",
            'accion': 'Mantener el método de estimación actual.',
        })

    # 3. Calidad
    if m['perdida_clase_usd'] and m['kg_recibidos']:
        pct_a = next((c['pct'] for c in clases if c['clase'] == 'A'), 0)
        recs.append({
            'nivel': 'info', 'icono': 'bi-award',
            'titulo': f"USD {fmt_num(m['perdida_clase_usd'])} por debajo de Clase A",
            'texto': f"El {fmt_num(pct_a, 1)}% de su tabaco se clasificó como Clase A. "
                     f"Cada punto de mejora en curado impacta directamente en su liquidación.",
            'accion': 'Solicitar asistencia técnica de curado y clasificar en finca antes de cargar.',
        })

    # 4. Espera comparada con la planta
    if m['espera_prom'] and bench['espera_prom'] and m['espera_prom'] > bench['espera_prom'] * 1.15:
        recs.append({
            'nivel': 'alerta', 'icono': 'bi-hourglass-split',
            'titulo': 'Espera mayor que el promedio de la planta',
            'texto': f"Usted espera {fmt_min(m['espera_prom'])} frente a {fmt_min(bench['espera_prom'])} del resto de los productores.",
            'accion': 'Llegar al inicio de la banda asignada y evitar las bandas de media mañana.',
        })

    # 5. Mejor planta (precio y espera)
    if len(plantas) >= 2:
        mejor_precio = max(plantas, key=lambda p: p['precio_promedio'] or 0)
        recs.append({
            'nivel': 'info', 'icono': 'bi-building-check',
            'titulo': f"Mejor precio promedio: {mejor_precio['nombre']}",
            'texto': f"USD {fmt_num(mejor_precio['precio_promedio'], 2)}/kg con espera media de {fmt_min(mejor_precio['espera_prom'])}.",
            'accion': 'Priorizar esta planta para los lotes de mejor calidad.',
        })

    # 6. Puntualidad
    if m['puntualidad_pct'] is not None and m['puntualidad_pct'] < 80:
        recs.append({
            'nivel': 'riesgo', 'icono': 'bi-stopwatch',
            'titulo': f"Puntualidad del {fmt_num(m['puntualidad_pct'], 1)}%",
            'texto': 'Los arribos fuera de banda pueden ser reubicados al final de la cola del playón.',
            'accion': f"Salir de finca con {fmt_min((m['viaje_prom'] or 60) + 20)} de anticipación al inicio de la banda.",
        })

    orden = {'riesgo': 0, 'alerta': 1, 'oportunidad': 2, 'info': 3, 'positivo': 4}
    recs.sort(key=lambda r: orden.get(r['nivel'], 9))
    return recs


def analitica_productor(productor, dias, desde, hasta):
    base = Turno.objects.filter(productor=productor).select_related('empresa')
    turnos = list(base.filter(fecha_entrega__range=(desde, hasta)))

    prev_hasta = desde - timedelta(days=1)
    prev_desde = prev_hasta - timedelta(days=dias - 1)
    previos = list(base.filter(fecha_entrega__range=(prev_desde, prev_hasta)))

    m = _metricas_basicas(turnos)
    mp = _metricas_basicas(previos)

    # Referencia: resto de los productores en las mismas plantas y período
    empresas_ids = {t.empresa_id for t in turnos}
    planta = list(
        Turno.objects.filter(empresa_id__in=empresas_ids, fecha_entrega__range=(desde, hasta))
        .exclude(productor=productor)
    )
    bench = _metricas_basicas(planta)

    def comparar(valor, ref, fmt, mejor_si):
        if valor is None or ref is None:
            return None
        mejor = (valor >= ref) if mejor_si == 'sube' else (valor <= ref)
        return {'texto': f"Planta: {fmt(ref)}", 'mejor': mejor}

    perdida_total = m['perdida_merma_usd'] + m['perdida_clase_usd']
    perdida_total_prev = mp['perdida_merma_usd'] + mp['perdida_clase_usd']

    kpis = [
        _kpi('monto', 'Ingresos liquidados', m['monto_usd'], fmt_num(m['monto_usd']), 'USD',
             mp['monto_usd'], 'sube', 'bi-cash-stack',
             'Kilos netos × precio de la clase asignada en báscula.'),
        _kpi('kg', 'Tabaco entregado', m['kg_recibidos'], fmt_num(m['kg_recibidos']), 'kg',
             mp['kg_recibidos'], 'sube', 'bi-box-seam',
             f"{m['entregas']} entregas descargadas en el período."),
        _kpi('precio', 'Precio promedio', m['precio_promedio'], fmt_num(m['precio_promedio'], 2), 'USD/kg',
             mp['precio_promedio'], 'sube', 'bi-tag',
             'Refleja la calidad promedio clasificada.',
             comparar(m['precio_promedio'], bench['precio_promedio'], lambda v: f"USD {fmt_num(v, 2)}", 'sube')),
        _kpi('perdida', 'Valor no percibido', perdida_total, fmt_num(perdida_total), 'USD',
             perdida_total_prev, 'baja', 'bi-graph-down-arrow',
             f"Merma: USD {fmt_num(m['perdida_merma_usd'])} · Calidad vs Clase A: USD {fmt_num(m['perdida_clase_usd'])}."),
        _kpi('espera', 'Espera media en planta', m['espera_prom'], fmt_min(m['espera_prom']), '',
             mp['espera_prom'], 'baja', 'bi-hourglass-split',
             'Desde el arribo a garita hasta el cierre de descarga.',
             comparar(m['espera_prom'], bench['espera_prom'], fmt_min, 'baja')),
        _kpi('viaje', 'Viaje finca → planta', m['viaje_prom'], fmt_min(m['viaje_prom']), '',
             mp['viaje_prom'], 'baja', 'bi-truck',
             'Tiempo medio entre la salida informada y el arribo a garita.'),
        _kpi('ciclo', 'Recorrido total medio', m['ciclo_prom'], fmt_min(m['ciclo_prom']), '',
             mp['ciclo_prom'], 'baja', 'bi-signpost-split',
             'Puerta a puerta: salida de finca → descarga terminada.',
             comparar(m['ciclo_prom'], bench['ciclo_prom'], fmt_min, 'baja')),
        _kpi('punt', 'Puntualidad', m['puntualidad_pct'], fmt_num(m['puntualidad_pct'], 1), '%',
             mp['puntualidad_pct'], 'sube', 'bi-stopwatch',
             'Arribos dentro de la banda horaria reservada.',
             comparar(m['puntualidad_pct'], bench['puntualidad_pct'], lambda v: f"{fmt_num(v, 1)}%", 'sube')),
    ]

    franjas_propias = _por_franja(turnos)
    franjas_planta = _por_franja(planta)
    clases = _por_clase(turnos)

    plantas = []
    por_empresa = defaultdict(list)
    for t in turnos:
        por_empresa[t.empresa].append(t)
    for empresa, lista in por_empresa.items():
        pm = _metricas_basicas(lista)
        plantas.append({'nombre': empresa.nombre, **pm})
    plantas.sort(key=lambda p: p['monto_usd'], reverse=True)

    entregas = sorted(
        [t for t in turnos if t.estado == Turno.ESTADO_DESCARGADO],
        key=lambda t: (t.fecha_entrega, t.franja_horaria),
    )
    ultimas = entregas[-20:]

    movimientos = sorted(
        [t for t in turnos if t.estado != Turno.ESTADO_CONFIRMADO],
        key=lambda t: (t.fecha_entrega, t.franja_horaria),
        reverse=True,
    )[:60]

    graficos = {
        'entregas': {
            'labels': [t.fecha_entrega.strftime('%d/%m') for t in ultimas],
            'codigos': [t.codigo for t in ultimas],
            'importe': [float(t.importe_usd) for t in ultimas],
            'precio': [float(t.precio_kg or 0) for t in ultimas],
            'estimado': [t.kilos_estimados for t in ultimas],
            'real': [t.kilos_reales for t in ultimas],
            'viaje': [t.minutos_viaje for t in ultimas],
            'espera': [t.minutos_espera for t in ultimas],
        },
        'franjas': {
            'labels': FRANJAS,
            'propia': [f['espera_prom'] for f in franjas_propias],
            'planta': [f['espera_prom'] for f in franjas_planta],
        },
        'clases': {
            'labels': [c['nombre'] for c in clases],
            'kg': [c['kg'] for c in clases],
        },
    }

    return {
        'metricas': m,
        'metricas_previas': mp,
        'benchmark': bench,
        'kpis': kpis,
        'clases': clases,
        'plantas': plantas,
        'movimientos': movimientos,
        'recomendaciones': _recomendaciones_productor(m, bench, franjas_propias, franjas_planta, plantas, clases),
        'graficos': graficos,
        'periodo_anterior': (prev_desde, prev_hasta),
    }

