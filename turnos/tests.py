from datetime import datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmpresaAcopio, OperadorAcopio, Productor, Recorredor, Usuario
from . import services
from .choices import CorteTabaco, EstadoTurno
from .models import Comprobante, CupoProductor, RegistroOperador, Turno


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

    def solicitar(self, kilos=10_000, fecha=None, productor=None):
        return services.solicitar_turno(
            productor or self.productor, self.empresa, kilos, CorteTabaco.MEDIANA, 'ae123cd', fecha or self.manana,
        )

    def turno_pasado(self, dias, estado, kilos=10_000, kilos_reales=None, con_comprobante=False):
        t = Turno.objects.create(
            codigo=services.generar_codigo_turno(self.hoy), productor=self.productor, empresa=self.empresa,
            kilos_estimados=kilos, kilos_reales=kilos_reales, patente_vehiculo='AA000AA',
            fecha_entrega=self.hoy - timedelta(days=dias), franja_horaria='06:00 - 08:00',
            estado=estado, corte_tabaco=CorteTabaco.BAJERA,
        )
        if con_comprobante:
            services.emitir_comprobante(t)
        return t

    def turno_confirmado_hoy(self, kilos=10_000):
        """Turno confirmado para hoy, sin depender de la hora en que corre el test."""
        t = self.turno_pasado(0, EstadoTurno.CONFIRMADO, kilos=kilos, con_comprobante=True)
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
    def test_productor_solicita_turno_pendiente_sin_comprobante(self):
        turno = self.solicitar()
        self.assertEqual(turno.estado, EstadoTurno.PENDIENTE)
        self.assertEqual(turno.franja_horaria, '06:00 - 08:00')
        self.assertEqual(turno.patente_vehiculo, 'AE123CD')
        self.assertFalse(Comprobante.objects.filter(turno=turno).exists())

    def test_recorredor_confirma_y_se_emite_comprobante(self):
        turno = self.solicitar()
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertEqual(turno.estado, EstadoTurno.CONFIRMADO)
        self.assertEqual(turno.recorredor, self.recorredor)
        self.assertEqual(turno.comprobante.codigo_turno, turno.codigo)

    def test_turno_asignado_por_recorredor_nace_confirmado(self):
        turno = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.SUPERIOR, 'AB123', self.manana)
        self.assertEqual(turno.estado, EstadoTurno.CONFIRMADO)
        self.assertTrue(Comprobante.objects.filter(turno=turno).exists())

    def test_asigna_la_siguiente_franja_cuando_la_primera_se_llena(self):
        CupoProductor.objects.create(productor=self.productor, recorredor=self.recorredor, cupo_maximo=100_000)
        primero = self.solicitar(kilos=25_000)
        segundo = self.solicitar(kilos=10_000)
        tercero = self.solicitar(kilos=5_000)
        self.assertEqual(primero.franja_horaria, '06:00 - 08:00')
        self.assertEqual(segundo.franja_horaria, '08:00 - 10:00')
        self.assertEqual(tercero.franja_horaria, '06:00 - 08:00')  # entra en el lugar libre

    def test_respeta_la_capacidad_diaria(self):
        self.empresa.capacidad_diaria_kg = 200_000
        self.empresa.capacidad_franja_kg = 30_000
        self.empresa.save()
        otro = Productor.objects.create_user(username='p2', password='x', cuit_dni='20999999999')
        CupoProductor.objects.create(productor=otro, recorredor=self.recorredor, cupo_maximo=1_000_000)
        for _ in range(6):
            self.solicitar(kilos=30_000, productor=otro)
        self.solicitar(kilos=20_000, productor=otro)  # total 200.000 kg
        with self.assertRaises(services.CupoExcedidoError):
            self.solicitar(kilos=1_000, productor=otro)

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

    def test_turno_mayor_a_una_franja_se_rechaza(self):
        with self.assertRaises(services.TurnoError):
            self.solicitar(kilos=31_000)

    def test_hoy_saltea_franjas_que_ya_empezaron(self):
        ahora = timezone.make_aware(datetime.combine(self.hoy, time(13, 30)))
        franja = services.buscar_franja(self.empresa, self.hoy, 1_000, ahora=ahora)
        self.assertEqual(franja, '14:00 - 16:00')


class CumplimientoTests(BaseSoltat):
    def test_sin_incumplimientos_es_bueno(self):
        self.turno_pasado(5, EstadoTurno.DESCARGADO, kilos_reales=10_500)
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['nivel'], 'BUENO')

    def test_no_presentarse_baja_a_regular_y_restringe_franjas(self):
        self.turno_pasado(3, EstadoTurno.CONFIRMADO)
        cumplimiento = services.evaluar_cumplimiento(self.productor)
        self.assertEqual(cumplimiento['nivel'], 'REGULAR')
        self.assertEqual(self.solicitar().franja_horaria, '12:00 - 14:00')

    def test_desvio_mayor_al_20_por_ciento_cuenta(self):
        self.turno_pasado(3, EstadoTurno.DESCARGADO, kilos=10_000, kilos_reales=7_500)
        self.turno_pasado(4, EstadoTurno.DESCARGADO, kilos=10_000, kilos_reales=8_500)  # 15 %: no cuenta
        self.assertEqual(services.evaluar_cumplimiento(self.productor)['cantidad'], 1)

    def test_cancelar_confirmado_cuenta_y_cancelacion_del_recorredor_no(self):
        self.turno_pasado(2, EstadoTurno.CANCELADO, con_comprobante=True)   # canceló el productor
        self.turno_pasado(3, EstadoTurno.CANCELADO, con_comprobante=False)  # canceló el recorredor
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
        self.assertTrue(Comprobante.objects.filter(turno=turno).exists())
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
            self.operador.registrar_pesaje_y_descarga(turno, 9_800, CorteTabaco.MEDIANA)

    def test_ingreso_solo_el_dia_del_turno(self):
        turno = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.MEDIANA, 'AB1', self.manana)
        with self.assertRaises(services.TurnoError):
            self.operador.registrar_ingreso_garita(turno)

    def test_kilos_reales_liberan_capacidad(self):
        turno = self.turno_confirmado_hoy()
        self.operador.registrar_ingreso_garita(turno)
        self.operador.registrar_pesaje_y_descarga(turno, 8_000, CorteTabaco.MEDIANA)
        self.assertEqual(self.empresa.calcular_kilos_ocupados(self.hoy), 8_000)


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
            ('coop', ['turnos:panel_empresa']),
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
            'empresa': self.empresa.pk, 'fecha_entrega': self.manana.isoformat(),
            'kilos_estimados': 8000, 'corte_tabaco': 'BAJERA', 'patente_vehiculo': 'ab123cd',
        })
        turno = Turno.objects.get()
        self.assertRedirects(r, reverse('turnos:comprobante', args=[turno.codigo]))

        self.client.force_login(self.recorredor)
        r = self.client.post(reverse('turnos:confirmar_turno', args=[turno.id]), follow=True)
        self.assertContains(r, 'https://wa.me/543884912345')
