"""
Integración con la API gratuita Open-Meteo para pronósticos climáticos.

Ubicación configurada: Ciudad Perico, Jujuy, Argentina.
  latitude  = -24.3833
  longitude = -65.1167

Reglas de negocio implementadas:
  - Solo se consulta para turnos en estado CONFIRMADO con fecha dentro
    de los próximos 16 días.
  - Lazy Refresh: si ya hay datos guardados y tienen menos de 6 horas,
    se devuelven sin volver a llamar a la API.
  - Si la API falla o tarda, se captura la excepción y se devuelve None
    silenciosamente para no bloquear la carga de la vista.
"""
import logging
from datetime import timedelta
from urllib import request as urllib_request
from urllib.error import URLError
import json

from django.utils import timezone

from .choices import INICIO_FRANJA, EstadoTurno

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuración de la ubicación (Ciudad Perico, Jujuy)
# ---------------------------------------------------------------------------
LAT = -24.3833
LON = -65.1167
TIMEZONE = "America%2FArgentina%2FJujuy"

VENTANA_PRONOSTICO_DIAS = 16   # Límite de la API de pronóstico gratuita
REFRESCO_HORAS = 6             # Lazy Refresh cada N horas

# Variables horarias solicitadas a la API
HOURLY_VARS = (
    "temperature_2m,"
    "relative_humidity_2m,"
    "precipitation_probability,"
    "precipitation,"
    "weather_code,"
    "cloud_cover,"
    "wind_gusts_10m"
)

# Mapa de WMO Weather Code → descripción corta en español
_WMO_DESC = {
    0: "Despejado", 1: "Mayormente despejado", 2: "Parcialmente nublado",
    3: "Nublado", 45: "Niebla", 48: "Niebla con escarcha",
    51: "Llovizna leve", 53: "Llovizna moderada", 55: "Llovizna intensa",
    61: "Lluvia leve", 63: "Lluvia moderada", 65: "Lluvia intensa",
    71: "Nevada leve", 73: "Nevada moderada", 75: "Nevada intensa",
    80: "Chubascos leves", 81: "Chubascos moderados", 82: "Chubascos fuertes",
    95: "Tormenta", 96: "Tormenta con granizo", 99: "Tormenta fuerte",
}


def _wmo_descripcion(code) -> str:
    return _WMO_DESC.get(code, f"Código {code}")


def _recomendar(datos: dict) -> str:
    """
    Genera una recomendación operativa breve en función de los valores
    del pronóstico para el horario del turno.
    """
    prob_lluvia = datos.get("precipitation_probability", 0) or 0
    precipitacion = datos.get("precipitation", 0) or 0
    nubosidad = datos.get("cloud_cover", 0) or 0
    rafagas = datos.get("wind_gusts_10m", 0) or 0
    temperatura = datos.get("temperature_2m")

    if prob_lluvia >= 60 or precipitacion > 1:
        return "⚠️ Alta probabilidad de lluvia. Cubrir los fardos con lona y prever demoras."
    if prob_lluvia >= 30 or precipitacion > 0:
        return "🌦️ Posible lluvia. Monitorear el clima antes de cargar."
    if temperatura is not None and temperatura >= 32 and prob_lluvia < 30:
        return "🌡️ Temperatura elevada: Evitar mantener el tabaco cubierto con lona durante la espera para prevenir que se queme la hoja."
    if rafagas > 50:
        return "💨 Ráfagas fuertes. Asegurar la carga en el transporte."
    if nubosidad > 80:
        return "☁️ Cielo muy nublado. Condiciones aptas, sin lluvia prevista."
    return "✅ Condiciones favorables para la entrega."


def _build_url(fecha_str: str) -> str:
    return (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly={HOURLY_VARS}"
        f"&timezone={TIMEZONE}"
        f"&start_date={fecha_str}&end_date={fecha_str}"
    )


def _extraer_hora(turno) -> int | None:
    """Devuelve la hora de inicio de la franja del turno (entero, ej. 8)."""
    if not turno.franja_horaria:
        return None
    return INICIO_FRANJA.get(turno.franja_horaria)


