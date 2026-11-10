import django.core.validators
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='EmpresaAcopio',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('nombre', models.CharField(max_length=150, unique=True, verbose_name='Nombre de la Empresa')),
                ('cuit', models.CharField(max_length=20, unique=True, verbose_name='CUIT')),
                ('direccion', models.CharField(blank=True, max_length=200, verbose_name='Dirección de la Planta / Acopio')),
                ('capacidad_diaria_kg', models.PositiveIntegerField(default=250000, help_text='Límite operativo estricto de recepción por jornada (por defecto 250.000 kg)', verbose_name='Capacidad Diaria (Kg)')),
                ('activa', models.BooleanField(default=True, verbose_name='¿Activa para recepción?')),
            ],
            options={
                'verbose_name': 'Empresa de Acopio',
                'verbose_name_plural': 'Empresas de Acopio',
                'ordering': ['nombre'],
            },
        ),
    ]

