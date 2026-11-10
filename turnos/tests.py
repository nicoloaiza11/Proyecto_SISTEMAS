from datetime import date
from django.test import TestCase
from accounts.models import CustomUser
from turnos.models import EmpresaAcopio, Turno
from turnos import services


class CuposYTurnosTestCase(TestCase):
    def setUp(self):
        self.empresa = EmpresaAcopio.objects.create(
            nombre="Cooperativa de Tabacaleros de Jujuy",
            cuit="30554433221",
            capacidad_diaria_kg=250000,
            activa=True
        )
        self.productor = CustomUser.objects.create_user(
            username="productor_test",
            cuit_dni="20345678901",
            password="testpassword",
            rol=CustomUser.ROL_PRODUCTOR,
            razon_social="Finca Test"
        )
        self.fecha = date(2026, 11, 10)

    def test_asignacion_turno_valido_dentro_del_cupo(self):
        """Verifica que si hay cupo disponible, se genera el turno correctamente"""
        turno = services.validar_y_crear_turno(
            productor=self.productor,
            empresa=self.empresa,
            kilos_estimados=50000,
            patente_vehiculo="AA123BB",
            fecha_entrega=self.fecha,
            franja_horaria="08:00 - 10:00"
        )
        self.assertIsNotNone(turno.id)
        self.assertTrue(turno.codigo.startswith("SLT-"))
        self.assertEqual(turno.estado, Turno.ESTADO_CONFIRMADO)
        self.assertEqual(services.obtener_kilos_reservados(self.empresa, self.fecha), 50000)

    def test_bloqueo_estricto_por_cupo_excedido(self):
        """Verifica que si la solicitud supera el límite de 250.000 kg, no se registra y lanza excepción con remanente"""
        # Reservamos 230.000 kg
        services.validar_y_crear_turno(
            productor=self.productor,
            empresa=self.empresa,
            kilos_estimados=230000,
            patente_vehiculo="AA111AA",
            fecha_entrega=self.fecha,
            franja_horaria="08:00 - 10:00"
        )

        # Intentamos reservar 30.000 kg cuando solo quedan 20.000 kg
        with self.assertRaises(services.CupoExcedidoException) as context:
            services.validar_y_crear_turno(
                productor=self.productor,
                empresa=self.empresa,
                kilos_estimados=30000,
                patente_vehiculo="BB222BB",
                fecha_entrega=self.fecha,
                franja_horaria="10:00 - 12:00"
            )

        self.assertEqual(context.exception.remanente_disponible, 20000)
        self.assertEqual(context.exception.kilos_solicitados, 30000)
        # Aseguramos que la base de datos no sumó los kilos fallidos
        self.assertEqual(services.obtener_kilos_reservados(self.empresa, self.fecha), 230000)

    def test_semaforo_de_capacidad(self):
        """Evalúa los umbrales de semaforización (normal < 80%, alerta >= 80%, completo 100%)"""
        # Sin carga: normal
        metricas = services.consultar_disponibilidad_cupo(self.empresa, self.fecha)
        self.assertEqual(metricas['nivel_semaforo'], 'normal')

        # Cargar 210.000 kg (84% -> alerta)
        services.validar_y_crear_turno(
            productor=self.productor,
            empresa=self.empresa,
            kilos_estimados=210000,
            patente_vehiculo="CC333CC",
            fecha_entrega=self.fecha,
            franja_horaria="08:00 - 10:00"
        )
        metricas_alerta = services.consultar_disponibilidad_cupo(self.empresa, self.fecha)
        self.assertEqual(metricas_alerta['nivel_semaforo'], 'alerta')
        self.assertEqual(metricas_alerta['color_hex'], '#D6A84F')

    def test_transicion_de_estados_operativos(self):
        """Verifica el flujo 'CONFIRMADO' -> 'EN_PLAYA' -> 'DESCARGADO'"""
        turno = services.validar_y_crear_turno(
            productor=self.productor,
            empresa=self.empresa,
            kilos_estimados=15000,
            patente_vehiculo="DD444DD",
            fecha_entrega=self.fecha,
            franja_horaria="12:00 - 14:00"
        )
        self.assertEqual(turno.estado, Turno.ESTADO_CONFIRMADO)

        # Transición 1: Ingreso a Playa
        services.transicionar_estado_turno(turno.id, Turno.ESTADO_EN_PLAYA)
        turno.refresh_from_db()
        self.assertEqual(turno.estado, Turno.ESTADO_EN_PLAYA)

        # Transición 2: Descargado en báscula
        services.transicionar_estado_turno(turno.id, Turno.ESTADO_DESCARGADO)
        turno.refresh_from_db()
        self.assertEqual(turno.estado, Turno.ESTADO_DESCARGADO)

        # Transición inválida: volver a playa desde descargado
        with self.assertRaises(ValueError):
            services.transicionar_estado_turno(turno.id, Turno.ESTADO_EN_PLAYA)

    def test_trazabilidad_recorrido_y_analitica_gerencial_sad(self):
        """Verifica el registro de tiempos de ruta/planta, liquidación USD y el motor SAD (Empresa y Productor)"""
        from turnos import analytics

        turno = services.validar_y_crear_turno(
            productor=self.productor,
            empresa=self.empresa,
            kilos_estimados=20000,
            patente_vehiculo="EE555FF",
            fecha_entrega=self.fecha,
            franja_horaria="08:00 - 10:00"
        )

        # 1. Productor informa salida de finca
        services.registrar_salida_origen(turno.id, productor=self.productor)
        turno.refresh_from_db()
        self.assertIsNotNone(turno.hora_salida_origen)

        # 2. Ingreso a garita de playa
        services.transicionar_estado_turno(turno.id, Turno.ESTADO_EN_PLAYA)
        turno.refresh_from_db()
        self.assertIsNotNone(turno.hora_arribo_playa)

        # 3. Cierre en báscula con 19.200 kg reales y Clase A (USD 4.20/kg)
        services.transicionar_estado_turno(
            turno.id,
            Turno.ESTADO_DESCARGADO,
            kilos_reales=19200,
            clase_tabaco=Turno.CLASE_A,
        )
        turno.refresh_from_db()
        self.assertEqual(turno.kilos_reales, 19200)
        self.assertEqual(turno.clase_tabaco, Turno.CLASE_A)
        self.assertEqual(turno.diferencia_kg, -800)
        self.assertAlmostEqual(turno.importe_usd, 19200 * 4.20, places=2)
        self.assertIsNotNone(turno.minutos_viaje)
        self.assertIsNotNone(turno.minutos_espera)
        self.assertIsNotNone(turno.minutos_ciclo)

        # 4. Motor SAD de Gerencia de Acopio y Productor
        sad_emp = analytics.analitica_empresa(self.empresa, 7, self.fecha, self.fecha)
        self.assertEqual(sad_emp['metricas']['kg_recibidos'], 19200)
        self.assertEqual(sad_emp['uso']['sobre_reserva'], 800)
        self.assertTrue(len(sad_emp['recomendaciones']) >= 1)

        sad_prod = analytics.analitica_productor(self.productor, 7, self.fecha, self.fecha)
        self.assertEqual(sad_prod['metricas']['kg_recibidos'], 19200)
        self.assertEqual(sad_prod['metricas']['entregas'], 1)
        self.assertTrue(len(sad_prod['recomendaciones']) >= 1)

        # 5. Verificación de vistas HTTP (Tableros Gerenciales SAD y Operativos)
        operador = CustomUser.objects.create_user(
            username="operador_test",
            cuit_dni="30554433221",
            password="testpassword",
            rol=CustomUser.ROL_EMPRESA,
            empresa_asociada=self.empresa,
        )
        self.client.force_login(operador)
        resp_sad_emp = self.client.get('/acopio/gerencia/?periodo=30d')
        self.assertEqual(resp_sad_emp.status_code, 200)
        resp_panel = self.client.get(f'/acopio/panel/?fecha={self.fecha.isoformat()}')
        self.assertEqual(resp_panel.status_code, 200)

        self.client.force_login(self.productor)
        resp_sad_prod = self.client.get('/turnos/rendimiento/?periodo=30d')
        self.assertEqual(resp_sad_prod.status_code, 200)
        resp_solicitar = self.client.get('/turnos/solicitar/')
        self.assertEqual(resp_solicitar.status_code, 200)
        resp_comprobante = self.client.get(f'/turnos/comprobante/{turno.codigo}/')
        self.assertEqual(resp_comprobante.status_code, 200)



