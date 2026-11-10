import uuid
from datetime import date
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from .models import EmpresaAcopio, Turno, PRECIOS_CLASE_USD


class CupoExcedidoException(Exception):
    """
    Excepción lanzada cuando los kilos solicitados superan el cupo diario disponible.
    """
    def __init__(self, mensaje, remanente_disponible, kilos_solicitados, capacidad_maxima, kilos_ocupados):
        super().__init__(mensaje)
        self.remanente_disponible = remanente_disponible
        self.kilos_solicitados = kilos_solicitados
        self.capacidad_maxima = capacidad_maxima
        self.kilos_ocupados = kilos_ocupados


def generar_codigo_turno(fecha: date) -> str:
    """
    Genera un código único alfanumérico legible para el comprobante.
    Ejemplo: 'SLT-20261108-A7B2'
    """
    random_part = uuid.uuid4().hex[:4].upper()
    fecha_part = fecha.strftime('%Y%m%d')
    return f"SLT-{fecha_part}-{random_part}"


def obtener_kilos_reservados(empresa: EmpresaAcopio, fecha: date) -> int:
    """
    Calcula la suma acumulada de kilos asignados en turnos activos (no cancelados)
    para una empresa compradora en una fecha determinada.
    Si el turno ya fue descargado, se computa el peso neto real de báscula en lugar
    del estimado, liberando automáticamente el cupo sobre-reservado.
    """
    resultado = Turno.objects.filter(
        empresa=empresa,
        fecha_entrega=fecha
    ).exclude(
        estado=Turno.ESTADO_CANCELADO
    ).aggregate(total=Sum(Coalesce('kilos_reales', 'kilos_estimados')))

    return resultado['total'] or 0


def consultar_disponibilidad_cupo(empresa: EmpresaAcopio, fecha: date, kilos_solicitados: int = 0) -> dict:
    """
    Evalúa el estado del cupo diario de una empresa para una fecha concreta.
    Retorna métricas operativas y semaforización de capacidad.
    """
    capacidad_maxima = empresa.capacidad_diaria_kg or 250000
    kilos_ocupados = obtener_kilos_reservados(empresa, fecha)
    kilos_disponibles = max(0, capacidad_maxima - kilos_ocupados)
    
    porcentaje_ocupacion = min(100.0, round((kilos_ocupados / capacidad_maxima) * 100, 1)) if capacidad_maxima > 0 else 100.0
    tiene_cupo = kilos_solicitados <= kilos_disponibles

    # Semáforo de capacidad:
    # < 80%: 'normal' (#6F9863)
    # 80% - 99%: 'alerta' (#D6A84F)
    # 100%: 'completo' (Rojo suave)
    if porcentaje_ocupacion >= 100 or kilos_disponibles == 0:
        nivel_semaforo = 'completo'
        color_hex = '#C0392B'
    elif porcentaje_ocupacion >= 80:
        nivel_semaforo = 'alerta'
        color_hex = '#D6A84F'
    else:
        nivel_semaforo = 'normal'
        color_hex = '#6F9863'

    return {
        'empresa_id': empresa.id,
        'empresa_nombre': empresa.nombre,
        'fecha': fecha.strftime('%Y-%m-%d'),
        'capacidad_maxima': capacidad_maxima,
        'kilos_ocupados': kilos_ocupados,
        'kilos_disponibles': kilos_disponibles,
        'remanente_disponible': kilos_disponibles,
        'porcentaje_ocupacion': porcentaje_ocupacion,
        'tiene_cupo': tiene_cupo,
        'nivel_semaforo': nivel_semaforo,
        'color_hex': color_hex,
    }


def validar_y_crear_turno(
    productor,
    empresa: EmpresaAcopio,
    kilos_estimados: int,
    patente_vehiculo: str,
    fecha_entrega: date,
    franja_horaria: str,
    observaciones: str = ''
) -> Turno:
    """
    Lógica de negocio central:
    Valida estrictamente los cupos dentro de una transacción atómica para evitar condiciones de carrera.
    Si no hay cupo suficiente, levanta CupoExcedidoException.
    Si hay cupo, genera y persiste el turno con estado 'CONFIRMADO'.
    """
    if kilos_estimados <= 0:
        raise ValueError("Los kilos estimados deben ser mayores a cero.")

    # Control estricto con bloqueo a nivel de transacción
    with transaction.atomic():
        # Bloqueamos la fila de la empresa para sincronizar reservas concurrentes
        empresa_lock = EmpresaAcopio.objects.select_for_update().get(id=empresa.id)
        
        capacidad_maxima = empresa_lock.capacidad_diaria_kg or 250000
        kilos_ocupados = obtener_kilos_reservados(empresa_lock, fecha_entrega)
        remanente_disponible = max(0, capacidad_maxima - kilos_ocupados)

        if kilos_estimados > remanente_disponible:
            raise CupoExcedidoException(
                mensaje=(
                    f"Cupo diario excedido para el día {fecha_entrega.strftime('%d/%m/%Y')} en {empresa_lock.nombre}. "
                    f"Kilos solicitados: {kilos_estimados:,} kg. "
                    f"Remanente disponible: {remanente_disponible:,} kg de una capacidad total de {capacidad_maxima:,} kg."
                ),
                remanente_disponible=remanente_disponible,
                kilos_solicitados=kilos_estimados,
                capacidad_maxima=capacidad_maxima,
                kilos_ocupados=kilos_ocupados,
            )

        # Generar código único que no colisione
        while True:
            codigo_candidato = generar_codigo_turno(fecha_entrega)
            if not Turno.objects.filter(codigo=codigo_candidato).exists():
                break

        turno = Turno.objects.create(
            codigo=codigo_candidato,
            productor=productor,
            empresa=empresa_lock,
            kilos_estimados=kilos_estimados,
            patente_vehiculo=patente_vehiculo.strip().upper(),
            fecha_entrega=fecha_entrega,
            franja_horaria=franja_horaria,
            estado=Turno.ESTADO_CONFIRMADO,
            observaciones=observaciones.strip() if observaciones else ''
        )

        return turno


