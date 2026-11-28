import threading
from datetime import datetime, time, timedelta

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Administrador, EmpresaAcopio, OperadorAcopio, Productor, Recorredor, Usuario
from . import graficos, services
from . import weather as weather_service
from .choices import CorteTabaco, EstadoTurno, HorarioPreferente
from .models import Comprobante, CupoProductor, OcupacionRampa, Rampa, RegistroOperador, Turno


class BaseSoltat(TestCase):
    """Escenario mínimo: una empresa, su recorredor, un operador y un productor con cupo."""

    def setUp(self):
        self.hoy = timezone.localdate()
        self.manana = self.hoy + timedelta(days=1)
        self.empresa = EmpresaAcopio.objects.create_user(
            username='coop', password='x', cuit_dni='30554433221', nombre='Cooperativa',
            capacidad_diaria_kg=200_000, capacidad_franja_kg=30_000, telefono='+54 388 4220000',
        )
        self.recorredor = Recorredor.objects.create_user(
            username='rec', password='x', cuit_dni='20111111111', empresa_asignada=self.empresa,
            telefono='+54 388 4500001',
        )
        self.operador = OperadorAcopio.objects.create_user(username='op', password='x', cuit_dni='20222222222')
        self.productor = Productor.objects.create_user(
            username='prod', password='x', cuit_dni='20345678901', telefono='+54 388 4912345',
        )
        CupoProductor.objects.create(productor=self.productor, recorredor=self.recorredor, cupo_maximo=100_000)
        # Las 5 rampas (las crea la migración) habilitadas solo para la empresa del escenario.
        for numero in range(1, 6):
            Rampa.objects.get_or_create(numero=numero)[0].empresas.set([self.empresa])

    def solicitar(self, kilos=10_000, productor=None):
        return services.solicitar_turno(
            productor or self.productor, self.empresa, kilos, CorteTabaco.MEDIANA, 'ae123cd',
        )

    def turno_pasado(self, dias, estado, kilos=10_000, kilos_reales=None, fue_confirmado=False):
        t = Turno.objects.create(
            codigo=services.generar_codigo_turno(self.hoy), productor=self.productor, empresa=self.empresa,
            kilos_estimados=kilos, kilos_reales=kilos_reales, patente_vehiculo='AA000AA',
            fecha_entrega=self.hoy - timedelta(days=dias), franja_horaria='06:00 - 08:00',
            estado=estado, corte_tabaco=CorteTabaco.BAJERA, fue_confirmado=fue_confirmado,
        )
        return t

    def turno_confirmado_hoy(self, kilos=10_000):
        """Turno confirmado para hoy, sin depender de la hora en que corre el test."""
        t = self.turno_pasado(0, EstadoTurno.CONFIRMADO, kilos=kilos, fue_confirmado=True)
        t.corte_tabaco = CorteTabaco.MEDIANA
        t.save()
        return t


class TiposDeUsuarioTests(BaseSoltat):
    def test_cada_usuario_conoce_su_tipo(self):
        tipos = {u.username: u.tipo for u in Usuario.objects.all()}
        self.assertEqual(tipos, {
            'coop': 'empresaacopio', 'rec': 'recorredor', 'op': 'operadoracopio', 'prod': 'productor',
        })

    def test_nombre_completo_de_empresa_es_su_nombre(self):
        self.assertEqual(Usuario.objects.get(username='coop').get_nombre_completo(), 'Cooperativa')

    def test_login_por_cuit_redirige_segun_tipo(self):
        destinos = {
            '20345678901': 'turnos:mis_turnos',
            '20111111111': 'turnos:panel_recorredor',
            '20222222222': 'turnos:panel_operador',
            '30554433221': 'turnos:panel_empresa',
        }
        for cuit, destino in destinos.items():
            self.client.logout()
            r = self.client.post(reverse('login'), {'identificador': cuit, 'password': 'x'}, follow=True)
            self.assertEqual(r.resolver_match.view_name, destino, cuit)


class SolicitudYAsignacionTests(BaseSoltat):
    def test_productor_solicita_turno_pendiente_sin_fecha_ni_comprobante(self):
        turno = self.solicitar()
        self.assertEqual(turno.estado, EstadoTurno.PENDIENTE)
        self.assertIsNone(turno.fecha_entrega)
        self.assertIsNone(turno.franja_horaria)
        self.assertEqual(turno.patente_vehiculo, 'AE123CD')
        self.assertFalse(Comprobante.objects.filter(turno=turno).exists())

    def test_recorredor_confirma_y_sistema_asigna_fecha_y_franja_sin_comprobante(self):
        turno = self.solicitar()
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertEqual(turno.estado, EstadoTurno.CONFIRMADO)
        self.assertEqual(turno.recorredor, self.recorredor)
        self.assertIsNotNone(turno.fecha_entrega)
        self.assertIsNotNone(turno.franja_horaria)
        self.assertTrue(turno.fue_confirmado)
        self.assertFalse(Comprobante.objects.filter(turno=turno).exists())

    def test_turno_asignado_por_recorredor_nace_confirmado_sin_comprobante(self):
        turno = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.SUPERIOR, 'AB123', self.manana)
        self.assertEqual(turno.estado, EstadoTurno.CONFIRMADO)
        self.assertTrue(turno.fue_confirmado)
        self.assertFalse(Comprobante.objects.filter(turno=turno).exists())

    def test_con_las_rampas_ocupadas_asigna_el_horario_siguiente(self):
        # 5 rampas: los primeros cinco turnos de 2 h arrancan a las 06:00 y el sexto a las 08:00.
        CupoProductor.objects.create(productor=self.productor, recorredor=self.recorredor, cupo_maximo=100_000)
        turnos = [self.recorredor.asignar_turno(self.productor, 30_000, CorteTabaco.MEDIANA, f'AA{i}', self.manana)
                  for i in range(6)]
        self.assertEqual([t.hora_inicio for t in turnos[:5]], [time(6, 0)] * 5)
        self.assertEqual(len({t.rampa_id for t in turnos[:5]}), 5)
        self.assertEqual(turnos[5].hora_inicio, time(8, 0))

    def test_respeta_la_capacidad_diaria(self):
        self.empresa.capacidad_diaria_kg = 200_000
        self.empresa.capacidad_franja_kg = 30_000
        self.empresa.save()
        otro = Productor.objects.create_user(username='p2', password='x', cuit_dni='20999999999')
        CupoProductor.objects.create(productor=otro, recorredor=self.recorredor, cupo_maximo=1_000_000)
        for i in range(6):
            self.recorredor.asignar_turno(otro, 30_000, CorteTabaco.MEDIANA, f'AA{i}', self.manana)
        self.recorredor.asignar_turno(otro, 20_000, CorteTabaco.MEDIANA, 'AA99', self.manana)  # total 200.000 kg
        with self.assertRaises(services.CupoExcedidoError):
            self.recorredor.asignar_turno(otro, 1_000, CorteTabaco.MEDIANA, 'AA100', self.manana)

    def test_no_supera_el_cupo_de_temporada(self):
        self.solicitar(kilos=30_000)
        self.solicitar(kilos=30_000)
        self.solicitar(kilos=30_000)
        with self.assertRaises(services.SinCupoProductorError):
            self.solicitar(kilos=20_000)

    def test_sin_cupo_con_la_empresa_no_puede_solicitar(self):
        CupoProductor.objects.all().delete()
        with self.assertRaises(services.SinCupoProductorError):
            self.solicitar()

    def test_turno_que_supera_la_capacidad_diaria_se_rechaza(self):
        with self.assertRaises(services.TurnoError):
            self.solicitar(kilos=200_001)

    def test_solicitud_menor_a_50_kg_se_rechaza_y_50_kg_se_acepta(self):
        with self.assertRaises(services.TurnoError):
            self.solicitar(kilos=49)
        turno = self.solicitar(kilos=50)
        self.assertEqual(turno.kilos_estimados, 50)

    def test_productor_solicita_turno_sin_patente(self):
        self.client.force_login(self.productor)
        r = self.client.post(reverse('turnos:solicitar_turno'), {
            'empresa': self.empresa.pk,
            'kilos_estimados': 5000,
            'corte_tabaco': CorteTabaco.MEDIANA,
            'patente_vehiculo': '',
        })
        self.assertEqual(r.status_code, 302)
        turno = Turno.objects.get()
        self.assertEqual(turno.patente_vehiculo, '')

    def test_hoy_saltea_franjas_que_ya_empezaron(self):
        ahora = timezone.make_aware(datetime.combine(self.hoy, time(13, 30)))
        inicio, fin, rampa = services.buscar_bloque(self.empresa, self.hoy, 1_000, ahora=ahora)
        self.assertEqual(inicio, 14 * 60)  # el primer tramo que todavía no empezó


