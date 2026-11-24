"""
Carga datos de prueba para SOLTAT.

    python manage.py seed_data          # crea lo que falte
    python manage.py seed_data --reset  # borra turnos y cupos y los vuelve a crear

Usuarios de prueba (DNI/CUIT · contraseña):
    Administrador   20000000001 · admin123        (también usuario 'admin')
    Empresa Coop.   30554433221 · empresa123
    Empresa AOT     30665544332 · empresa123
    Empresa Massalin 30776655443 · empresa123
    Recorredores    20111111111 / 20111111112 / 20111111113 · recorredor123
    Operador        20222222222 · operador123
    Productores     20345678901 (nivel bueno), 20287654321 (regular),
                    20312233445 (bloqueado), 27298877663 (bueno) · productor123
"""
import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Administrador, EmpresaAcopio, OperadorAcopio, Productor, Recorredor, Usuario
from turnos import services
from turnos.choices import FRANJAS_HORARIAS, CorteTabaco, EstadoTurno
from turnos.models import Comprobante, CupoProductor, RegistroOperador, Turno

FRANJAS = [valor for valor, _ in FRANJAS_HORARIAS]


def crear_usuario(modelo, username, password, cuit_dni, **datos):
    existente = Usuario.objects.filter(username=username).first()
    if existente:
        return existente.perfil
    return modelo.objects.create_user(username=username, password=password, cuit_dni=cuit_dni, **datos)


