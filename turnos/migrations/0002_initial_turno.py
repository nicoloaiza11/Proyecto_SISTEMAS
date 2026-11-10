from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('turnos', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Turno',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('codigo', models.CharField(editable=False, max_length=20, unique=True, verbose_name='Código de Turno')),
                ('kilos_estimados', models.PositiveIntegerField(help_text='Estimación de carga en kilogramos a entregar', verbose_name='Kilos Estimados')),
                ('patente_vehiculo', models.CharField(help_text='Dominio del camión, acoplado o camioneta', max_length=20, verbose_name='Vehículo / Patente')),
                ('fecha_entrega', models.DateField(verbose_name='Fecha de Entrega')),
                ('franja_horaria', models.CharField(choices=[('08:00 - 10:00', '08:00 a 10:00 hs'), ('10:00 - 12:00', '10:00 a 12:00 hs'), ('12:00 - 14:00', '12:00 a 14:00 hs'), ('14:00 - 16:00', '14:00 a 16:00 hs'), ('16:00 - 18:00', '16:00 a 18:00 hs')], max_length=30, verbose_name='Franja Horaria')),
                ('estado', models.CharField(choices=[('CONFIRMADO', 'Confirmado'), ('EN_PLAYA', 'En playa'), ('DESCARGADO', 'Descargado'), ('CANCELADO', 'Cancelado')], default='CONFIRMADO', max_length=20, verbose_name='Estado del Turno')),
                ('observaciones', models.TextField(blank=True, null=True, verbose_name='Observaciones adicionales')),
                ('creado_el', models.DateTimeField(auto_now_add=True, verbose_name='Fecha de Creación')),
                ('actualizado_el', models.DateTimeField(auto_now=True, verbose_name='Última Actualización')),
                ('empresa', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='turnos_programados', to='turnos.empresaacopio', verbose_name='Empresa Compradora')),
                ('productor', models.ForeignKey(limit_choices_to={'rol': 'productor'}, on_delete=django.db.models.deletion.PROTECT, related_name='turnos_solicitados', to=settings.AUTH_USER_MODEL, verbose_name='Productor')),
            ],
            options={
                'verbose_name': 'Turno de Acopio',
                'verbose_name_plural': 'Turnos de Acopio',
                'ordering': ['fecha_entrega', 'franja_horaria', 'creado_el'],
                'indexes': [models.Index(fields=['fecha_entrega', 'empresa', 'estado'], name='turnos_turn_fecha_e_52419c_idx')],
            },
        ),
    ]