class CumplimientoTests(BaseSoltat):
    def test_sin_incumplimientos_es_bueno(self):
        self.turno_pasado(5, EstadoTurno.DESCARGADO, kilos_reales=10_500)
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['nivel'], 'BUENO')

    def test_no_presentarse_baja_a_regular_sin_restringir_franjas(self):
        self.turno_pasado(3, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        cumplimiento = services.evaluar_cumplimiento(self.productor)
        self.assertEqual(cumplimiento['nivel'], 'REGULAR')
        turno = self.solicitar()
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertIsNotNone(turno.franja_horaria)
        self.assertEqual(turno.fecha_entrega, self.hoy if int(turno.franja_horaria[:2]) > timezone.localtime().hour or turno.fecha_entrega == self.hoy else self.manana)

    def test_nivel_malo_asigna_minimo_5_dias_despues(self):
        self.turno_pasado(2, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        self.turno_pasado(4, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['nivel'], 'MALO')
        turno = self.solicitar()
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertGreaterEqual(turno.fecha_entrega, self.hoy + timedelta(days=5))
        self.assertEqual(turno.franja_horaria, '06:00 - 08:00')

    def test_nivel_malo_si_hay_turnos_mas_de_5_dias_asigna_al_final(self):
        # Otro turno ya programado para dentro de 8 días
        otro = Productor.objects.create_user(username='p_otro', password='x', cuit_dni='20888888888')
        CupoProductor.objects.create(productor=otro, recorredor=self.recorredor, cupo_maximo=100_000)
        fecha_lejana = self.hoy + timedelta(days=8)
        self.recorredor.asignar_turno(otro, 10_000, CorteTabaco.MEDIANA, 'AA111BB', fecha_lejana)

        self.turno_pasado(2, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        self.turno_pasado(4, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['nivel'], 'MALO')

        turno = self.solicitar()
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertEqual(turno.fecha_entrega, fecha_lejana)
        # El otro turno (10.000 kg a 17.500 kg/h) va de 06:00 a 07:00: el de nivel MALO
        # empieza cuando termina, aunque haya otras rampas libres a las 06:00.
        self.assertEqual(turno.hora_inicio, time(7, 0))

    def test_nivel_malo_nunca_queda_antes_ni_al_mismo_tiempo_que_la_agenda(self):
        otro = Productor.objects.create_user(username='p_otro', password='x', cuit_dni='20888888888')
        CupoProductor.objects.create(productor=otro, recorredor=self.recorredor, cupo_maximo=200_000)
        fecha = self.hoy + timedelta(days=8)
        self.recorredor.asignar_turno(otro, 35_000, CorteTabaco.MEDIANA, 'A', fecha)  # 06:00 a 08:00
        self.recorredor.asignar_turno(otro, 70_000, CorteTabaco.MEDIANA, 'B', fecha)  # 06:00 a 10:00, otra rampa
        self.turno_pasado(2, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        self.turno_pasado(4, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        turno = self.solicitar(kilos=17_500)
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertEqual((turno.fecha_entrega, turno.hora_inicio), (fecha, time(10, 0)))

    def test_nivel_malo_pasa_al_dia_siguiente_si_no_entra_despues_de_la_agenda(self):
        otro = Productor.objects.create_user(username='p_otro', password='x', cuit_dni='20888888888')
        CupoProductor.objects.create(productor=otro, recorredor=self.recorredor, cupo_maximo=300_000)
        fecha = self.hoy + timedelta(days=8)
        self.empresa.capacidad_diaria_kg = 300_000
        self.empresa.save()
        t = self.recorredor.asignar_turno(otro, 10_000, CorteTabaco.MEDIANA, 'A', fecha)
        services.liberar_rampa(t)  # se reubica a última hora: 21:00 a 22:00
        services.reservar_bloque(t, 21 * 60, 22 * 60, t.rampa)
        self.turno_pasado(2, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        self.turno_pasado(4, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        turno = self.solicitar(kilos=10_000)
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertEqual(turno.fecha_entrega, fecha + timedelta(days=1))
        self.assertEqual(turno.hora_inicio, time(6, 0))

    def test_desvio_mayor_al_20_por_ciento_cuenta(self):
        self.turno_pasado(3, EstadoTurno.DESCARGADO, kilos=10_000, kilos_reales=7_500)
        self.turno_pasado(4, EstadoTurno.DESCARGADO, kilos=10_000, kilos_reales=8_500)  # 15 %: no cuenta
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['cantidad'], 1)

    def test_cancelar_confirmado_cuenta_y_cancelacion_del_recorredor_no(self):
        self.turno_pasado(2, EstadoTurno.CANCELADO, fue_confirmado=True)   # canceló el productor
        self.turno_pasado(3, EstadoTurno.CANCELADO, fue_confirmado=False)  # canceló el recorredor
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['cantidad'], 1)

    def test_tres_incumplimientos_bloquean_por_30_dias(self):
        for dias in (2, 4, 6):
            self.turno_pasado(dias, EstadoTurno.CONFIRMADO)
        cumplimiento = services.evaluar_cumplimiento(self.productor)
        self.assertTrue(cumplimiento['bloqueado'])
        self.assertEqual(cumplimiento['bloqueado_hasta'], self.hoy - timedelta(days=2) + timedelta(days=30))
        with self.assertRaises(services.ProductorBloqueadoError):
            self.solicitar()
        # El recorredor sí puede asignarle un turno
        turno = self.recorredor.asignar_turno(self.productor, 5_000, CorteTabaco.BAJERA, 'AB1', self.manana)
        self.assertEqual(turno.franja_horaria, '06:00 - 08:00')

    def test_vencido_el_bloqueo_queda_malo(self):
        for dias in (40, 45, 50):
            self.turno_pasado(dias, EstadoTurno.CONFIRMADO)
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['nivel'], 'MALO')

    def test_incumplimientos_de_mas_de_90_dias_no_cuentan(self):
        self.turno_pasado(91, EstadoTurno.CONFIRMADO)
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['nivel'], 'BUENO')


class CancelacionTests(BaseSoltat):
    def test_cancelar_pendiente_sin_motivo_y_sin_avisos(self):
        turno = self.solicitar()
        self.assertEqual(self.productor.cancelar_turno(turno), [])
        self.assertEqual(turno.estado, EstadoTurno.CANCELADO)

    def test_cancelar_confirmado_exige_motivo_y_avisa_por_whatsapp(self):
        turno = self.solicitar()
        self.recorredor.confirmar_turno(turno)
        with self.assertRaises(services.TurnoError):
            self.productor.cancelar_turno_confirmado(turno, '')
        avisos = self.productor.cancelar_turno_confirmado(turno, 'Se rompió el tractor')
        turno.refresh_from_db()
        self.assertTrue(turno.fue_confirmado)
        self.assertEqual(turno.estado, EstadoTurno.CANCELADO)
        self.assertTrue(all('Se%20rompi' in a['url'] for a in avisos))
        self.assertIn('https://wa.me/543884500001', avisos[0]['url'])

    def test_recorredor_cancela_y_anula_comprobante(self):
        turno = self.recorredor.asignar_turno(self.productor, 5_000, CorteTabaco.BAJERA, 'AB1', self.manana)
        avisos = self.recorredor.cancelar_turno(turno, 'Planta en mantenimiento')
        self.assertFalse(Comprobante.objects.filter(turno=turno).exists())
        self.assertIn('543884912345', avisos[0]['url'])

    def test_recorredor_de_otra_empresa_no_puede_confirmar(self):
        otra = EmpresaAcopio.objects.create_user(username='aot', password='x', cuit_dni='30665544332', nombre='AOT')
        ajeno = Recorredor.objects.create_user(username='rec2', password='x', cuit_dni='20111111112',
                                               empresa_asignada=otra)
        with self.assertRaises(services.TurnoError):
            ajeno.confirmar_turno(self.solicitar())


class OperacionEnPlantaTests(BaseSoltat):
    def test_ingreso_y_pesaje_completan_turno_registro_y_comprobante(self):
        turno = self.turno_confirmado_hoy()
        self.operador.registrar_ingreso_garita(turno)
        self.assertEqual(turno.estado, EstadoTurno.EN_PLAYA)

        registro = self.operador.registrar_pesaje_y_descarga(turno, 9_800, CorteTabaco.SUPERIOR, '3,50')
        turno.refresh_from_db()
        self.assertEqual(turno.estado, EstadoTurno.DESCARGADO)
        self.assertEqual(turno.kilos_reales, 9_800)
        self.assertEqual(turno.corte_tabaco, CorteTabaco.SUPERIOR)
        self.assertEqual(registro.peso_acordado, 10_000)
        self.assertEqual(str(turno.comprobante.precio), '3.50')
        self.assertEqual(str(turno.comprobante.importe), '34300.00')
        self.assertEqual(str(turno.calcular_perdida_merma()), '700.00')

    def test_no_se_puede_pesar_sin_ingreso(self):
        turno = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.MEDIANA, 'AB1', self.manana)
        with self.assertRaises(services.TurnoError):
            self.operador.registrar_pesaje_y_descarga(turno, 9_800, CorteTabaco.MEDIANA, '3.50')

    def test_no_se_puede_pesar_sin_precio_acordado(self):
        turno = self.turno_confirmado_hoy()
        self.operador.registrar_ingreso_garita(turno)
        with self.assertRaises(services.TurnoError):
            self.operador.registrar_pesaje_y_descarga(turno, 9_800, CorteTabaco.MEDIANA, precio=None)

    def test_ingreso_solo_el_dia_del_turno(self):
        turno = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.MEDIANA, 'AB1', self.manana)
        with self.assertRaises(services.TurnoError):
            self.operador.registrar_ingreso_garita(turno)

    def test_kilos_reales_liberan_capacidad(self):
        turno = self.turno_confirmado_hoy()
        self.operador.registrar_ingreso_garita(turno)
        self.operador.registrar_pesaje_y_descarga(turno, 8_000, CorteTabaco.MEDIANA, precio='3.50')
        self.assertEqual(self.empresa.calcular_kilos_ocupados(self.hoy), 8_000)

    def test_asignar_sin_patente_y_registrar_patente_en_garita(self):
        # Se asigna para mañana y se pasa a hoy, para que el test no dependa de la hora
        # (después de las 21:30 ya no quedan horarios para hoy).
        turno = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.MEDIANA, '', self.manana)
        turno.fecha_entrega = self.hoy
        turno.save(update_fields=['fecha_entrega'])
        self.assertEqual(turno.patente_vehiculo, '')

        # En panel del operador debe aparecer el campo de patente para este turno
        self.client.force_login(self.operador)
        resp = self.client.get(reverse('turnos:panel_operador'))
        self.assertContains(resp, 'name="patente_vehiculo"')

        # Sin patente falla el ingreso en garita
        with self.assertRaises(services.TurnoError):
            self.operador.registrar_ingreso_garita(turno)

        # Con patente registra el ingreso y guarda la patente en mayúsculas
        self.operador.registrar_ingreso_garita(turno, patente_vehiculo='ad456ef')
        turno.refresh_from_db()
        self.assertEqual(turno.estado, EstadoTurno.EN_PLAYA)
        self.assertEqual(turno.patente_vehiculo, 'AD456EF')

    def test_panel_operador_no_muestra_campo_patente_si_ya_tiene_patente(self):
        self.turno_confirmado_hoy()
        self.client.force_login(self.operador)
        resp = self.client.get(reverse('turnos:panel_operador'))
        self.assertNotContains(resp, 'name="patente_vehiculo"')


