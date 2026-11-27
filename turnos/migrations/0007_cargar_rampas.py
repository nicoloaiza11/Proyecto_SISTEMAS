"""
Carga las 5 rampas de descarga del centro de acopio de Perico y les asigna
las empresas existentes según el reparto acordado:

- Massalin:     rampas 1 y 2       (prioridad a la mañana)
- Alliance One: rampas 3, 4 y 5    (prioridad a la mañana)
- Cooperativa:  rampas 1 a 5       (prioridad a la tarde)

Después asigna una rampa libre a los turnos ya agendados que no la tienen.
"""
from django.db import migrations

REPARTO = [
    # (texto en el nombre de la empresa, rampas, horario preferente)
    ('massalin', [1, 2], 'MANANA'),
    ('alliance', [3, 4, 5], 'MANANA'),
    ('cooperativa', [1, 2, 3, 4, 5], 'TARDE'),
]


def cargar_rampas(apps, schema_editor):
    Rampa = apps.get_model('turnos', 'Rampa')
    Turno = apps.get_model('turnos', 'Turno')
    EmpresaAcopio = apps.get_model('accounts', 'EmpresaAcopio')

    rampas = {n: Rampa.objects.get_or_create(numero=n)[0] for n in range(1, 6)}
    for texto, numeros, horario in REPARTO:
        for empresa in EmpresaAcopio.objects.filter(nombre__icontains=texto):
            empresa.horario_preferente = horario
            empresa.save(update_fields=['horario_preferente'])
            for n in numeros:
                rampas[n].empresas.add(empresa)

    # Turnos ya agendados: primera rampa libre de su empresa en esa franja.
    ocupadas = set(
        Turno.objects.filter(rampa__isnull=False).exclude(estado='CANCELADO')
        .values_list('fecha_entrega', 'franja_horaria', 'rampa_id')
    )
    pendientes = (
        Turno.objects.filter(rampa__isnull=True, fecha_entrega__isnull=False, franja_horaria__isnull=False)
        .exclude(estado='CANCELADO').order_by('fecha_entrega', 'franja_horaria', 'id')
    )
    for turno in pendientes:
        for rampa in Rampa.objects.filter(empresas__pk=turno.empresa_id).order_by('numero'):
            clave = (turno.fecha_entrega, turno.franja_horaria, rampa.pk)
            if clave not in ocupadas:
                ocupadas.add(clave)
                turno.rampa_id = rampa.pk
                turno.save(update_fields=['rampa'])
                break


class Migration(migrations.Migration):

    dependencies = [
        ('turnos', '0006_rampas'),
    ]

    operations = [
        migrations.RunPython(cargar_rampas, migrations.RunPython.noop),
    ]
