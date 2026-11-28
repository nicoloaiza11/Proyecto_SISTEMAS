"""
Registra en OcupacionRampa los turnos activos que ya tenían rampa y franja,
para que la nueva búsqueda de horarios los respete. Cada uno ocupa su franja
completa de 2 h (4 tramos de 30 min). No se modifica ningún turno.
"""
from datetime import time

from django.db import migrations


def registrar_ocupaciones(apps, schema_editor):
    Turno = apps.get_model('turnos', 'Turno')
    OcupacionRampa = apps.get_model('turnos', 'OcupacionRampa')
    turnos = (
        Turno.objects.filter(rampa__isnull=False, fecha_entrega__isnull=False, franja_horaria__isnull=False)
        .exclude(estado='CANCELADO')
    )
    nuevas = []
    for turno in turnos:
        hora = int(turno.franja_horaria[:2])
        for tramo in range(4):
            minutos = hora * 60 + tramo * 30
            nuevas.append(OcupacionRampa(
                turno_id=turno.pk, rampa_id=turno.rampa_id, fecha=turno.fecha_entrega,
                inicio=time(minutos // 60, minutos % 60),
            ))
    OcupacionRampa.objects.bulk_create(nuevas, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('turnos', '0008_turnos_duracion_variable'),
    ]

    operations = [
        migrations.RunPython(registrar_ocupaciones, migrations.RunPython.noop),
    ]