class ConvocatoriaTests(BaseSoltat):
    def test_convocatoria_avisa_a_productores_con_cupo(self):
        avisos = self.empresa.enviar_aviso(CorteTabaco.SUPERIOR, 50_000, 'Recibimos desde el lunes')
        self.empresa.refresh_from_db()
        self.assertEqual(self.empresa.corte_solicitado, CorteTabaco.SUPERIOR)
        self.assertEqual(len(avisos), 1)
        self.assertIn('SUPERIOR', avisos[0]['mensaje'])


class VistasTests(BaseSoltat):
    def test_pantallas_principales_responden(self):
        turno = self.solicitar()
        casos = [
            ('prod', ['turnos:mis_turnos', 'turnos:solicitar_turno', 'turnos:registros_historicos', 'turnos:rendimiento']),
            ('rec', ['turnos:panel_recorredor', 'turnos:asignar_turno']),
            ('op', ['turnos:panel_operador']),
            ('coop', ['turnos:panel_empresa', 'turnos:historial_empresa']),
        ]
        for username, vistas in casos:
            self.client.force_login(Usuario.objects.get(username=username))
            for vista in vistas:
                self.assertEqual(self.client.get(reverse(vista)).status_code, 200, vista)
            self.assertEqual(self.client.get(reverse('turnos:comprobante', args=[turno.codigo])).status_code, 200)

    def test_productor_no_accede_al_panel_del_operador(self):
        self.client.force_login(self.productor)
        r = self.client.get(reverse('turnos:panel_operador'))
        self.assertEqual(r.status_code, 302)

    def test_productor_ajeno_no_ve_el_comprobante(self):
        turno = self.solicitar()
        otro = Productor.objects.create_user(username='p2', password='x', cuit_dni='20999999999')
        self.client.force_login(otro)
        self.assertEqual(self.client.get(reverse('turnos:comprobante', args=[turno.codigo])).status_code, 404)

    def test_solicitud_por_formulario_y_confirmacion_con_aviso(self):
        self.client.force_login(self.productor)
        r = self.client.post(reverse('turnos:solicitar_turno'), {
            'empresa': self.empresa.pk,
            'kilos_estimados': 8000, 'corte_tabaco': 'BAJERA', 'patente_vehiculo': 'ab123cd',
        })
        turno = Turno.objects.get()
        self.assertRedirects(r, reverse('turnos:comprobante', args=[turno.codigo]))

        self.client.force_login(self.recorredor)
        r = self.client.post(reverse('turnos:confirmar_turno', args=[turno.id]), follow=True)
        self.assertContains(r, 'https://wa.me/543884912345')

    def test_turnos_confirmados_vencidos_pasan_a_cancelado_y_solo_salen_en_historial(self):
        vencido = self.turno_pasado(1, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        vigente = self.turno_confirmado_hoy()

        # Al abrir mis_turnos, el vencido pasa a CANCELADO con marca de ausencia
        self.client.force_login(self.productor)
        resp_activos = self.client.get(reverse('turnos:mis_turnos'))
        vencido.refresh_from_db()
        vigente.refresh_from_db()
        self.assertEqual(vencido.estado, EstadoTurno.CANCELADO)
        self.assertTrue(vencido.cancelado_por_ausencia)
        self.assertEqual(vigente.estado, EstadoTurno.CONFIRMADO)
        codigos_activos = [t.codigo for t in resp_activos.context['turnos']]
        self.assertNotIn(vencido.codigo, codigos_activos)
        self.assertIn(vigente.codigo, codigos_activos)

        # La justificación del incumplimiento debe indicar ausencia y no cancelación manual
        cumplimiento = services.evaluar_cumplimiento(self.productor)
        self.assertEqual(cumplimiento['cantidad'], 1)
        self.assertEqual(cumplimiento['detalle'][0]['motivo'], 'No se presentó (ausencia)')

        # Si el productor cancela manualmente un turno confirmado, el motivo es distinto
        self.productor.cancelar_turno_confirmado(vigente, 'Problema mecánico')
        cumplimiento_2 = services.evaluar_cumplimiento(self.productor)
        motivos = {d['turno'].codigo: d['motivo'] for d in cumplimiento_2['detalle']}
        self.assertEqual(motivos[vencido.codigo], 'No se presentó (ausencia)')
        self.assertEqual(motivos[vigente.codigo], 'Canceló un turno confirmado')

        # En registros_historicos sí aparece el turno cancelado por vencimiento
        resp_hist = self.client.get(reverse('turnos:registros_historicos'))
        codigos_hist = [t.codigo for t in resp_hist.context['turnos']]
        self.assertIn(vencido.codigo, codigos_hist)
        self.assertContains(resp_hist, vencido.codigo)

    def test_panel_operador_no_muestra_turnos_vencidos_con_boton_deshabilitado(self):
        vencido = self.turno_pasado(1, EstadoTurno.CONFIRMADO, fue_confirmado=True)
        ayer_iso = (self.hoy - timedelta(days=1)).isoformat()

        self.client.force_login(self.operador)
        resp = self.client.get(reverse('turnos:panel_operador') + f'?fecha={ayer_iso}')
        vencido.refresh_from_db()
        self.assertEqual(vencido.estado, EstadoTurno.CANCELADO)
        self.assertNotContains(resp, vencido.codigo)

    def test_registros_historicos_pagina_de_a_10_turnos(self):
        turnos_creados = []
        for i in range(12):
            turnos_creados.append(self.turno_pasado(i + 1, EstadoTurno.DESCARGADO, kilos_reales=9_500))

        self.client.force_login(self.productor)
        r1 = self.client.get(reverse('turnos:registros_historicos'))
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(len(r1.context['turnos']), 10)
        self.assertEqual(r1.context['page_obj'].paginator.num_pages, 2)
        # El más reciente (1 día atrás) está en página 1; los 2 más antiguos en página 2
        codigos_p1 = [t.codigo for t in r1.context['turnos']]
        self.assertIn(turnos_creados[0].codigo, codigos_p1)
        self.assertNotIn(turnos_creados[11].codigo, codigos_p1)

        r2 = self.client.get(reverse('turnos:registros_historicos') + '?page=2')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(len(r2.context['turnos']), 2)
        codigos_p2 = [t.codigo for t in r2.context['turnos']]
        self.assertIn(turnos_creados[10].codigo, codigos_p2)
        self.assertIn(turnos_creados[11].codigo, codigos_p2)


# ---------------------------------------------------------------------------
# Tests del módulo weather (clima_display)
# ---------------------------------------------------------------------------
class WeatherDisplayTests(BaseSoltat):
    """Verifica la lógica de clima_display sin llamar a la red."""

    def _turno_confirmado(self, dias_desde_hoy=3):
        """Crea un turno CONFIRMADO con fecha futura y franja válida."""
        fecha = timezone.localdate() + timedelta(days=dias_desde_hoy)
        return Turno.objects.create(
            codigo=services.generar_codigo_turno(fecha),
            productor=self.productor,
            empresa=self.empresa,
            kilos_estimados=10_000,
            patente_vehiculo='AB123CD',
            fecha_entrega=fecha,
            franja_horaria='08:00 - 10:00',
            estado=EstadoTurno.CONFIRMADO,
            corte_tabaco=CorteTabaco.MEDIANA,
            fue_confirmado=True,
        )

    def test_pendiente_devuelve_modo_pendiente_confirmacion(self):
        turno = self.solicitar()
        self.assertEqual(weather_service.clima_display(turno)['modo'], 'pendiente_confirmacion')

    def test_en_playa_devuelve_modo_en_playa(self):
        turno = self._turno_confirmado(0)
        turno.estado = EstadoTurno.EN_PLAYA
        turno.save()
        self.assertEqual(weather_service.clima_display(turno)['modo'], 'en_playa')

    def test_confirmado_sin_datos_devuelve_modo_cargando(self):
        turno = self._turno_confirmado(dias_desde_hoy=3)
        self.assertIsNone(turno.clima_datos)
        self.assertEqual(weather_service.clima_display(turno)['modo'], 'cargando')

    def test_confirmado_con_datos_devuelve_modo_disponible(self):
        turno = self._turno_confirmado(dias_desde_hoy=3)
        turno.clima_datos = {
            'temperature_2m': 25.0, 'precipitation_probability': 10,
            'precipitation': 0.0, 'cloud_cover': 20, 'wind_gusts_10m': 15.0,
            'weather_code': 1, 'descripcion': 'Mayormente despejado',
            'recomendacion': '✅ Condiciones favorables para la entrega.',
            'hora_consulta': '2026-11-29T08:00',
        }
        turno.clima_actualizado_el = timezone.now()
        turno.save()
        result = weather_service.clima_display(turno)
        self.assertEqual(result['modo'], 'disponible')
        self.assertEqual(result['datos']['temperature_2m'], 25.0)

    def test_confirmado_mas_de_16_dias_devuelve_fuera_ventana(self):
        turno = self._turno_confirmado(dias_desde_hoy=17)
        self.assertEqual(weather_service.clima_display(turno)['modo'], 'fuera_ventana')

    def test_necesita_refresco_sin_timestamp(self):
        turno = self._turno_confirmado()
        self.assertTrue(weather_service.necesita_refresco(turno))

    def test_no_necesita_refresco_con_timestamp_reciente(self):
        turno = self._turno_confirmado()
        turno.clima_actualizado_el = timezone.now() - timedelta(hours=2)
        turno.save()
        self.assertFalse(weather_service.necesita_refresco(turno))

    def test_necesita_refresco_con_timestamp_viejo(self):
        turno = self._turno_confirmado()
        turno.clima_actualizado_el = timezone.now() - timedelta(hours=7)
        turno.save()
        self.assertTrue(weather_service.necesita_refresco(turno))

    def test_vista_mis_turnos_incluye_clima_display_en_contexto(self):
        """La vista del productor renderiza sin error con turnos que tienen clima_display."""
        self._turno_confirmado(dias_desde_hoy=3)
        self.client.force_login(self.productor)
        r = self.client.get(reverse('turnos:mis_turnos'))
        self.assertEqual(r.status_code, 200)
        # El template debe mostrar la columna climática
        self.assertContains(r, 'Recomendación Climática')

    def test_recomendacion_calor_elevado_con_baja_lluvia(self):
        rec = weather_service._recomendar({
            'temperature_2m': 33.5,
            'precipitation_probability': 10,
            'precipitation': 0.0,
            'cloud_cover': 15,
            'wind_gusts_10m': 12.0,
        })
        self.assertIn('Temperatura elevada', rec)
        self.assertIn('lona', rec)


class RampasTests(BaseSoltat):
    """
    Reparto real: Massalin rampas 1-2 y Alliance One 3-5 (prioridad a la mañana);
    Cooperativa las 5 (prioridad a la tarde). Un camión por rampa a la vez.
    """

    def setUp(self):
        super().setUp()
        self.coop = self.empresa
        self.coop.horario_preferente = HorarioPreferente.TARDE
        self.coop.save()
        self.massalin = EmpresaAcopio.objects.create_user(
            username='mas', password='x', cuit_dni='30776655443', nombre='Massalin',
            capacidad_franja_kg=30_000, velocidad_descarga_kg_hora=15_000,
        )
        self.alliance = EmpresaAcopio.objects.create_user(
            username='aot', password='x', cuit_dni='30665544332', nombre='Alliance One',
            capacidad_franja_kg=35_000, velocidad_descarga_kg_hora=17_500,
        )
        for rampa in Rampa.objects.all():
            rampa.empresas.add(self.massalin if rampa.numero <= 2 else self.alliance)
        self.rec_massalin = Recorredor.objects.create_user(
            username='recm', password='x', cuit_dni='20111111113', empresa_asignada=self.massalin,
        )
        CupoProductor.objects.create(productor=self.productor, recorredor=self.rec_massalin, cupo_maximo=500_000)
        self.pasado_manana = self.hoy + timedelta(days=2)
        # Antes de que empiece cualquier tramo del día.
        self.temprano = timezone.make_aware(datetime.combine(self.hoy, time(5, 0)))

    def ocupar(self, empresa, fecha, desde, hasta, numero):
        """Turno confirmado que ocupa la rampa `numero` de `desde` a `hasta` (horas, ej. 6 y 8.5)."""
        t = Turno.objects.create(
            codigo=services.generar_codigo_turno(fecha), productor=self.productor, empresa=empresa,
            kilos_estimados=1_000, fecha_entrega=fecha, estado=EstadoTurno.CONFIRMADO,
            corte_tabaco=CorteTabaco.MEDIANA,
        )
        return services.reservar_bloque(t, int(desde * 60), int(hasta * 60), Rampa.objects.get(numero=numero))

    def ocupar_tarde(self, fecha):
        for numero in range(1, 6):
            self.ocupar(self.coop, fecha, 14, 22, numero)

    def buscar(self, empresa, fecha, kilos=1_000):
        return services.buscar_bloque(empresa, fecha, kilos, ahora=self.temprano)

    def test_turno_asignado_tiene_rampa_y_horario(self):
        turno = self.recorredor.asignar_turno(self.productor, 1_000, CorteTabaco.MEDIANA, 'AA1', self.pasado_manana)
        self.assertIsNotNone(turno.rampa)
        self.assertEqual(turno.hora_inicio, time(14, 0))  # la Cooperativa empieza por la tarde
        self.assertEqual(turno.ocupaciones.count(), 1)

    def test_confirmar_asigna_rampa_y_horario(self):
        turno = self.solicitar(kilos=1_000)
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertIsNotNone(turno.rampa)
        self.assertIsNotNone(turno.hora_inicio)

    def test_cada_empresa_usa_solo_sus_rampas(self):
        self.assertIn(self.buscar(self.massalin, self.pasado_manana)[2].numero, (1, 2))
        self.assertIn(self.buscar(self.alliance, self.pasado_manana)[2].numero, (3, 4, 5))

    def test_una_rampa_atiende_un_camion_a_la_vez(self):
        self.ocupar(self.massalin, self.pasado_manana, 6, 8, 1)
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana)[2].numero, 2)
        self.ocupar(self.massalin, self.pasado_manana, 6, 8, 2)
        inicio, fin, rampa = self.buscar(self.massalin, self.pasado_manana)
        self.assertEqual(inicio, 8 * 60)

    def test_la_base_de_datos_impide_superposiciones_en_una_rampa(self):
        self.ocupar(self.massalin, self.pasado_manana, 6, 9, 1)
        with self.assertRaises(services.HorarioOcupadoError):
            self.ocupar(self.coop, self.pasado_manana, 8.5, 10, 1)  # se pisa de 08:30 a 09:00

    def test_turnos_contiguos_en_la_misma_rampa(self):
        self.ocupar(self.massalin, self.pasado_manana, 6, 8, 1)
        self.ocupar(self.massalin, self.pasado_manana, 8, 10, 1)  # empieza justo cuando termina el otro
        self.assertEqual(Turno.objects.filter(rampa__numero=1).count(), 2)

    def test_un_turno_cancelado_libera_la_rampa(self):
        t = self.ocupar(self.massalin, self.pasado_manana, 6, 8, 1)
        self.ocupar(self.massalin, self.pasado_manana, 6, 8, 2)
        services.cancelar_turno_productor(self.productor, t, 'No llego')
        self.assertFalse(t.ocupaciones.exists())
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana)[:2], (6 * 60, 6 * 60 + 30))

    def test_cada_empresa_busca_primero_en_su_horario_preferente(self):
        self.assertEqual(self.buscar(self.coop, self.pasado_manana)[0], 14 * 60)
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana)[0], 6 * 60)

    def test_cooperativa_no_toma_rampas_de_la_manana_en_dias_futuros(self):
        self.ocupar_tarde(self.pasado_manana)
        self.assertEqual(self.buscar(self.coop, self.pasado_manana), (None, None, None))
        with self.assertRaises(services.CupoExcedidoError) as error:
            self.recorredor.asignar_turno(self.productor, 1_000, CorteTabaco.MEDIANA, 'AA1', self.pasado_manana)
        self.assertIn('Próximo horario disponible', str(error.exception))

    def test_massalin_no_toma_rampas_de_la_tarde_en_dias_futuros(self):
        for numero in (1, 2):
            self.ocupar(self.massalin, self.pasado_manana, 6, 14, numero)
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana), (None, None, None))

    def test_prioridad_por_tramo_un_turno_no_puede_cruzar_a_la_tarde_de_otra(self):
        # Massalin con 60.000 kg (4 h a 15.000 kg/h) y sus rampas ocupadas de 08:00 a 11:00:
        # a la mañana no entran 4 h seguidas, y cruzar a la tarde no puede porque desde
        # las 14:00 la rampa es de la Cooperativa.
        for numero in (1, 2):
            self.ocupar(self.massalin, self.pasado_manana, 8, 11, numero)
        inicio, fin, rampa = self.buscar(self.massalin, self.pasado_manana, kilos=60_000)
        self.assertIsNone(inicio)
        # El mismo día la rampa se libera y sí puede cruzar.
        temprano_ese_dia = timezone.make_aware(datetime.combine(self.pasado_manana, time(5, 0)))
        inicio, fin, rampa = services.buscar_bloque(self.massalin, self.pasado_manana, 60_000,
                                                    ahora=temprano_ese_dia)
        self.assertEqual((inicio, fin), (11 * 60, 15 * 60))

    def test_el_mismo_dia_se_liberan_las_rampas_libres(self):
        self.ocupar_tarde(self.hoy)
        inicio, fin, rampa = self.buscar(self.coop, self.hoy)
        self.assertEqual((inicio, rampa.numero), (6 * 60, 1))

    def test_confirmar_pasa_al_dia_siguiente_si_su_horario_esta_lleno(self):
        self.ocupar_tarde(self.pasado_manana)
        fecha, inicio, fin, rampa = services.buscar_proxima_disponibilidad(
            self.coop, 1_000, desde_fecha=self.pasado_manana,
        )
        self.assertEqual(fecha, self.pasado_manana + timedelta(days=1))
        self.assertEqual(inicio, 14 * 60)

    def test_linea_de_tiempo_de_la_empresa(self):
        self.ocupar(self.massalin, self.pasado_manana, 6, 8, 1)
        grilla = services.ocupacion_rampas(self.coop, self.pasado_manana, hoy=self.hoy)
        rampa1 = grilla['filas'][0]
        self.assertEqual(len(rampa1['bloques']), 1)
        self.assertEqual(rampa1['bloques'][0]['estado'], 'otra')
        self.assertEqual(rampa1['bloques'][0]['ancho'], 12.5)  # 2 h de 16 h
        self.assertEqual(len(rampa1['reservas']), 1)  # la mañana es de Massalin

    def test_paneles_muestran_las_rampas(self):
        self.ocupar(self.massalin, self.hoy, 6, 8, 1)
        self.client.force_login(self.massalin)
        r = self.client.get(reverse('turnos:panel_empresa'))
        self.assertContains(r, 'Sus rampas de descarga')
        self.assertContains(r, 'lt-bloque')
        self.client.force_login(self.operador)
        r = self.client.get(reverse('turnos:panel_operador'))
        self.assertContains(r, 'Rampas de descarga')
        self.assertContains(r, '06:00 a 08:00')