class Command(BaseCommand):
    help = 'Carga usuarios, empresas, cupos y turnos de prueba.'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true', help='Borra turnos y cupos antes de cargar.')

    @transaction.atomic
    def handle(self, *args, **options):
        random.seed(2026)
        if options['reset']:
            RegistroOperador.objects.all().delete()
            Comprobante.objects.all().delete()
            Turno.objects.all().delete()
            CupoProductor.objects.all().delete()

        crear_usuario(Administrador, 'admin', 'admin123', '20000000001', first_name='Administrador',
                      email='admin@soltat.com.ar')

        empresas = [
            crear_usuario(EmpresaAcopio, 'cooperativa', 'empresa123', '30554433221',
                          nombre='Cooperativa de Tabacaleros de Jujuy', telefono='+54 388 4220000',
                          capacidad_diaria_kg=250_000, capacidad_franja_kg=40_000),
            crear_usuario(EmpresaAcopio, 'allianceone', 'empresa123', '30665544332',
                          nombre='Alliance One Tobacco Argentina', telefono='+54 388 4230000',
                          capacidad_diaria_kg=220_000, capacidad_franja_kg=35_000),
            crear_usuario(EmpresaAcopio, 'massalin', 'empresa123', '30776655443',
                          nombre='Massalin Particulares', telefono='+54 388 4240000',
                          capacidad_diaria_kg=200_000, capacidad_franja_kg=30_000),
        ]
        coop, alliance, massalin = empresas

        recorredores = [
            crear_usuario(Recorredor, f'recorredor{i}', 'recorredor123', f'2011111111{i}',
                          first_name=nombre, last_name=apellido, telefono=f'+54 388 450000{i}',
                          empresa_asignada=empresa)
            for i, (nombre, apellido, empresa) in enumerate([
                ('Marcos', 'Quispe', coop), ('Lucía', 'Fernández', alliance), ('Diego', 'Cruz', massalin),
            ], start=1)
        ]
        rec_coop, rec_alliance, rec_massalin = recorredores

        operador = crear_usuario(OperadorAcopio, 'operador', 'operador123', '20222222222',
                                 first_name='Operador', last_name='Garita Perico', telefono='+54 388 4600000')

        productores = [
            crear_usuario(Productor, uname, 'productor123', cuit, first_name=nombre, last_name=apellido,
                          telefono=tel, email=f'{uname}@soltat.com.ar')
            for uname, cuit, nombre, apellido, tel in [
                ('productor', '20345678901', 'Juan', 'Pérez (Finca La Tabacalera)', '+54 388 4912345'),
                ('productor2', '20287654321', 'Rosa', 'Mamani (Valle de Los Perales)', '+54 388 4219988'),
                ('productor3', '20312233445', 'Carlos', 'Gutiérrez (San Isidro)', '+54 388 4332211'),
                ('productor4', '27298877663', 'Ana', 'Ríos (Finca El Ceibal)', '+54 388 4776655'),
            ]
        ]
        bueno, regular, bloqueado, bueno2 = productores

        if CupoProductor.objects.exists():
            self.stdout.write(self.style.WARNING('Ya hay cupos y turnos cargados. Use --reset para recrearlos.'))
            return

        # Cupos de temporada
        for p in productores:
            CupoProductor.objects.create(productor=p, recorredor=rec_coop, cupo_maximo=300_000)
        CupoProductor.objects.create(productor=bueno, recorredor=rec_alliance, cupo_maximo=150_000)
        CupoProductor.objects.create(productor=regular, recorredor=rec_alliance, cupo_maximo=120_000)
        CupoProductor.objects.create(productor=bueno2, recorredor=rec_massalin, cupo_maximo=100_000)

        hoy = timezone.localdate()

        def turno(productor, empresa, recorredor, dias, franja, kilos, corte, estado, kilos_reales=None,
                  precio=None, arribo_min=None, espera_min=None):
            fecha = hoy + timedelta(days=dias)
            t = Turno.objects.create(
                codigo=services.generar_codigo_turno(fecha), productor=productor, empresa=empresa,
                recorredor=recorredor if estado != EstadoTurno.PENDIENTE else None,
                kilos_estimados=kilos, patente_vehiculo=f'AE{random.randint(100, 999)}JY',
                fecha_entrega=fecha, franja_horaria=franja, estado=estado, corte_tabaco=corte,
            )
            if estado in (EstadoTurno.CONFIRMADO, EstadoTurno.EN_PLAYA, EstadoTurno.DESCARGADO) \
                    or (estado == EstadoTurno.CANCELADO and recorredor):
                services.emitir_comprobante(t)
            inicio = t.inicio_franja
            if estado in (EstadoTurno.EN_PLAYA, EstadoTurno.DESCARGADO):
                t.hora_arribo_playa = inicio + timedelta(minutes=arribo_min if arribo_min is not None else random.randint(-20, 60))
            if estado == EstadoTurno.DESCARGADO:
                t.hora_descarga = t.hora_arribo_playa + timedelta(minutes=espera_min or random.randint(35, 150))
                t.kilos_reales = kilos_reales or int(kilos * random.uniform(0.94, 1.04))
                RegistroOperador.objects.create(turno=t, operador=operador, peso_acordado=kilos,
                                                peso_real=t.kilos_reales, corte_tabaco=corte)
                Comprobante.objects.filter(turno=t).update(peso_real=t.kilos_reales, precio=precio)
            t.save()
            return t

        D, C, P, E, X = (EstadoTurno.DESCARGADO, EstadoTurno.CONFIRMADO, EstadoTurno.PENDIENTE,
                         EstadoTurno.EN_PLAYA, EstadoTurno.CANCELADO)
        B, M, S = CorteTabaco.BAJERA, CorteTabaco.MEDIANA, CorteTabaco.SUPERIOR

        # Historial: entregas cumplidas
        for dias in (-58, -45, -33, -21, -12, -4):
            turno(bueno, coop, rec_coop, dias, random.choice(FRANJAS), random.randint(12, 30) * 1000,
                  random.choice([B, M, S]), D, precio=Decimal('3.40'))
        for dias in (-50, -26, -9):
            turno(bueno, alliance, rec_alliance, dias, random.choice(FRANJAS), random.randint(10, 20) * 1000, M, D)
        for dias in (-55, -40, -28, -16, -7):
            turno(regular, coop, rec_coop, dias, random.choice(FRANJAS), random.randint(8, 18) * 1000, B, D)
        for dias in (-44, -30, -18, -6):
            turno(bueno2, coop, rec_coop, dias, random.choice(FRANJAS), random.randint(15, 25) * 1000, S, D)
        for dias in (-60, -48):
            turno(bloqueado, coop, rec_coop, dias, random.choice(FRANJAS), 15_000, M, D)

        # Incumplimientos
        turno(regular, coop, rec_coop, -3, '10:00 - 12:00', 14_000, B, C)            # no se presentó
        turno(bloqueado, coop, rec_coop, -20, '08:00 - 10:00', 20_000, M, D,
              kilos_reales=14_000)                                                    # desvío del 30 %
        turno(bloqueado, coop, rec_coop, -10, '14:00 - 16:00', 18_000, M, X)           # canceló confirmado
        turno(bloqueado, coop, rec_coop, -5, '06:00 - 08:00', 16_000, M, C)            # no se presentó

        # Hoy: trabajo para el operador
        turno(bueno, coop, rec_coop, 0, '06:00 - 08:00', 22_000, S, D, precio=Decimal('3.60'))
        turno(bueno2, coop, rec_coop, 0, '08:00 - 10:00', 18_000, M, E, arribo_min=5)
        turno(regular, alliance, rec_alliance, 0, '08:00 - 10:00', 12_000, B, E, arribo_min=25)
        turno(bueno, alliance, rec_alliance, 0, '12:00 - 14:00', 16_000, M, C)
        turno(bueno2, massalin, rec_massalin, 0, '14:00 - 16:00', 20_000, S, C)
        turno(regular, coop, rec_coop, 0, '16:00 - 18:00', 10_000, B, C)

        # Próximos días: confirmados y solicitudes pendientes para los recorredores
        turno(bueno, coop, rec_coop, 1, '06:00 - 08:00', 25_000, M, C)
        turno(bueno2, coop, rec_coop, 2, '08:00 - 10:00', 18_000, S, C)
        turno(bueno, coop, rec_coop, 3, '06:00 - 08:00', 20_000, S, P)
        turno(regular, coop, rec_coop, 3, '12:00 - 14:00', 9_000, B, P)
        turno(regular, alliance, rec_alliance, 4, '12:00 - 14:00', 11_000, M, P)
        turno(bueno2, massalin, rec_massalin, 5, '06:00 - 08:00', 15_000, S, P)

        coop.corte_solicitado = CorteTabaco.SUPERIOR
        coop.save(update_fields=['corte_solicitado'])

        self.stdout.write(self.style.SUCCESS(
            f'Datos cargados: {Turno.objects.count()} turnos, {CupoProductor.objects.count()} cupos, '
            f'{Usuario.objects.count()} usuarios.'
        ))
        for p in productores:
            nivel = services.evaluar_cumplimiento(p)
            self.stdout.write(f'  {p.cuit_dni} {p.get_nombre_completo()}: {nivel["nivel_display"]}')
