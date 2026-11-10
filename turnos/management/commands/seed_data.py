import random
from datetime import date, datetime, time, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from accounts.models import CustomUser
from turnos.models import EmpresaAcopio, Turno, PRECIOS_CLASE_USD
from turnos import services


class Command(BaseCommand):
    help = 'Pobla la base de datos con empresas, usuarios e historial de movimientos con tiempos y liquidación para analítica gerencial'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Actualizando datos operativos e históricos para SOLTAT (SAD Gerencial)..."))

        # 1. Empresas de Acopio
        empresa_coop, _ = EmpresaAcopio.objects.get_or_create(
            cuit='30554433221',
            defaults={
                'nombre': 'Cooperativa de Tabacaleros de Jujuy Ltda.',
                'direccion': 'Ruta Provincial 42, Km 12 - Perico, Jujuy',
                'capacidad_diaria_kg': 250000,
                'activa': True,
            }
        )
        empresa_alliance, _ = EmpresaAcopio.objects.get_or_create(
            cuit='30665544332',
            defaults={
                'nombre': 'Alliance One Tobacco Argentina',
                'direccion': 'Parque Industrial El Carmen, Jujuy',
                'capacidad_diaria_kg': 250000,
                'activa': True,
            }
        )
        empresa_massalin, _ = EmpresaAcopio.objects.get_or_create(
            cuit='30776655443',
            defaults={
                'nombre': 'Massalin Particulares S.R.L.',
                'direccion': 'Av. de los Trabajadores 1500 - Salta',
                'capacidad_diaria_kg': 250000,
                'activa': True,
            }
        )

        # 2. Usuarios Productores y Operadores
        productores_spec = [
            ('productor', '20345678901', 'Finca La Tabacalera - Pérez Hermanos', '+54 388 4912345'),
            ('productor2', '20287654321', 'Agropecuaria Valle de Lerma S.A.', '+54 387 4219988'),
            ('productor3', '20312233445', 'Establecimiento San Isidro S.R.L.', '+54 388 4332211'),
            ('productor4', '27298877663', 'Finca El Ceibal - Familia Ríos', '+54 388 4776655'),
        ]
        productores = []
        for uname, cuit, razon, tel in productores_spec:
            u, created = CustomUser.objects.get_or_create(
                username=uname,
                defaults={
                    'cuit_dni': cuit,
                    'email': f'{uname}@soltat.com.ar',
                    'razon_social': razon,
                    'telefono': tel,
                    'rol': CustomUser.ROL_PRODUCTOR,
                    'is_active': True,
                }
            )
            if created:
                u.set_password('productor123')
                u.save()
            productores.append(u)

        user_empresa, created_emp = CustomUser.objects.get_or_create(
            username='operador_cooperativa',
            defaults={
                'cuit_dni': '30554433221',
                'email': 'acopio@cooperativatabaco.com.ar',
                'razon_social': 'Gerencia de Operaciones - Cooperativa Jujuy',
                'telefono': '+54 388 4911122',
                'rol': CustomUser.ROL_EMPRESA,
                'empresa_asociada': empresa_coop,
                'is_active': True,
            }
        )
        if created_emp:
            user_empresa.set_password('empresa123')
            user_empresa.save()

        admin_user, created_admin = CustomUser.objects.get_or_create(
            username='admin',
            defaults={
                'cuit_dni': '99999999999',
                'email': 'admin@soltat.gob.ar',
                'razon_social': 'Dirección General SOLTAT',
                'rol': CustomUser.ROL_EMPRESA,
                'is_staff': True,
                'is_superuser': True,
                'is_active': True,
            }
        )
        if created_admin:
            admin_user.set_password('admin123')
            admin_user.save()

        # 3. Limpiar turnos previos de demo y sembrar historial rico y determinístico (últimos 45 días + hoy)
        Turno.objects.all().delete()

        rng = random.Random(20261108)
        hoy = date.today()
        franjas = [f[0] for f in Turno.FRANJAS_HORARIAS]
        patentes = ['AD123EF', 'AC456GH', 'AF789JK', 'AE321MN', 'AG452KL', 'AB908RT', 'AA774LP', 'AE512QP']
        lotes_obs = [
            'Tabaco Virginia Fardos Estándar 45kg',
            'Virginia Despalillado Primera Cosecha',
            'Burley Bultos Prensa Hidráulica',
            'Virginia Hojas Medianas Secado Estufa',
            'Lote Clasificado Finca - Fardos Secos',
        ]

        # Generar historial de 40 días hacia atrás (excluyendo domingos)
        for delta in range(40, 0, -1):
            dia = hoy - timedelta(days=delta)
            if dia.weekday() == 6:  # Domingo sin operación
                continue

            # Entre 4 y 6 movimientos por jornada en Cooperativa + 2 en Alliance/Massalin
            cant_coop = rng.randint(4, 6)
            for idx in range(cant_coop):
                prod = productores[idx % len(productores)]
                franja = franjas[idx % len(franjas)]
                hora_base = int(franja.split(':')[0])

                kg_est = rng.choice([28000, 32000, 36000, 40000, 44000, 48000])
                es_cancelado = (delta % 9 == 0 and idx == cant_coop - 1)

                if es_cancelado:
                    Turno.objects.create(
                        codigo=services.generar_codigo_turno(dia),
                        productor=prod,
                        empresa=empresa_coop,
                        kilos_estimados=kg_est,
                        patente_vehiculo=rng.choice(patentes),
                        fecha_entrega=dia,
                        franja_horaria=franja,
                        estado=Turno.ESTADO_CANCELADO,
                        observaciones='Inasistencia informada por lluvia en camino vecinal',
                    )
                    continue

                # Productor 3 tiende a sobre-estimar (~12% menos en báscula) para que el SAD lo detecte
                if prod.username == 'productor3':
                    factor_real = rng.uniform(0.85, 0.91)
                else:
                    factor_real = rng.uniform(0.95, 1.02)
                kg_real = int(round((kg_est * factor_real) / 50.0) * 50)

                # Banda 10:00 - 12:00 tiene cuello de botella deliberado (mayor espera)
                desvio_arribo = rng.randint(-15, 55) if franja != '10:00 - 12:00' else rng.randint(10, 85)
                viaje_min = rng.randint(42, 82)
                espera_min = rng.randint(58, 92) if franja == '10:00 - 12:00' else rng.randint(24, 52)

                inicio_dt = timezone.make_aware(datetime.combine(dia, time(hora_base, 0)))
                t_arribo = inicio_dt + timedelta(minutes=desvio_arribo)
                t_salida = t_arribo - timedelta(minutes=viaje_min)
                t_descarga = t_arribo + timedelta(minutes=espera_min)

                clase = rng.choices(['A', 'B', 'C', 'D'], weights=[44, 34, 16, 6])[0]

                Turno.objects.create(
                    codigo=services.generar_codigo_turno(dia),
                    productor=prod,
                    empresa=empresa_coop,
                    kilos_estimados=kg_est,
                    kilos_reales=kg_real,
                    clase_tabaco=clase,
                    precio_kg=PRECIOS_CLASE_USD[clase],
                    patente_vehiculo=rng.choice(patentes),
                    fecha_entrega=dia,
                    franja_horaria=franja,
                    estado=Turno.ESTADO_DESCARGADO,
                    hora_salida_origen=t_salida,
                    hora_arribo_playa=t_arribo,
                    hora_descarga=t_descarga,
                    observaciones=rng.choice(lotes_obs),
                )

            # Entregas del productor principal también en Alliance One para comparativa multi-planta
            if delta % 3 == 0:
                franja = rng.choice(['08:00 - 10:00', '14:00 - 16:00'])
                hora_base = int(franja.split(':')[0])
                kg_est = rng.choice([25000, 30000, 35000])
                kg_real = int(round(kg_est * rng.uniform(0.97, 1.01) / 50.0) * 50)
                clase = rng.choices(['A', 'B'], weights=[65, 35])[0]
                inicio_dt = timezone.make_aware(datetime.combine(dia, time(hora_base, 0)))
                t_arribo = inicio_dt + timedelta(minutes=rng.randint(-10, 25))
                Turno.objects.create(
                    codigo=services.generar_codigo_turno(dia),
                    productor=productores[0],
                    empresa=empresa_alliance,
                    kilos_estimados=kg_est,
                    kilos_reales=kg_real,
                    clase_tabaco=clase,
                    precio_kg=PRECIOS_CLASE_USD[clase],
                    patente_vehiculo='AD123EF',
                    fecha_entrega=dia,
                    franja_horaria=franja,
                    estado=Turno.ESTADO_DESCARGADO,
                    hora_salida_origen=t_arribo - timedelta(minutes=rng.randint(35, 55)),
                    hora_arribo_playa=t_arribo,
                    hora_descarga=t_arribo + timedelta(minutes=rng.randint(20, 38)),
                    observaciones='Lote Exportación Virginia Superior',
                )

        # 4. Turnos de la jornada ACTUAL (hoy) para operar en vivo
        t08 = timezone.make_aware(datetime.combine(hoy, time(8, 12)))
        Turno.objects.create(
            codigo=services.generar_codigo_turno(hoy),
            productor=productores[0],
            empresa=empresa_coop,
            kilos_estimados=35000,
            kilos_reales=34650,
            clase_tabaco='A',
            precio_kg=PRECIOS_CLASE_USD['A'],
            patente_vehiculo='AD123EF',
            fecha_entrega=hoy,
            franja_horaria='08:00 - 10:00',
            estado=Turno.ESTADO_DESCARGADO,
            hora_salida_origen=t08 - timedelta(minutes=52),
            hora_arribo_playa=t08,
            hora_descarga=t08 + timedelta(minutes=34),
            observaciones='Tabaco Virginia Clase A · Fardos 45kg',
        )

        t10 = timezone.make_aware(datetime.combine(hoy, time(10, 18)))
        Turno.objects.create(
            codigo=services.generar_codigo_turno(hoy),
            productor=productores[1],
            empresa=empresa_coop,
            kilos_estimados=65000,
            patente_vehiculo='AC456GH',
            fecha_entrega=hoy,
            franja_horaria='10:00 - 12:00',
            estado=Turno.ESTADO_EN_PLAYA,
            hora_salida_origen=t10 - timedelta(minutes=64),
            hora_arribo_playa=t10,
            observaciones='Burley Bultos Estándar · En espera de báscula 2',
        )

        t12 = timezone.make_aware(datetime.combine(hoy, time(11, 20)))
        Turno.objects.create(
            codigo=services.generar_codigo_turno(hoy),
            productor=productores[0],
            empresa=empresa_coop,
            kilos_estimados=45000,
            patente_vehiculo='AF789JK',
            fecha_entrega=hoy,
            franja_horaria='12:00 - 14:00',
            estado=Turno.ESTADO_CONFIRMADO,
            hora_salida_origen=t12,
            observaciones='Virginia Fardos 45kg · Unidad en tránsito por RN 9',
        )

        Turno.objects.create(
            codigo=services.generar_codigo_turno(hoy),
            productor=productores[2],
            empresa=empresa_coop,
            kilos_estimados=40000,
            patente_vehiculo='AE321MN',
            fecha_entrega=hoy,
            franja_horaria='14:00 - 16:00',
            estado=Turno.ESTADO_CONFIRMADO,
            observaciones='Carga directa chasis y acoplado',
        )

        self.stdout.write(self.style.SUCCESS(
            f"✓ Sembrado completo: {Turno.objects.count()} movimientos con tiempos de recorrido, espera y liquidación."
        ))