class DuracionTests(BaseSoltat):
    """Turnos de duración variable según los kilos (velocidad 17.500 kg/h por defecto)."""

    def setUp(self):
        super().setUp()
        self.empresa.capacidad_diaria_kg = 300_000
        self.empresa.save()
        CupoProductor.objects.create(productor=self.productor, recorredor=self.recorredor, cupo_maximo=1_000_000)
        self.fecha = self.hoy + timedelta(days=2)

    def test_duracion_segun_kilos_redondeada_a_30_min(self):
        casos = {50: 30, 17_500: 60, 17_501: 90, 35_000: 120, 52_500: 180, 70_000: 240,
                 87_500: 300, 100_000: 360}
        for kilos, minutos in casos.items():
            self.assertEqual(services.duracion_turno(self.empresa, kilos), minutos, kilos)

    def test_duracion_usa_la_velocidad_de_cada_empresa(self):
        self.empresa.velocidad_descarga_kg_hora = 20_000
        self.assertEqual(services.duracion_turno(self.empresa, 100_000), 300)
        self.empresa.velocidad_descarga_kg_hora = 15_000
        self.assertEqual(services.duracion_turno(self.empresa, 100_000), 420)

    def test_100_mil_kilos_en_un_solo_turno_continuo(self):
        turno = self.recorredor.asignar_turno(self.productor, 100_000, CorteTabaco.MEDIANA, 'AA1', self.fecha)
        self.assertEqual((turno.hora_inicio, turno.hora_fin), (time(6, 0), time(12, 0)))
        self.assertEqual(turno.duracion_min, 360)
        self.assertEqual(turno.horario_display, '06:00 a 12:00')
        self.assertEqual(turno.duracion_display, '6 h')
        self.assertEqual(turno.ocupaciones.count(), 12)  # 12 tramos de 30 min en una rampa

    def test_kilos_por_encima_de_la_vieja_franja_se_aceptan(self):
        turno = self.solicitar(kilos=31_000)  # antes superaba la capacidad por franja
        self.assertEqual(turno.estado, EstadoTurno.PENDIENTE)

    def test_bloque_continuo_saltea_huecos_cortos(self):
        rampa = Rampa.objects.get(numero=1)
        Rampa.objects.exclude(pk=rampa.pk).update(activa=False)
        t = Turno.objects.create(
            codigo='X1', productor=self.productor, empresa=self.empresa, kilos_estimados=1_000,
            fecha_entrega=self.fecha, estado=EstadoTurno.CONFIRMADO, corte_tabaco=CorteTabaco.BAJERA,
        )
        services.reservar_bloque(t, 8 * 60, 9 * 60, rampa)  # rampa ocupada de 08:00 a 09:00
        # 35.000 kg = 2 h: el hueco 06:00-08:00 entra justo; 52.500 kg = 3 h ya no.
        self.assertEqual(services.buscar_bloque(self.empresa, self.fecha, 35_000)[:2], (360, 480))
        self.assertEqual(services.buscar_bloque(self.empresa, self.fecha, 52_500)[:2], (540, 720))

    def test_no_supera_el_horario_de_cierre(self):
        rampa = Rampa.objects.get(numero=1)
        Rampa.objects.exclude(pk=rampa.pk).update(activa=False)
        t = Turno.objects.create(
            codigo='X2', productor=self.productor, empresa=self.empresa, kilos_estimados=1_000,
            fecha_entrega=self.fecha, estado=EstadoTurno.CONFIRMADO, corte_tabaco=CorteTabaco.BAJERA,
        )
        services.reservar_bloque(t, 6 * 60, 19 * 60, rampa)
        # Quedan 3 h (19 a 22): 4 h de descarga no entran ese día.
        self.assertEqual(services.buscar_bloque(self.empresa, self.fecha, 70_000), (None, None, None))
        inicio, fin, _ = services.buscar_bloque(self.empresa, self.fecha, 52_500)
        self.assertEqual((inicio, fin), (19 * 60, 22 * 60))

    def test_un_turno_que_no_entra_en_un_dia_se_rechaza(self):
        with self.assertRaises(services.TurnoError):
            self.solicitar(kilos=300_001)  # supera la capacidad diaria
        self.empresa.capacidad_diaria_kg = 300_000
        self.empresa.velocidad_descarga_kg_hora = 15_000
        self.empresa.save()
        with self.assertRaises(services.TurnoError):
            self.solicitar(kilos=250_000)  # 16 h 40 min: más que el horario del acopio

    def test_respeta_la_capacidad_diaria_con_turnos_largos(self):
        self.empresa.capacidad_diaria_kg = 200_000
        self.empresa.save()
        self.recorredor.asignar_turno(self.productor, 100_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        self.recorredor.asignar_turno(self.productor, 90_000, CorteTabaco.MEDIANA, 'B', self.fecha)
        with self.assertRaises(services.CupoExcedidoError):
            self.recorredor.asignar_turno(self.productor, 20_000, CorteTabaco.MEDIANA, 'C', self.fecha)

    def test_turnos_largos_simultaneos_usan_distintas_rampas(self):
        a = self.recorredor.asignar_turno(self.productor, 70_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        b = self.recorredor.asignar_turno(self.productor, 70_000, CorteTabaco.MEDIANA, 'B', self.fecha)
        self.assertEqual(a.hora_inicio, b.hora_inicio)
        self.assertNotEqual(a.rampa, b.rampa)

    def test_turnos_anteriores_sin_horario_se_muestran_con_su_franja(self):
        viejo = self.turno_pasado(1, EstadoTurno.DESCARGADO, kilos_reales=10_000)
        self.assertIsNone(viejo.hora_inicio)
        self.assertEqual(viejo.horario_display, '06:00 a 08:00')
        self.assertEqual(viejo.duracion_display, '2 h')

    def test_ocupacion_por_hora_reparte_los_kilos_del_turno(self):
        self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'A', self.fecha)  # 06 a 08
        horas = services.ocupacion_por_hora(self.empresa, self.fecha)
        self.assertEqual([h['ocupados'] for h in horas[:3]], [17_500, 17_500, 0])
        self.assertEqual(horas[0]['capacidad'], 17_500 * 5)

    def test_solicitar_muestra_la_duracion_estimada(self):
        self.client.force_login(self.productor)
        r = self.client.get(reverse('turnos:solicitar_turno'))
        self.assertContains(r, 'velocidades-descarga')
        self.assertContains(r, 'duracion-estimada')

    def test_mis_turnos_muestra_inicio_fin_y_duracion(self):
        self.recorredor.asignar_turno(self.productor, 100_000, CorteTabaco.MEDIANA, 'AA1', self.fecha)
        self.client.force_login(self.productor)
        r = self.client.get(reverse('turnos:mis_turnos'))
        self.assertContains(r, '06:00 a 12:00')
        self.assertContains(r, '6 h')


class GraficosTests(BaseSoltat):
    def descargado(self, dias, kilos, reales, corte):
        t = self.turno_pasado(dias, EstadoTurno.DESCARGADO, kilos=kilos, kilos_reales=reales)
        t.corte_tabaco = corte
        t.save()
        return t

    def test_graficos_del_productor(self):
        self.descargado(5, 10_000, 9_500, CorteTabaco.BAJERA)
        self.descargado(2, 8_000, 8_400, CorteTabaco.SUPERIOR)
        self.turno_pasado(1, EstadoTurno.CANCELADO)  # no cuenta
        datos = graficos.graficos_productor(self.productor)
        self.assertEqual(datos['productorKilos']['estimados'], [10_000, 8_000])  # orden cronológico
        self.assertEqual(datos['productorKilos']['reales'], [9_500, 8_400])
        self.assertEqual(datos['productorCortes']['valores'], [9_500, 0, 8_400])
        self.assertEqual(len(datos['productorEspera']['minutos']), 2)

    def test_liquidacion_y_precio_solo_con_precio_cargado(self):
        from decimal import Decimal
        con_precio = self.descargado(4, 10_000, 9_500, CorteTabaco.BAJERA)
        services.emitir_comprobante(con_precio, 9_500, Decimal('3.60'), CorteTabaco.BAJERA)
        self.descargado(2, 8_000, 8_400, CorteTabaco.SUPERIOR)  # sin comprobante: no aparece
        datos = graficos.graficos_productor(self.productor)['productorLiquidacion']
        self.assertEqual(datos['importes'], [34_200.0])
        self.assertEqual(datos['precios'], [3.6])
        self.assertEqual(datos['kilos'], [9_500])

    def test_graficos_de_la_empresa(self):
        self.descargado(1, 10_000, 9_000, CorteTabaco.MEDIANA)
        disponibilidad = services.consultar_disponibilidad(self.empresa, self.hoy)
        datos = graficos.graficos_empresa(self.empresa, self.hoy, disponibilidad)
        self.assertEqual(len(datos['empresaDias']['kilos']), graficos.DIAS_HISTORIAL_EMPRESA)
        self.assertEqual(datos['empresaDias']['kilos'][-2], 9_000)  # ayer
        self.assertEqual(datos['empresaDias']['capacidad'], self.empresa.capacidad_diaria_kg)
        self.assertEqual(len(datos['empresaHoras']['kilos']), 16)  # una barra por hora de 06 a 22
        self.assertEqual(datos['empresaCortes']['valores'], [0, 9_000, 0])
        self.assertNotIn('empresaEstados', datos)

    def test_las_paginas_incluyen_los_graficos(self):
        self.client.force_login(self.productor)
        r = self.client.get(reverse('turnos:rendimiento'))
        self.assertContains(r, 'data-grafico="productorKilos"')
        self.assertContains(r, 'id="datos-graficos"')
        self.assertContains(r, 'data-grafico="productorPrecio"')
        self.assertContains(r, 'Tiempo promedio de espera en planta')
        self.assertNotContains(r, 'Ciclo total')
        self.client.force_login(self.empresa)
        r = self.client.get(reverse('turnos:panel_empresa'))
        self.assertContains(r, 'data-grafico="empresaDias"')
        self.assertNotContains(r, 'Estados de los turnos')


class IntegridadAgendaTests(BaseSoltat):
    """Reversión de reservas, cancelaciones, admin, operación en planta y API."""

    def setUp(self):
        super().setUp()
        self.fecha = self.hoy + timedelta(days=3)
        self.empresa.capacidad_diaria_kg = 300_000
        self.empresa.save()
        CupoProductor.objects.create(productor=self.productor, recorredor=self.recorredor, cupo_maximo=500_000)
        self.admin = Administrador.objects.create_user(username='adm', password='x', cuit_dni='20000000001')

    def turno_sin_horario(self, codigo='R1'):
        return Turno.objects.create(
            codigo=codigo, productor=self.productor, empresa=self.empresa, kilos_estimados=1_000,
            fecha_entrega=self.fecha, estado=EstadoTurno.CONFIRMADO, corte_tabaco=CorteTabaco.BAJERA,
        )

    def test_si_falla_un_tramo_se_revierte_toda_la_reserva(self):
        rampa = Rampa.objects.get(numero=1)
        services.reservar_bloque(self.turno_sin_horario('A'), 9 * 60, 10 * 60, rampa)  # ocupa 09:00-10:00
        turno = self.turno_sin_horario('B')
        with self.assertRaises(services.HorarioOcupadoError):
            services.reservar_bloque(turno, 8 * 60, 11 * 60, rampa)  # choca en el medio del bloque
        self.assertEqual(turno.ocupaciones.count(), 0)
        self.assertIsNone(turno.hora_inicio)  # el objeto en memoria vuelve a como estaba
        self.assertIsNone(turno.rampa)
        turno.refresh_from_db()
        self.assertEqual((turno.hora_inicio, turno.hora_fin, turno.rampa_id), (None, None, None))

    def test_bloqueo_de_la_base_se_informa_como_error_de_negocio(self):
        from unittest import mock
        from django.db import OperationalError
        with mock.patch.object(services, 'buscar_bloque', side_effect=OperationalError('database is locked')):
            with self.assertRaises(services.AgendaOcupadaError):
                self.recorredor.asignar_turno(self.productor, 1_000, CorteTabaco.MEDIANA, 'A', self.fecha)

    def test_bloqueo_al_solicitar_no_da_error_500(self):
        from unittest import mock
        from django.db import OperationalError
        self.client.force_login(self.productor)
        with mock.patch.object(services, 'remanente_cupo', side_effect=OperationalError('database is locked')):
            r = self.client.post(reverse('turnos:solicitar_turno'), {
                'empresa': self.empresa.pk, 'kilos_estimados': 5_000,
                'corte_tabaco': CorteTabaco.MEDIANA, 'patente_vehiculo': '',
            })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'procesando otras reservas')

    def test_cancelacion_del_recorredor_libera_la_rampa(self):
        turno = self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        self.assertEqual(turno.ocupaciones.count(), 4)
        services.cancelar_turno_recorredor(self.recorredor, turno, 'Lluvia')
        self.assertEqual(turno.ocupaciones.count(), 0)
        otro = self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'B', self.fecha)
        self.assertEqual((otro.rampa_id, otro.hora_inicio), (turno.rampa_id, turno.hora_inicio))

    def test_cancelacion_por_ausencia_libera_la_rampa(self):
        turno = self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        services.cancelar_turnos_vencidos(hoy=self.fecha + timedelta(days=1))
        turno.refresh_from_db()
        self.assertEqual(turno.estado, EstadoTurno.CANCELADO)
        self.assertTrue(turno.cancelado_por_ausencia)
        self.assertEqual(turno.ocupaciones.count(), 0)

    def test_ingreso_y_pesaje_de_un_turno_largo(self):
        turno = self.recorredor.asignar_turno(self.productor, 100_000, CorteTabaco.MEDIANA, 'AB1', self.manana)
        turno.fecha_entrega = self.hoy  # se pasa a hoy para registrar el ingreso sin depender de la hora
        turno.save(update_fields=['fecha_entrega'])
        turno.ocupaciones.update(fecha=self.hoy)
        self.operador.registrar_ingreso_garita(turno)
        self.operador.registrar_pesaje_y_descarga(turno, 98_500, CorteTabaco.MEDIANA, '3.50')
        turno.refresh_from_db()
        self.assertEqual(turno.estado, EstadoTurno.DESCARGADO)
        self.assertEqual(turno.duracion_min, 360)
        self.assertEqual(turno.ocupaciones.count(), 12)  # el historial conserva la ocupación de la rampa
        self.assertEqual(turno.comprobante.peso_real, 98_500)

    def test_admin_no_permite_editar_la_agenda_del_turno(self):
        turno = self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        self.client.force_login(self.admin)
        r = self.client.get(reverse('admin:turnos_turno_change', args=[turno.pk]))
        self.assertEqual(r.status_code, 200)
        for campo in ('hora_inicio', 'hora_fin', 'duracion_min', 'rampa', 'estado', 'fecha_entrega',
                      'kilos_estimados', 'empresa'):
            self.assertNotIn(f'name="{campo}"', r.content.decode(), campo)
        # Un POST con otros valores no cambia la agenda.
        self.client.post(reverse('admin:turnos_turno_change', args=[turno.pk]), {
            'productor': self.productor.pk, 'recorredor': self.recorredor.pk,
            'patente_vehiculo': 'ZZ999ZZ', 'corte_tabaco': CorteTabaco.MEDIANA,
            'hora_inicio': '20:00', 'rampa': Rampa.objects.get(numero=5).pk, 'estado': 'CANCELADO',
        })
        turno.refresh_from_db()
        self.assertEqual(turno.patente_vehiculo, 'ZZ999ZZ')
        self.assertEqual((turno.hora_inicio, turno.estado), (time(6, 0), EstadoTurno.CONFIRMADO))
        self.assertEqual(turno.ocupaciones.count(), 4)

    def test_admin_no_permite_borrar_tramos_sueltos(self):
        turno = self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        tramo = turno.ocupaciones.first()
        self.client.force_login(self.admin)
        r = self.client.post(reverse('admin:turnos_ocupacionrampa_delete', args=[tramo.pk]), {'post': 'yes'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(turno.ocupaciones.count(), 4)

    def test_admin_puede_borrar_un_turno_y_libera_su_rampa(self):
        turno = self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        self.client.force_login(self.admin)
        r = self.client.post(reverse('admin:turnos_turno_delete', args=[turno.pk]), {'post': 'yes'})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Turno.objects.filter(pk=turno.pk).exists())
        self.assertFalse(OcupacionRampa.objects.filter(turno_id=turno.pk).exists())

    def test_api_de_disponibilidad_con_claves_nuevas_y_anteriores(self):
        self.recorredor.asignar_turno(self.productor, 35_000, CorteTabaco.MEDIANA, 'A', self.fecha)
        self.client.force_login(self.empresa)
        r = self.client.get(reverse('turnos:api_disponibilidad'),
                            {'empresa_id': self.empresa.pk, 'fecha': self.fecha.isoformat()})
        datos = r.json()
        self.assertEqual(datos['velocidad_descarga'], 17_500)
        self.assertEqual(len(datos['horas']), 16)
        self.assertEqual([h['ocupados'] for h in datos['horas'][:3]], [17_500, 17_500, 0])
        # Compatibilidad con el formato anterior
        self.assertEqual(datos['capacidad_franja'], self.empresa.capacidad_franja_kg)
        self.assertEqual(len(datos['franjas']), 8)
        self.assertEqual(datos['franjas'][0]['valor'], '06:00 - 08:00')
        self.assertEqual(datos['franjas'][0]['ocupados'], 35_000)
        self.assertEqual(datos['kilos_ocupados'], 35_000)

    def test_hoy_el_primer_horario_es_el_proximo_tramo_de_30_min(self):
        for hora, minuto, esperado in ((13, 0, 13 * 60 + 30), (13, 10, 13 * 60 + 30), (13, 30, 14 * 60),
                                       (5, 0, 6 * 60), (21, 30, None)):
            ahora = timezone.make_aware(datetime.combine(self.hoy, time(hora, minuto)))
            inicio, _, _ = services.buscar_bloque(self.empresa, self.hoy, 1_000, ahora=ahora)
            self.assertEqual(inicio, esperado, f'{hora}:{minuto:02d}')


class AgendaCompartidaTests(BaseSoltat):
    """Línea de tiempo y capacidad con varias empresas en rampas compartidas."""

    def setUp(self):
        super().setUp()
        self.empresa.horario_preferente = HorarioPreferente.TARDE
        self.empresa.velocidad_descarga_kg_hora = 20_000
        self.empresa.save()
        self.massalin = EmpresaAcopio.objects.create_user(
            username='mas', password='x', cuit_dni='30776655443', nombre='Massalin',
            velocidad_descarga_kg_hora=15_000,
        )
        for rampa in Rampa.objects.filter(numero__lte=2):
            rampa.empresas.add(self.massalin)
        self.fecha = self.hoy + timedelta(days=4)

    def reservar(self, empresa, codigo, desde, hasta, numero):
        t = Turno.objects.create(
            codigo=codigo, productor=self.productor, empresa=empresa, kilos_estimados=10_000,
            fecha_entrega=self.fecha, estado=EstadoTurno.CONFIRMADO, corte_tabaco=CorteTabaco.MEDIANA,
        )
        return services.reservar_bloque(t, desde * 60, hasta * 60, Rampa.objects.get(numero=numero))

    def test_linea_de_tiempo_del_centro_con_varias_empresas(self):
        self.reservar(self.massalin, 'M1', 6, 10, 1)
        self.reservar(self.empresa, 'C1', 14, 19, 1)  # misma rampa, otra empresa, a la tarde
        self.reservar(self.empresa, 'C2', 15, 17, 4)
        centro = services.ocupacion_rampas_centro(self.fecha)
        rampa1 = centro['filas'][0]
        self.assertEqual([b['turno'].codigo for b in rampa1['bloques']], ['M1', 'C1'])
        self.assertEqual([b['ancho'] for b in rampa1['bloques']], [25.0, 31.25])
        self.assertEqual([b['turno'].codigo for b in centro['filas'][3]['bloques']], ['C2'])

    def test_linea_de_tiempo_de_cada_empresa(self):
        self.reservar(self.massalin, 'M1', 6, 10, 1)
        self.reservar(self.empresa, 'C1', 14, 19, 1)
        coop = services.ocupacion_rampas(self.empresa, self.fecha, hoy=self.hoy)
        self.assertEqual([b['estado'] for b in coop['filas'][0]['bloques']], ['otra', 'propia'])
        self.assertEqual(len(coop['filas'][0]['reservas']), 1)   # rampa 1: la mañana es de Massalin
        self.assertEqual(len(coop['filas'][2]['reservas']), 0)   # rampa 3: solo de la Cooperativa
        massalin = services.ocupacion_rampas(self.massalin, self.fecha, hoy=self.hoy)
        self.assertEqual(len(massalin['filas']), 2)
        self.assertEqual([b['estado'] for b in massalin['filas'][0]['bloques']], ['propia', 'otra'])

    def test_capacidad_por_hora_segun_rampas_disponibles(self):
        self.reservar(self.massalin, 'M1', 6, 10, 1)
        coop = {h['valor']: h['capacidad'] for h in services.ocupacion_por_hora(self.empresa, self.fecha)}
        # 06:00: las rampas 1 y 2 son de Massalin a la mañana -> la Cooperativa tiene 3 rampas.
        self.assertEqual(coop['06:00'], 3 * 20_000)
        # 14:00: tiene sus 5 rampas.
        self.assertEqual(coop['14:00'], 5 * 20_000)
        massalin = {h['valor']: h['capacidad'] for h in services.ocupacion_por_hora(self.massalin, self.fecha)}
        # 06:00: rampa 1 con su propio turno + rampa 2 libre = 2 rampas; 14:00: ninguna (tarde de la Cooperativa).
        self.assertEqual(massalin['06:00'], 2 * 15_000)
        self.assertEqual(massalin['14:00'], 0)

    def test_capacidad_por_hora_descuenta_turnos_de_otra_empresa(self):
        self.reservar(self.empresa, 'C1', 14, 16, 1)
        self.reservar(self.empresa, 'C2', 14, 16, 2)
        hoy_mismo = self.fecha  # el mismo día las rampas se liberan para Massalin
        capacidades = services.capacidad_por_hora(self.massalin, hoy_mismo, hoy=hoy_mismo)
        self.assertEqual(capacidades[14 * 60], 0)            # ocupadas por la Cooperativa
        self.assertEqual(capacidades[16 * 60], 2 * 15_000)   # liberadas el mismo día


class ConcurrenciaTests(TransactionTestCase):
    """
    Reservas simultáneas reales (hilos con su propia conexión). Usa una base
    SQLite en archivo: con transaction_mode IMMEDIATE las reservas se atienden
    de a una, sin superposiciones ni capacidad diaria excedida.
    """

    def setUp(self):
        if connection.vendor == 'sqlite' and connection.settings_dict['NAME'].startswith(('file:memorydb', ':memory:')):
            self.skipTest('La prueba de concurrencia necesita una base de datos en archivo.')
        self.empresa = EmpresaAcopio.objects.create_user(
            username='mas', password='x', cuit_dni='30776655443', nombre='Massalin',
            capacidad_diaria_kg=200_000, velocidad_descarga_kg_hora=15_000,
        )
        Rampa.objects.all().delete()
        for numero in (1, 2):
            Rampa.objects.create(numero=numero).empresas.add(self.empresa)
        self.recorredor = Recorredor.objects.create_user(
            username='rec', password='x', cuit_dni='20111111113', empresa_asignada=self.empresa,
        )
        self.productores = []
        for i in range(4):
            p = Productor.objects.create_user(username=f'p{i}', password='x', cuit_dni=f'2099999990{i}')
            CupoProductor.objects.create(productor=p, recorredor=self.recorredor, cupo_maximo=1_000_000)
            self.productores.append(p)
        self.fecha = timezone.localdate() + timedelta(days=5)

    def en_paralelo(self, funcion, argumentos):
        barrera = threading.Barrier(len(argumentos))
        resultados = []

        def correr(arg):
            try:
                barrera.wait()
                resultados.append(('ok', funcion(arg)))
            except Exception as error:  # noqa: BLE001 - se informa el tipo en el test
                resultados.append(('error', error))
            finally:
                connections.close_all()

        hilos = [threading.Thread(target=correr, args=(a,)) for a in argumentos]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        return resultados

    def test_reservas_simultaneas_sin_superposicion_ni_exceso_de_capacidad(self):
        # 4 turnos de 60.000 kg (4 h) a la vez: entran 3 (180.000 de 200.000 kg).
        resultados = self.en_paralelo(
            lambda p: services.asignar_turno(self.recorredor, p, 60_000, CorteTabaco.MEDIANA, 'AA1', self.fecha),
            self.productores,
        )
        ok = [r for estado, r in resultados if estado == 'ok']
        errores = [r for estado, r in resultados if estado == 'error']
        self.assertEqual(len(ok), 3)
        self.assertEqual(len(errores), 1)
        self.assertIsInstance(errores[0], services.CupoExcedidoError)
        tramos = list(OcupacionRampa.objects.values_list('rampa_id', 'fecha', 'inicio'))
        self.assertEqual(len(tramos), len(set(tramos)))  # ningún tramo repetido
        self.assertEqual(len(tramos), 3 * 8)

    def test_dos_reservas_del_mismo_tramo_a_la_vez(self):
        rampa = Rampa.objects.get(numero=1)
        turnos = [Turno.objects.create(
            codigo=f'C{i}', productor=self.productores[i], empresa=self.empresa, kilos_estimados=1_000,
            fecha_entrega=self.fecha, estado=EstadoTurno.CONFIRMADO, corte_tabaco=CorteTabaco.BAJERA,
        ) for i in range(2)]
        resultados = self.en_paralelo(lambda t: services.reservar_bloque(t, 600, 720, rampa), turnos)
        estados = sorted(estado for estado, _ in resultados)
        self.assertEqual(estados, ['error', 'ok'])
        error = next(r for estado, r in resultados if estado == 'error')
        self.assertIsInstance(error, services.HorarioOcupadoError)
        con_tramos = [t for t in Turno.objects.filter(pk__in=[t.pk for t in turnos]) if t.ocupaciones.exists()]
        self.assertEqual(len(con_tramos), 1)
        sin_tramos = Turno.objects.exclude(pk=con_tramos[0].pk).get(pk__in=[t.pk for t in turnos])
        self.assertIsNone(sin_tramos.rampa_id)  # el que perdió no quedó a medias


class MigracionDuracionTests(TransactionTestCase):
    """Las migraciones de duración variable conservan los turnos existentes."""

    antes = [('accounts', '0002_empresa_horario_preferente'), ('turnos', '0007_cargar_rampas')]
    despues = [('accounts', '0003_empresa_velocidad_descarga'), ('turnos', '0010_orden_turnos_uniforme')]

    def setUp(self):
        from django.db.migrations.executor import MigrationExecutor
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.antes)
        apps = self.executor.loader.project_state(self.antes).apps
        Empresa = apps.get_model('accounts', 'EmpresaAcopio')
        Productor_ = apps.get_model('accounts', 'Productor')
        Turno_ = apps.get_model('turnos', 'Turno')
        Rampa_ = apps.get_model('turnos', 'Rampa')
        empresa = Empresa.objects.create(username='e', cuit_dni='30111111111', nombre='Massalin',
                                         password='x', capacidad_franja_kg=30_000, telefono='')
        productor = Productor_.objects.create(username='p', cuit_dni='20111111110', password='x', telefono='')
        rampa, _ = Rampa_.objects.get_or_create(numero=1)
        hoy = timezone.localdate()
        datos = [
            ('V1', EstadoTurno.CONFIRMADO, hoy + timedelta(days=2), '08:00 - 10:00', rampa),
            ('V2', EstadoTurno.DESCARGADO, hoy - timedelta(days=5), '06:00 - 08:00', rampa),
            ('V3', EstadoTurno.CANCELADO, hoy + timedelta(days=2), '10:00 - 12:00', rampa),
            ('V4', EstadoTurno.PENDIENTE, None, None, None),
        ]
        for codigo, estado, fecha, franja, r in datos:
            Turno_.objects.create(codigo=codigo, productor=productor, empresa=empresa, kilos_estimados=12_000,
                                  kilos_reales=11_800 if estado == EstadoTurno.DESCARGADO else None,
                                  fecha_entrega=fecha, franja_horaria=franja, rampa=r, estado=estado,
                                  corte_tabaco=CorteTabaco.MEDIANA, patente_vehiculo='', fue_confirmado=True)
        self.columnas = ('codigo', 'estado', 'fecha_entrega', 'franja_horaria', 'rampa_id', 'kilos_estimados',
                         'kilos_reales', 'fue_confirmado')
        self.originales = list(Turno_.objects.order_by('codigo').values_list(*self.columnas))

    def tearDown(self):
        from django.db.migrations.executor import MigrationExecutor
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_migrar_conserva_los_turnos_y_registra_sus_tramos(self):
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.despues)
        apps = executor.loader.project_state(self.despues).apps
        Turno_ = apps.get_model('turnos', 'Turno')
        Ocupacion = apps.get_model('turnos', 'OcupacionRampa')
        Empresa = apps.get_model('accounts', 'EmpresaAcopio')
        self.assertEqual(list(Turno_.objects.order_by('codigo').values_list(*self.columnas)), self.originales)
        self.assertFalse(Turno_.objects.filter(hora_inicio__isnull=False).exists())
        # Activos con rampa (V1 confirmado y V2 descargado): 4 tramos cada uno; cancelado y pendiente, ninguno.
        self.assertEqual(Ocupacion.objects.filter(turno__codigo='V1').count(), 4)
        self.assertEqual(Ocupacion.objects.filter(turno__codigo='V2').count(), 4)
        self.assertFalse(Ocupacion.objects.filter(turno__codigo__in=['V3', 'V4']).exists())
        self.assertEqual(sorted(str(t) for t in Ocupacion.objects.filter(turno__codigo='V1')
                                .values_list('inicio', flat=True)),
                         ['08:00:00', '08:30:00', '09:00:00', '09:30:00'])
        self.assertEqual(Empresa.objects.get(nombre='Massalin').velocidad_descarga_kg_hora, 15_000)