def _fetch_pronostico(fecha_str: str, hora: int) -> dict | None:
    """
    Llama a Open-Meteo y extrae los valores de la hora exacta del turno.
    Devuelve un dict con los valores o None si falla.
    """
    url = _build_url(fecha_str)
    try:
        with urllib_request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
    except (URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Open-Meteo: fallo al consultar %s — %s", url, exc)
        return None

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    # La API devuelve horas en formato "YYYY-MM-DDTHH:00"
    hora_str = f"{fecha_str}T{hora:02d}:00"
    try:
        idx = times.index(hora_str)
    except ValueError:
        logger.warning("Open-Meteo: hora %s no encontrada en la respuesta.", hora_str)
        return None

    datos = {key: (vals[idx] if isinstance(vals, list) and idx < len(vals) else None)
             for key, vals in hourly.items() if key != "time"}

    # Añadimos campos derivados listos para el template
    datos["descripcion"] = _wmo_descripcion(datos.get("weather_code"))
    datos["recomendacion"] = _recomendar(datos)
    datos["hora_consulta"] = hora_str
    return datos


# ---------------------------------------------------------------------------
# Función pública principal
# ---------------------------------------------------------------------------
def pronostico_para_turno(turno) -> dict | None:
    """
    Devuelve el pronóstico climático para el turno dado siguiendo las
    reglas de negocio:

      PENDIENTE  → None  (no consultar)
      EN_PLAYA   → None  (no consultar)
      CONFIRMADO:
        · fecha > 16 días → None  (fuera de ventana)
        · datos frescos (<6 h) → devuelve cache guardado en turno.clima_datos
        · datos viejos / sin datos → llama a Open-Meteo, guarda y devuelve

    La función **no guarda en la BD**; eso lo hace `refrescar_si_necesario`.
    """
    if turno.estado != EstadoTurno.CONFIRMADO:
        return None
    if not turno.fecha_entrega:
        return None

    hoy = timezone.localdate()
    dias_diferencia = (turno.fecha_entrega - hoy).days
    if dias_diferencia < 0 or dias_diferencia > VENTANA_PRONOSTICO_DIAS:
        return None

    hora = _extraer_hora(turno)
    if hora is None:
        return None

    fecha_str = turno.fecha_entrega.strftime("%Y-%m-%d")
    return _fetch_pronostico(fecha_str, hora)


def necesita_refresco(turno) -> bool:
    """True si el turno no tiene datos o si ya pasaron ≥ 6 h desde la última consulta."""
    if not turno.clima_actualizado_el:
        return True
    limite = turno.clima_actualizado_el + timedelta(hours=REFRESCO_HORAS)
    return timezone.now() >= limite


def refrescar_si_necesario(turno) -> bool:
    """
    Consulta Open-Meteo y persiste los datos en turno.clima_datos y
    turno.clima_actualizado_el si corresponde.
    Devuelve True si se actualizó, False en caso contrario.
    No lanza excepciones: cualquier error de API se loguea y se ignora.
    """
    if turno.estado != EstadoTurno.CONFIRMADO:
        return False
    if not turno.fecha_entrega:
        return False

    hoy = timezone.localdate()
    dias_diferencia = (turno.fecha_entrega - hoy).days
    if dias_diferencia < 0 or dias_diferencia > VENTANA_PRONOSTICO_DIAS:
        return False

    if not necesita_refresco(turno):
        return False

    hora = _extraer_hora(turno)
    if hora is None:
        return False

    fecha_str = turno.fecha_entrega.strftime("%Y-%m-%d")
    datos = _fetch_pronostico(fecha_str, hora)
    if datos is None:
        return False

    turno.clima_datos = datos
    turno.clima_actualizado_el = timezone.now()
    turno.save(update_fields=["clima_datos", "clima_actualizado_el"])
    return True


def clima_display(turno) -> dict:
    """
    Devuelve un diccionario listo para el template con:
      - modo: 'pendiente_confirmacion' | 'en_playa' | 'fuera_ventana' | 'disponible'
      - datos: el dict del pronóstico (solo cuando modo=='disponible')
    No realiza ninguna llamada HTTP; solo lee turno.clima_datos.
    """
    from .choices import EstadoTurno  # import local para evitar ciclos

    if turno.estado == EstadoTurno.PENDIENTE:
        return {"modo": "pendiente_confirmacion", "datos": None}
    if turno.estado == EstadoTurno.EN_PLAYA:
        return {"modo": "en_playa", "datos": None}
    if turno.estado != EstadoTurno.CONFIRMADO:
        return {"modo": "no_aplica", "datos": None}

    if not turno.fecha_entrega:
        return {"modo": "fuera_ventana", "datos": None}

    hoy = timezone.localdate()
    dias = (turno.fecha_entrega - hoy).days
    if dias < 0 or dias > VENTANA_PRONOSTICO_DIAS:
        return {"modo": "fuera_ventana", "datos": None}

    if turno.clima_datos:
        return {"modo": "disponible", "datos": turno.clima_datos}

    # Si llegamos aquí, el refresco todavía no corrió para este turno
    return {"modo": "cargando", "datos": None}
