from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from .models import Alerta, Categoria, ItemReembolso, ItemVenta, MovimientoInventario, PerfilUsuario, Producto, Reembolso, Venta


class FlujoVentaTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(username='admin', password='test-pass-123')
        self.user = get_user_model().objects.create_user(username='seller', password='test-pass-123')
        self.producto = Producto(sku='SKU-001', nombre='Producto de prueba', precio=Decimal('10.00'), cost_price=Decimal('6.00'), stock=5, min_stock=3)
        self.producto.save(user=self.admin)

    def crear_venta(self, cantidad=2):
        venta = Venta.objects.create(receipt_number='REC-001', seller=self.user, payment_method='CASH')
        ItemVenta.objects.create(sale=venta, product=self.producto, quantity=cantidad)
        return venta

    def test_borrador_no_descuenta_stock(self):
        self.crear_venta()
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 5)

    def test_completar_calcula_total_descuenta_stock_y_crea_movimiento(self):
        venta = self.crear_venta()
        venta.completar()
        self.producto.refresh_from_db()
        venta.refresh_from_db()
        item = venta.items.get()
        self.assertEqual(self.producto.stock, 3)
        self.assertEqual(venta.total, Decimal('20.00'))
        self.assertEqual(item.product_name, 'Producto de prueba')
        self.assertEqual(item.unit_price, Decimal('10.00'))
        self.assertTrue(MovimientoInventario.objects.filter(sale=venta, quantity=-2).exists())
        self.assertTrue(self.producto.alertas.filter(type='LOW_STOCK').exists())

    def test_no_permite_vender_mas_del_stock(self):
        venta = self.crear_venta(cantidad=6)
        with self.assertRaises(ValidationError):
            venta.completar()
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 5)

    def test_reembolso_restaura_stock(self):
        venta = self.crear_venta()
        venta.completar()
        item_venta = venta.items.get()
        reembolso = Reembolso.objects.create(sale=venta, user=self.admin, reason='Devolución')
        ItemReembolso.objects.create(refund=reembolso, sale_item=item_venta, product=self.producto, quantity=1, amount=Decimal('10.00'), restore_stock=True)
        reembolso.procesar()
        self.producto.refresh_from_db()
        venta.refresh_from_db()
        self.assertEqual(self.producto.stock, 4)
        self.assertEqual(venta.status, Venta.Estado.PARTIALLY_REFUNDED)
        self.assertTrue(MovimientoInventario.objects.filter(refund=reembolso, quantity=1).exists())

    def test_producto_con_venta_se_desactiva_al_eliminar(self):
        venta = self.crear_venta()
        venta.completar()
        self.producto.delete()
        self.producto.refresh_from_db()
        self.assertFalse(self.producto.disponible)

    def devolver(self, venta, cantidad=1, restore=True):
        reembolso = Reembolso.objects.create(sale=venta, user=self.admin, reason='Prueba de devolución')
        ItemReembolso.objects.create(refund=reembolso, sale_item=venta.items.get(), product=self.producto,
                                     quantity=cantidad, amount=Decimal('999.99'), restore_stock=restore)
        return reembolso

    def test_snapshots_no_cambian_y_save_no_edita_historico(self):
        venta = self.crear_venta()
        venta.completar()
        self.producto.refresh_from_db()
        self.producto.nombre, self.producto.precio, self.producto.cost_price = 'Otro', Decimal('99'), Decimal('1')
        self.producto.save()
        item = venta.items.get()
        self.assertEqual((item.product_name, item.unit_price, item.unit_cost), ('Producto de prueba', Decimal('10'), Decimal('6')))
        item.quantity = 99
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(ValidationError):
            venta.items.all().delete()
        with self.assertRaises(ValidationError):
            venta.items.update(quantity=99)

    def test_completar_dos_veces(self):
        venta = self.crear_venta()
        venta.completar()
        with self.assertRaises(ValidationError):
            venta.completar()
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 3)
        self.assertEqual(venta.movimientos.count(), 1)

    def test_cantidad_cero_negativa_y_mayor_stock(self):
        venta = Venta.objects.create(seller=self.user)
        for cantidad in (0, -1):
            with self.subTest(cantidad=cantidad), self.assertRaises(ValidationError):
                ItemVenta.objects.create(sale=venta, product=self.producto, quantity=cantidad)

    def test_borrador_revalida_stock_y_precio(self):
        venta = self.crear_venta(4)
        self.producto.stock = 3
        self.producto.save(user=self.admin)
        with self.assertRaises(ValidationError):
            venta.completar()
        self.producto.reabastecer(2, self.admin)
        self.producto.precio = Decimal('12')
        self.producto.save()
        venta.completar()
        self.assertEqual(venta.total, Decimal('48'))

    def test_cancelar_objeto_obsoleto_no_revierte_venta(self):
        venta = self.crear_venta()
        viejo = Venta.objects.get(pk=venta.pk)
        venta.completar()
        with self.assertRaises(ValidationError):
            viejo.cancelar()
        venta.refresh_from_db()
        self.assertEqual(venta.status, 'COMPLETED')
        venta.status = 'DRAFT'
        with self.assertRaises(ValidationError):
            venta.save()

    def test_cancelar_borrador_no_mueve_stock(self):
        venta = self.crear_venta()
        venta.cancelar()
        self.assertEqual(venta.status, 'CANCELLED')
        self.assertFalse(venta.movimientos.exists())
        with self.assertRaises(ValidationError):
            venta.completar()

    def test_rollback_si_segundo_producto_falla(self):
        venta = self.crear_venta()
        otro = Producto.objects.create(sku='SIN-STOCK', nombre='Sin stock', precio=Decimal('2'))
        ItemVenta.objects.create(sale=venta, product=otro, quantity=1)
        with self.assertRaises(ValidationError):
            venta.completar()
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 5)
        self.assertFalse(venta.movimientos.exists())
        self.assertFalse(self.producto.alertas.exists())

    def test_rollback_si_falla_alerta_al_final(self):
        venta = self.crear_venta()
        with patch.object(Producto, 'actualizar_alerta_stock', side_effect=RuntimeError('fallo')), self.assertRaises(RuntimeError):
            venta.completar()
        self.producto.refresh_from_db()
        venta.refresh_from_db()
        self.assertEqual((self.producto.stock, venta.status), (5, 'DRAFT'))
        self.assertFalse(venta.movimientos.exists())

    def test_cambio_y_pago_insuficiente(self):
        venta = self.crear_venta()
        venta.amount_received = Decimal('1')
        venta.save()
        with self.assertRaises(ValidationError):
            venta.completar()
        self.assertFalse(venta.movimientos.exists())
        venta.amount_received = Decimal('25')
        venta.save()
        venta.completar()
        self.assertEqual(venta.change_amount, Decimal('5'))

    def test_movimientos_signo_balance_e_inmutabilidad(self):
        venta = self.crear_venta()
        venta.completar()
        movimiento = venta.movimientos.get()
        self.assertEqual((movimiento.previous_stock, movimiento.quantity, movimiento.new_stock), (5, -2, 3))
        with self.assertRaises(ValidationError):
            movimiento.delete()
        movimiento.new_stock = 9
        with self.assertRaises(ValidationError):
            movimiento.full_clean()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            models.Model.save(movimiento)

    def test_alerta_unica_umbral_cero_y_reposicion(self):
        self.producto.stock = 0
        self.producto.min_stock = 0
        self.producto.save(user=self.admin)
        for _ in range(3):
            self.producto.actualizar_alerta_stock()
        self.assertEqual(self.producto.alertas.filter(status='UNREAD').count(), 1)
        self.producto.reabastecer(2, self.admin, 'Compra')
        alerta = self.producto.alertas.get()
        self.assertEqual(alerta.status, 'READ')
        self.assertIsNotNone(alerta.read_at)
        self.assertTrue(self.producto.movimientos.filter(movement_type='RESTOCK').exists())

    def test_edicion_stock_sin_actor_rechazada(self):
        self.producto.stock = 3
        with self.assertRaises(PermissionDenied):
            self.producto.save()
        with self.assertRaises(PermissionDenied):
            self.producto.save(user=self.user)

    def test_codigos_opcionales_y_producto_no_eliminable_masivamente(self):
        otro = Producto.objects.create(sku='OTRO', nombre='Otro', precio=Decimal('0'), barcode='')
        self.assertIsNone(otro.barcode)
        venta = self.crear_venta()
        venta.completar()
        Producto.objects.all().delete()
        self.assertEqual(Producto.objects.count(), 2)
        self.assertFalse(Producto.objects.filter(disponible=True).exists())
        with self.assertRaises(ValidationError):
            Producto.objects.create(sku=' ', nombre='Inválido', precio=Decimal('1'))
        with self.assertRaises(ValidationError):
            Producto.objects.create(sku='OTRO', nombre='Duplicado', precio=Decimal('1'))

    def test_reembolsos_acumulados_importe_servidor_y_doble_proceso(self):
        venta = self.crear_venta()
        venta.completar()
        primero = self.devolver(venta)
        segundo = self.devolver(venta)
        tercero = self.devolver(venta)
        primero.procesar()
        self.assertEqual(primero.amount, Decimal('10'))
        with self.assertRaises(ValidationError):
            primero.procesar()
        segundo.procesar()
        venta.refresh_from_db()
        self.assertEqual(venta.status, 'REFUNDED')
        with self.assertRaises(ValidationError):
            tercero.procesar()
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 5)

    def test_reembolso_sin_reposicion(self):
        venta = self.crear_venta()
        venta.completar()
        reembolso = self.devolver(venta, 2, False)
        reembolso.procesar()
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 3)
        self.assertFalse(reembolso.movimientos.exists())

    def test_reembolso_rechaza_borrador_y_detalle_ajeno(self):
        venta = self.crear_venta()
        with self.assertRaises(ValidationError):
            Reembolso.objects.create(sale=venta, user=self.admin, reason='Inválida')
        venta.completar()
        reembolso = Reembolso.objects.create(sale=venta, user=self.admin, reason='Otra')
        otra = Venta.objects.create(seller=self.user)
        ajeno = ItemVenta.objects.create(sale=otra, product=self.producto, quantity=1)
        with self.assertRaises(ValidationError):
            ItemReembolso.objects.create(refund=reembolso, sale_item=ajeno, product=self.producto, quantity=1)

    def test_reembolso_rechaza_cantidad_excesiva_y_rollback(self):
        venta = self.crear_venta()
        venta.completar()
        reembolso = self.devolver(venta, 3)
        with self.assertRaises(ValidationError):
            reembolso.procesar()
        self.assertFalse(reembolso.movimientos.exists())
        reembolso.refresh_from_db()
        self.assertEqual(reembolso.status, 'PENDING')

    def test_reembolso_redondeo_con_impuestos_y_descuentos(self):
        venta = self.crear_venta(3)
        venta.tax = Decimal('.05')
        venta.save()
        item = venta.items.get()
        item.discount = Decimal('.01')
        item.save()
        venta.completar()
        importes = []
        for _ in range(3):
            reembolso = self.devolver(venta)
            reembolso.procesar()
            importes.append(reembolso.amount)
        self.assertEqual(sum(importes), venta.total)
        self.assertEqual(venta.total, Decimal('30.04'))

    def test_venta_gratis_reembolso_estado_por_cantidad(self):
        self.producto.precio = Decimal('0')
        self.producto.save()
        venta = self.crear_venta(2)
        venta.completar()
        self.devolver(venta).procesar()
        venta.refresh_from_db()
        self.assertEqual(venta.status, 'PARTIALLY_REFUNDED')

    def test_reembolso_rollback_si_falla_despues_de_reponer(self):
        venta = self.crear_venta()
        venta.completar()
        reembolso = self.devolver(venta)
        with patch.object(Producto, 'actualizar_alerta_stock', side_effect=RuntimeError('fallo')), self.assertRaises(RuntimeError):
            reembolso.procesar()
        self.producto.refresh_from_db()
        reembolso.refresh_from_db()
        venta.refresh_from_db()
        self.assertEqual((self.producto.stock, reembolso.status, venta.status), (3, 'PENDING', 'COMPLETED'))
        self.assertEqual(reembolso.items.get().amount, Decimal('0'))
        self.assertFalse(reembolso.movimientos.exists())


