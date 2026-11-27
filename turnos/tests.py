from datetime import datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmpresaAcopio, OperadorAcopio, Productor, Recorredor, Usuario
from . import services
from . import weather as weather_service
from .choices import CorteTabaco, EstadoTurno, HorarioPreferente
from .models import Comprobante, CupoProductor, Rampa, RegistroOperador, Turno


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

    def test_asigna_la_siguiente_franja_cuando_la_primera_se_llena(self):
        CupoProductor.objects.create(productor=self.productor, recorredor=self.recorredor, cupo_maximo=100_000)
        primero = self.recorredor.asignar_turno(self.productor, 25_000, CorteTabaco.MEDIANA, 'AA1', self.manana)
        segundo = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.MEDIANA, 'AA2', self.manana)
        tercero = self.recorredor.asignar_turno(self.productor, 5_000, CorteTabaco.MEDIANA, 'AA3', self.manana)
        self.assertEqual(primero.franja_horaria, '06:00 - 08:00')
        self.assertEqual(segundo.franja_horaria, '08:00 - 10:00')
        self.assertEqual(tercero.franja_horaria, '06:00 - 08:00')  # entra en el lugar libre

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

    def test_turno_mayor_a_una_franja_se_rechaza(self):
        with self.assertRaises(services.TurnoError):
            self.solicitar(kilos=31_000)

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
        franja = services.buscar_franja(self.empresa, self.hoy, 1_000, ahora=ahora)
        self.assertEqual(franja, '14:00 - 16:00')


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
        self.assertGreaterEqual(turno.fecha_entrega, fecha_lejana)
        self.assertEqual(turno.franja_horaria, '08:00 - 10:00')

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
        turno = self.recorredor.asignar_turno(self.productor, 10_000, CorteTabaco.MEDIANA, '', self.hoy)
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
    Cooperativa las 5 (prioridad a la tarde). Un turno por rampa y franja.
    """

    def setUp(self):
        super().setUp()
        self.coop = self.empresa
        self.coop.horario_preferente = HorarioPreferente.TARDE
        self.coop.save()
        self.massalin = EmpresaAcopio.objects.create_user(
            username='mas', password='x', cuit_dni='30776655443', nombre='Massalin',
            capacidad_franja_kg=30_000,
        )
        self.alliance = EmpresaAcopio.objects.create_user(
            username='aot', password='x', cuit_dni='30665544332', nombre='Alliance One',
            capacidad_franja_kg=30_000,
        )
        for rampa in Rampa.objects.all():
            rampa.empresas.add(self.massalin if rampa.numero <= 2 else self.alliance)
        self.rec_massalin = Recorredor.objects.create_user(
            username='recm', password='x', cuit_dni='20111111113', empresa_asignada=self.massalin,
        )
        CupoProductor.objects.create(productor=self.productor, recorredor=self.rec_massalin, cupo_maximo=100_000)
        self.pasado_manana = self.hoy + timedelta(days=2)
        # Antes de que empiece cualquier franja del día.
        self.temprano = timezone.make_aware(datetime.combine(self.hoy, time(5, 0)))

    def ocupar(self, empresa, fecha, franja, numero):
        return Turno.objects.create(
            codigo=services.generar_codigo_turno(fecha), productor=self.productor, empresa=empresa,
            kilos_estimados=1_000, fecha_entrega=fecha, franja_horaria=franja,
            rampa=Rampa.objects.get(numero=numero), estado=EstadoTurno.CONFIRMADO,
            corte_tabaco=CorteTabaco.MEDIANA,
        )

    def ocupar_tarde(self, fecha):
        for franja in ('14:00 - 16:00', '16:00 - 18:00', '18:00 - 20:00', '20:00 - 22:00'):
            for numero in range(1, 6):
                self.ocupar(self.coop, fecha, franja, numero)

    def buscar(self, empresa, fecha, kilos=1_000):
        return services.buscar_franja_y_rampa(empresa, fecha, kilos, ahora=self.temprano)

    def test_turno_asignado_tiene_rampa(self):
        turno = self.recorredor.asignar_turno(self.productor, 1_000, CorteTabaco.MEDIANA, 'AA1', self.pasado_manana)
        self.assertIsNotNone(turno.rampa)

    def test_confirmar_asigna_rampa(self):
        turno = self.solicitar(kilos=1_000)
        self.recorredor.confirmar_turno(turno)
        turno.refresh_from_db()
        self.assertIsNotNone(turno.rampa)

    def test_cada_empresa_usa_solo_sus_rampas(self):
        franja, rampa = self.buscar(self.massalin, self.pasado_manana)
        self.assertIn(rampa.numero, (1, 2))
        franja, rampa = self.buscar(self.alliance, self.pasado_manana)
        self.assertIn(rampa.numero, (3, 4, 5))

    def test_un_turno_por_rampa_y_franja(self):
        self.ocupar(self.massalin, self.pasado_manana, '06:00 - 08:00', 1)
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana), ('06:00 - 08:00', Rampa.objects.get(numero=2)))
        self.ocupar(self.massalin, self.pasado_manana, '06:00 - 08:00', 2)
        franja, rampa = self.buscar(self.massalin, self.pasado_manana)
        self.assertEqual(franja, '08:00 - 10:00')

    def test_la_base_de_datos_impide_dos_turnos_en_la_misma_rampa_y_franja(self):
        from django.db import IntegrityError, transaction
        self.ocupar(self.massalin, self.pasado_manana, '06:00 - 08:00', 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.ocupar(self.coop, self.pasado_manana, '06:00 - 08:00', 1)

    def test_un_turno_cancelado_libera_la_rampa(self):
        t = self.ocupar(self.massalin, self.pasado_manana, '06:00 - 08:00', 1)
        t.estado = EstadoTurno.CANCELADO
        t.save()
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana), ('06:00 - 08:00', Rampa.objects.get(numero=1)))

    def test_cada_empresa_busca_primero_en_su_horario_preferente(self):
        self.assertEqual(self.buscar(self.coop, self.pasado_manana)[0], '14:00 - 16:00')
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana)[0], '06:00 - 08:00')

    def test_cooperativa_no_toma_rampas_de_la_manana_en_dias_futuros(self):
        self.ocupar_tarde(self.pasado_manana)
        self.assertEqual(self.buscar(self.coop, self.pasado_manana), (None, None))
        with self.assertRaises(services.CupoExcedidoError):
            self.recorredor.asignar_turno(self.productor, 1_000, CorteTabaco.MEDIANA, 'AA1', self.pasado_manana)

    def test_massalin_no_toma_rampas_de_la_tarde_en_dias_futuros(self):
        for franja in ('06:00 - 08:00', '08:00 - 10:00', '10:00 - 12:00', '12:00 - 14:00'):
            for numero in (1, 2):
                self.ocupar(self.massalin, self.pasado_manana, franja, numero)
        self.assertEqual(self.buscar(self.massalin, self.pasado_manana), (None, None))

    def test_el_mismo_dia_se_liberan_las_rampas_libres(self):
        self.ocupar_tarde(self.hoy)
        franja, rampa = self.buscar(self.coop, self.hoy)
        self.assertEqual(franja, '06:00 - 08:00')
        self.assertEqual(rampa.numero, 1)

    def test_confirmar_pasa_al_dia_siguiente_si_su_horario_esta_lleno(self):
        self.ocupar_tarde(self.pasado_manana)
        fecha, franja, rampa = services.buscar_proxima_disponibilidad(
            self.coop, 1_000, desde_fecha=self.pasado_manana,
        )
        self.assertEqual(fecha, self.pasado_manana + timedelta(days=1))
        self.assertEqual(franja, '14:00 - 16:00')

    def test_ocupacion_de_rampas_de_la_empresa(self):
        self.ocupar(self.massalin, self.pasado_manana, '06:00 - 08:00', 1)
        grilla = services.ocupacion_rampas(self.coop, self.pasado_manana, hoy=self.hoy)
        fila = grilla['filas'][0]
        self.assertFalse(fila['preferente'])
        estados = [c['estado'] for c in fila['celdas']]
        self.assertEqual(estados, ['otra', 'reservada', 'reservada', 'reservada', 'reservada'])
        self.assertTrue(all(c['estado'] == 'libre' for c in grilla['filas'][4]['celdas']))

    def test_paneles_muestran_las_rampas(self):
        self.ocupar(self.massalin, self.hoy, '06:00 - 08:00', 1)
        self.client.force_login(self.massalin)
        r = self.client.get(reverse('turnos:panel_empresa'))
        self.assertContains(r, 'Sus rampas de descarga')
        self.client.force_login(self.operador)
        r = self.client.get(reverse('turnos:panel_operador'))
        self.assertContains(r, 'Rampas de descarga')
