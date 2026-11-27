"""
Datos para los gráficos (Chart.js) del rendimiento del productor y del
panel de la empresa. Cada función devuelve diccionarios simples que la
vista pasa al template con `json_script`; static/js/graficos.js los dibuja.
"""
from datetime import timedelta

from django.db.models import Sum
from django.db.models.functions import Coalesce

from .choices import CorteTabaco, EstadoTurno
from .models import Turno

DIAS_HISTORIAL_EMPRESA = 14


def _kilos_por_corte(turnos_descargados) -> dict:
    """{'etiquetas': [...], 'valores': [...]} con los kilos reales de cada corte."""
    totales = dict(
        turnos_descargados.order_by().values_list('corte_tabaco')
        .annotate(total=Coalesce(Sum('kilos_reales'), 0))
    )
    return {
        'etiquetas': [etiqueta for _, etiqueta in CorteTabaco.choices],
        'valores': [totales.get(valor, 0) for valor, _ in CorteTabaco.choices],
    }


def _comprobante(turno):
    try:
        return turno.comprobante
    except Exception:  # sin comprobante (RelatedObjectDoesNotExist)
        return None


def _liquidacion(entregas) -> dict:
    con_precio = [(t, c) for t in entregas if (c := _comprobante(t)) and c.precio]
    return {
        'etiquetas': [f'{t.fecha_entrega:%d/%m}' for t, _ in con_precio],
        'detalle': [f'{t.codigo} · {t.empresa.nombre}' for t, _ in con_precio],
        'kilos': [c.peso_real for _, c in con_precio],
        'importes': [float(c.importe) for _, c in con_precio],
        'precios': [float(c.precio) for _, c in con_precio],
    }


# ---------------------------------------------------------------------------
# Productor
# ---------------------------------------------------------------------------
def graficos_productor(productor) -> dict:
    descargados = productor.turnos.filter(estado=EstadoTurno.DESCARGADO)
    entregas = list(
        descargados.select_related('empresa', 'comprobante')
        .order_by('fecha_entrega', 'hora_descarga', 'id')
    )
    etiquetas = [f'{t.fecha_entrega:%d/%m}' if t.fecha_entrega else t.codigo for t in entregas]
    detalle = [f'{t.codigo} · {t.empresa.nombre}' for t in entregas]
    return {
        'productorKilos': {
            'etiquetas': etiquetas,
            'detalle': detalle,
            'estimados': [t.kilos_estimados for t in entregas],
            'reales': [t.kilos_reales or 0 for t in entregas],
        },
        'productorCortes': _kilos_por_corte(descargados),
        # Resultado económico: importe liquidado y precio por kg de cada entrega
        # (solo las que tienen un precio mayor a cero en el comprobante).
        'productorLiquidacion': _liquidacion(entregas),
        'productorEspera': {
            'etiquetas': etiquetas,
            'detalle': detalle,
            'minutos': [t.calcular_tiempo_espera() for t in entregas],
        },
    }


# ---------------------------------------------------------------------------
# Empresa
# ---------------------------------------------------------------------------
def graficos_empresa(empresa, fecha, disponibilidad) -> dict:
    turnos = Turno.objects.filter(empresa=empresa)
    descargados = turnos.filter(estado=EstadoTurno.DESCARGADO)

    # Kilos recibidos (descargados en báscula) en los últimos días hasta la fecha elegida.
    desde = fecha - timedelta(days=DIAS_HISTORIAL_EMPRESA - 1)
    por_dia = dict(
        descargados.filter(fecha_entrega__range=(desde, fecha))
        .order_by().values_list('fecha_entrega')
        .annotate(total=Coalesce(Sum('kilos_reales'), 0))
    )
    dias = [desde + timedelta(days=i) for i in range(DIAS_HISTORIAL_EMPRESA)]

    return {
        'empresaDias': {
            'etiquetas': [f'{d:%d/%m}' for d in dias],
            'kilos': [por_dia.get(d, 0) for d in dias],
            'capacidad': empresa.capacidad_diaria_kg,
        },
        'empresaFranjas': {
            'etiquetas': [f['valor'] for f in disponibilidad['franjas']],
            'kilos': [f['ocupados'] for f in disponibilidad['franjas']],
            'capacidad': empresa.capacidad_franja_kg,
            'fecha': f'{fecha:%d/%m/%Y}',
        },
        'empresaCortes': _kilos_por_corte(descargados),
    }