class AccesoYReportesTests(TestCase):
    setUp = FlujoVentaTests.setUp
    crear_venta = FlujoVentaTests.crear_venta
    devolver = FlujoVentaTests.devolver

    def test_anonimo_y_vendedor_no_pueden_inventario_ni_reportes(self):
        for url in ('/api/inventario/', '/api/inventario/nuevo/', f'/api/inventario/{self.producto.pk}/editar/', '/api/dashboard-ui/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.get('/api/analytics/metrics/').status_code, 403)
        self.client.force_login(self.user)
        for url in ('/api/inventario/', '/api/inventario/nuevo/', '/api/dashboard-ui/', '/api/analytics/metrics/'):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(f'/api/inventario/{self.producto.pk}/editar/', {'stock': 100}).status_code, 403)

    def test_administrador_y_superusuario_legacy(self):
        self.client.force_login(self.admin)
        for url in ('/api/inventario/', '/api/inventario/nuevo/', '/api/dashboard-ui/', '/api/analytics/metrics/', '/api/ventas-ui/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        PerfilUsuario.objects.create(user=self.user, role='ADMIN')
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/analytics/metrics/').status_code, 200)

    def test_perfil_inactivo_denegado(self):
        PerfilUsuario.objects.create(user=self.user, role='ADMIN', active=False)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/analytics/metrics/').status_code, 403)
        self.assertEqual(self.client.get('/api/ventas/').status_code, 403)

    def test_vendedor_staff_no_accede_admin(self):
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/admin/myapp/producto/').status_code, 302)

    def test_csrf_formulario_y_api(self):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/api/inventario/nuevo/', {}).status_code, 403)
        self.assertEqual(client.post('/api/ventas/', {}, format='json').status_code, 403)

    def test_api_backend_descarta_precios_y_aisla_vendedores(self):
        api = APIClient()
        api.force_login(self.user)
        response = api.post('/api/ventas/', {'items': [{'product': self.producto.pk, 'quantity': 2, 'unit_price': '0.01'}], 'total': '0.02', 'seller': self.admin.pk}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        venta_id = response.data['id']
        self.assertEqual(response.data['seller'], self.user.pk)
        self.assertNotIn('unit_cost', response.data['items'][0])
        response = api.post(f'/api/ventas/{venta_id}/completar/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total'], '20.00')
        ajeno = get_user_model().objects.create_user(username='ajeno')
        api.force_login(ajeno)
        self.assertEqual(api.get(f'/api/ventas/{venta_id}/').status_code, 404)
        self.assertEqual(api.post(f'/api/ventas/{venta_id}/cancelar/').status_code, 404)

    def test_api_ids_inexistentes_producto_inactivo_y_descuento_no_autorizado(self):
        api = APIClient()
        api.force_login(self.user)
        for producto_id, cantidad, descuento in ((99999, 1, '0'), (self.producto.pk, 0, '0'), (self.producto.pk, 1, '1')):
            response = api.post('/api/ventas/', {'items': [{'product': producto_id, 'quantity': cantidad, 'discount': descuento}]}, format='json')
            self.assertEqual(response.status_code, 400)
        self.producto.delete()
        response = api.post('/api/ventas/', {'items': [{'product': self.producto.pk, 'quantity': 1}]}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Venta.objects.exists())

    def test_reportes_parcial_bruto_neto_costo_y_borradores(self):
        venta = self.crear_venta()
        venta.completar()
        self.devolver(venta).procesar()
        Venta.objects.create(seller=self.user)
        cancelada = Venta.objects.create(seller=self.user)
        cancelada.cancelar()
        self.client.force_login(self.admin)
        data = self.client.get('/api/analytics/metrics/').json()
        resumen = data['diario']['resumen']
        self.assertEqual((resumen['transacciones'], resumen['unidades_vendidas']), (1, 2))
        self.assertEqual((resumen['ventas_brutas'], resumen['reembolsos'], resumen['ingresos_netos']), ('20.00', '10.00', '10.00'))
        self.assertEqual((resumen['costo_conocido'], resumen['utilidad_bruta']), ('6.00', '4.00'))
        self.assertEqual(len(data['charts']['line_chart_semana']), 7)

    def test_reembolso_hoy_de_venta_anterior_y_costo_desconocido(self):
        self.producto.cost_price = None
        self.producto.save()
        venta = self.crear_venta()
        venta.completar()
        venta.completed_at = timezone.now() - timedelta(days=40)
        models.Model.save(venta, update_fields=['completed_at'])  # Fixture histórica, evita la API protegida deliberadamente.
        self.devolver(venta).procesar()
        self.client.force_login(self.admin)
        resumen = self.client.get('/api/analytics/metrics/').json()['diario']['resumen']
        self.assertEqual(resumen['transacciones'], 0)
        self.assertEqual(resumen['ingresos_netos'], '-10.00')
        self.assertIsNone(resumen['utilidad_bruta'])

    def test_inventario_edicion_obsoleta_y_movimientos(self):
        self.client.force_login(self.admin)
        data = {'sku': self.producto.sku, 'nombre': self.producto.nombre, 'precio': '10.00', 'cost_price': '0.00', 'stock': 2, 'min_stock': 3, 'stock_original': 5, 'disponible': 'on'}
        self.producto.reabastecer(1, self.admin)
        response = self.client.post(f'/api/inventario/{self.producto.pk}/editar/', data)
        self.assertContains(response, 'El stock cambió')
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 6)
        data['stock_original'] = 6
        self.assertEqual(self.client.post(f'/api/inventario/{self.producto.pk}/editar/', data).status_code, 302)
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 2)
        self.assertTrue(self.producto.movimientos.filter(movement_type='ADJUSTMENT_OUT', quantity=-4).exists())

    def test_admin_formularios_historico_y_accion_invalida(self):
        self.client.force_login(self.admin)
        venta = self.crear_venta()
        for url in ('/admin/myapp/producto/add/', f'/admin/myapp/producto/{self.producto.pk}/change/', f'/admin/myapp/venta/{venta.pk}/change/', '/admin/myapp/reembolso/add/'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
        venta.completar()
        response = self.client.post('/admin/myapp/venta/', {'action': 'completar_ventas', '_selected_action': str(venta.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(f'/admin/myapp/venta/{venta.pk}/change/', {'status': 'DRAFT'}).status_code, 403)

    def test_logout_real(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/api/logout/').status_code, 405)
        self.assertEqual(self.client.post('/api/logout/').status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_api_recuperar_editar_confirmar_y_reembolsar(self):
        api = APIClient()
        api.force_login(self.user)
        datos = {'items': [{'product': self.producto.pk, 'quantity': 1}]}
        response = api.post('/api/ventas/', datos, format='json')
        pk = response.data['id']
        self.assertEqual(api.get(f'/api/ventas/{pk}/').data['status'], 'DRAFT')
        datos['items'][0]['quantity'] = 2
        self.assertEqual(api.put(f'/api/ventas/{pk}/', datos, format='json').status_code, 200)
        response = api.post(f'/api/ventas/{pk}/completar/', {}, format='json')
        self.assertEqual(response.data['total'], '20.00')
        item_id = response.data['items'][0]['id']
        devolucion = {'reason': 'Producto devuelto', 'items': [{'sale_item': item_id, 'quantity': 1, 'restore_stock': True, 'amount': '9999'}]}
        self.assertEqual(api.post(f'/api/ventas/{pk}/reembolsar/', devolucion, format='json').status_code, 403)
        api.force_login(self.admin)
        response = api.post(f'/api/ventas/{pk}/reembolsar/', devolucion, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['amount'], '10.00')

    def test_importacion_productos_audita_stock_y_dry_run_no_persiste(self):
        from tablib import Dataset
        from .admin import ProductoResource
        datos = Dataset(headers=['sku', 'nombre', 'precio', 'stock', 'min_stock', 'disponible'])
        datos.append(['IMPORTADO', 'Producto importado', '5.00', 3, 1, True])
        recurso = ProductoResource(user=self.admin)
        ensayo = recurso.import_data(datos, dry_run=True, raise_errors=True)
        self.assertFalse(ensayo.has_errors())
        self.assertFalse(Producto.objects.filter(sku='IMPORTADO').exists())
        recurso.import_data(datos, dry_run=False, raise_errors=True)
        producto = Producto.objects.get(sku='IMPORTADO')
        self.assertEqual(producto.movimientos.get().quantity, 3)
        self.assertEqual(producto.movimientos.get().user_id, self.admin.pk)

    def test_creacion_producto_ajax_con_movimiento_inicial(self):
        self.client.force_login(self.admin)
        data = {'sku': 'NUEVO', 'nombre': 'Producto AJAX', 'precio': '9.99', 'cost_price': '0', 'stock': 4, 'min_stock': 1, 'stock_original': 0, 'disponible': 'on'}
        response = self.client.post('/api/inventario/nuevo/', data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        producto = Producto.objects.get(pk=response.json()['id'])
        self.assertEqual(producto.movimientos.get().movement_type, 'INITIAL')

    def test_venta_con_costo_desconocido_no_inventa_utilidad(self):
        self.producto.cost_price = None
        self.producto.save()
        venta = self.crear_venta()
        venta.completar()
        self.client.force_login(self.admin)
        data = self.client.get('/api/analytics/metrics/').json()
        self.assertIsNone(data['diario']['resumen']['utilidad_bruta'])
        self.assertEqual(data['diario']['resumen']['unidades_sin_costo'], 2)

    def test_detalle_reembolso_inmutable_y_producto_incorrecto(self):
        venta = self.crear_venta()
        venta.completar()
        reembolso = self.devolver(venta)
        otro = Producto.objects.create(sku='EQUIVOCADO', nombre='Otro', precio='2')
        item = reembolso.items.get()
        item.product = otro
        with self.assertRaises(ValidationError):
            item.save()
        reembolso.procesar()
        item = reembolso.items.get()
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(ValidationError):
            reembolso.items.all().delete()

    def test_cascada_no_pierde_productos_ni_historial(self):
        categoria = Categoria.objects.create(name='Categoría')
        self.producto.categoria = categoria
        self.producto.save()
        venta = self.crear_venta()
        venta.completar()
        categoria.delete()
        self.producto.refresh_from_db()
        self.assertIsNone(self.producto.categoria_id)
        from django.db.models.deletion import ProtectedError
        with self.assertRaises(ProtectedError):
            self.user.delete()
        with self.assertRaises(ValidationError):
            venta.delete()