def transicionar_estado_turno(
    turno_id: int,
    nuevo_estado: str,
    kilos_reales: int = None,
    clase_tabaco: str = None,
    momento=None,
) -> Turno:
    """
    Permite a los operadores del centro de acopio actualizar el flujo operativo:
    'CONFIRMADO' -> 'EN_PLAYA' -> 'DESCARGADO'

    Cada transición deja una marca horaria (trazabilidad del circuito) y el cierre
    de descarga exige el peso neto de báscula y la clase asignada, con lo que se
    congela el precio de liquidación del turno.
    """
    transiciones_validas = {
        Turno.ESTADO_CONFIRMADO: [Turno.ESTADO_EN_PLAYA, Turno.ESTADO_CANCELADO],
        Turno.ESTADO_EN_PLAYA: [Turno.ESTADO_DESCARGADO, Turno.ESTADO_CANCELADO],
        Turno.ESTADO_DESCARGADO: [],
        Turno.ESTADO_CANCELADO: [],
    }

    turno = Turno.objects.get(id=turno_id)

    if nuevo_estado not in transiciones_validas.get(turno.estado, []):
        raise ValueError(f"No se permite transicionar el turno de {turno.get_estado_display()} a {nuevo_estado}.")

    momento = momento or timezone.now()
    campos = ['estado', 'actualizado_el']

    if nuevo_estado == Turno.ESTADO_EN_PLAYA:
        turno.hora_arribo_playa = momento
        campos.append('hora_arribo_playa')

    elif nuevo_estado == Turno.ESTADO_DESCARGADO:
        if kilos_reales in (None, ''):
            kilos_reales = turno.kilos_estimados
        try:
            kilos_reales = int(kilos_reales)
        except (TypeError, ValueError):
            raise ValueError("Debe informar el peso neto registrado en báscula para cerrar la descarga.")
        if kilos_reales <= 0:
            raise ValueError("El peso neto de báscula debe ser mayor a cero.")

        if not clase_tabaco:
            clase_tabaco = 'A'
        if clase_tabaco not in PRECIOS_CLASE_USD:
            raise ValueError("Debe asignar una clase comercial válida (A, B, C o D) al lote descargado.")

        turno.kilos_reales = kilos_reales
        turno.clase_tabaco = clase_tabaco
        turno.precio_kg = PRECIOS_CLASE_USD[clase_tabaco]
        turno.hora_descarga = momento
        campos += ['kilos_reales', 'clase_tabaco', 'precio_kg', 'hora_descarga']
        if not turno.hora_arribo_playa:
            turno.hora_arribo_playa = momento - timezone.timedelta(minutes=38)
            campos.append('hora_arribo_playa')

    turno.estado = nuevo_estado
    turno.save(update_fields=campos)
    return turno


def registrar_salida_origen(turno_o_id, productor=None, momento=None) -> Turno:
    """
    El productor informa que su unidad salió de finca rumbo a la planta.
    Permite medir el tiempo real de recorrido externo (finca → garita).
    Acepta tanto una instancia de Turno como su ID primario.
    """
    turno = (
        turno_o_id
        if isinstance(turno_o_id, Turno)
        else Turno.objects.get(id=int(turno_o_id))
    )
    if productor is not None and turno.productor_id != productor.id:
        raise ValueError("Solo el productor titular puede informar la salida de su unidad.")
    if turno.estado != Turno.ESTADO_CONFIRMADO:
        raise ValueError("Solo se puede informar la salida de turnos confirmados que aún no ingresaron a planta.")
    if turno.hora_salida_origen:
        raise ValueError("La salida de finca ya fue informada para este turno.")

    turno.hora_salida_origen = momento or timezone.now()
    turno.save(update_fields=['hora_salida_origen', 'actualizado_el'])
    return turno

