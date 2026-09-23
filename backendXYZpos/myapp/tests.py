from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, models, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import ItemVenta, MovimientoInventario, PerfilUsuario, Producto, Venta


class POSFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('admin', password='test-password')
        cls.seller = get_user_model().objects.create_user('seller', password='test-password')
        cls.other = get_user_model().objects.create_user('other', password='test-password')
        cls.product = Producto(sku='SKU-001', nombre='Producto', precio=Decimal('10.00'),
                              cost_price=Decimal('6.00'), stock=5)
        cls.product.save(user=cls.admin)


class InventarioTests(POSFixtures):
    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('producto_crear')
        self.data = {'sku': ' NUEVO-001 ', 'nombre': 'Nuevo', 'precio': '12.50',
                     'stock': '3', 'stock_original': '0', 'disponible': 'on'}

    def test_crear_desde_inventario_con_ajax(self):
        page = self.client.get(reverse('inventario'))
        self.assertContains(page, self.url)
        self.assertContains(self.client.get(self.url), 'name="sku"')
        response = self.client.post(self.url, self.data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        product = Producto.objects.get(pk=response.json()['id'])
        self.assertEqual((product.sku, product.stock), ('NUEVO-001', 3))
        self.assertIsNotNone(product.fecha_creacion)
        self.assertIsNotNone(product.updated_at)
        self.assertEqual((product.description, product.image_url, product.min_stock), ('', '', 0))
        self.assertEqual(product.movimientos.get().quantity, 3)

    def test_crear_sin_javascript(self):
        self.assertRedirects(self.client.post(self.url, self.data), reverse('inventario'))

    def test_sku_existente_con_espacios_es_rechazado(self):
        self.data['sku'] = ' SKU-001 '
        response = self.client.post(self.url, self.data)
        self.assertFormError(response.context['form'], 'sku', 'Ya existe un producto con ese SKU.')
        self.assertEqual(Producto.objects.count(), 1)

    def test_sku_vacio_es_rechazado(self):
        self.data['sku'] = '   '
        response = self.client.post(self.url, self.data)
        self.assertIn('sku', response.context['form'].errors)
        self.assertEqual(Producto.objects.count(), 1)

    def test_post_vacio_muestra_validaciones(self):
        response = self.client.post(self.url, {})
        self.assertIn('sku', response.context['form'].errors)

    def test_unicidad_tambien_existe_en_base_de_datos(self):
        duplicate = Producto(sku=self.product.sku, nombre='Duplicado', precio=Decimal('1.00'))
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.Model.save(duplicate)

    def test_editar_mismo_sku_conserva_producto_y_campos_no_expuestos(self):
        original_creation = self.product.fecha_creacion
        self.data.update(sku=self.product.sku, stock='5', stock_original='5')
        response = self.client.post(reverse('producto_editar', args=[self.product.pk]), self.data)
        self.assertRedirects(response, reverse('inventario'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.nombre, 'Nuevo')
        self.assertEqual(self.product.cost_price, Decimal('6.00'))
        self.assertEqual(self.product.fecha_creacion, original_creation)
        self.assertEqual(Producto.objects.count(), 1)

    def test_no_sobrescribe_stock_si_cambio_durante_edicion(self):
        self.data.update(sku=self.product.sku, stock='10', stock_original='4')
        response = self.client.post(reverse('producto_editar', args=[self.product.pk]), self.data)
        self.assertContains(response, 'El stock cambió mientras editabas.')
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_valores_negativos_son_rechazados(self):
        for field in ('precio', 'stock'):
            with self.subTest(field=field):
                response = self.client.post(self.url, {**self.data, field: '-1'})
                self.assertTrue(response.context['form'].errors)
        self.assertEqual(Producto.objects.count(), 1)

    def test_error_de_integridad_ajeno_a_sku_no_se_oculta(self):
        with patch('myapp.forms.ProductoForm.save', side_effect=IntegrityError('NOT NULL')):
            with self.assertRaises(IntegrityError):
                self.client.post(self.url, self.data)

    def test_colision_postgresql_de_sku_entre_validar_y_guardar(self):
        cause = Exception('duplicate key')
        cause.pgcode = '23505'
        cause.diag = SimpleNamespace(constraint_name='myapp_producto_sku_key')
        error = IntegrityError('duplicate key')
        error.__cause__ = cause
        with patch('myapp.forms.ProductoForm.save', side_effect=error):
            response = self.client.post(self.url, self.data)
        self.assertFormError(response.context['form'], 'sku', 'Ya existe un producto con ese SKU.')


class VentasTests(POSFixtures):
    def setUp(self):
        # Uses the rendered CSRF token and a real session, like the UI's fetch calls.
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.seller)
        self.page = self.client.get(reverse('ventas_ui'))
        self.client.credentials(HTTP_X_CSRFTOKEN=self.client.cookies['csrftoken'].value)
        self.url = reverse('ventas-list')

    def create_draft(self, items=None, **extra):
        data = {'items': items or [{'product': self.product.pk, 'quantity': 2}],
                'payment_method': 'CASH', 'amount_received': '30.00', **extra}
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.json()

    def complete(self, sale_id):
        return self.client.post(reverse('ventas-completar', args=[sale_id]), {}, format='json')

    def assert_unchanged(self, sale_id, stock=5):
        self.product.refresh_from_db()
        sale = Venta.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'DRAFT')
        self.assertIsNone(sale.completed_at)
        self.assertEqual(self.product.stock, stock)
        self.assertFalse(sale.movimientos.exists())

    def test_flujo_ui_guardar_recuperar_editar_confirmar(self):
        self.assertContains(self.page, 'id="sale-form"')
        self.assertContains(self.page, f'value="{self.product.pk}"')
        draft = self.create_draft()
        self.assertEqual(draft['status'], 'DRAFT')
        self.assert_unchanged(draft['id'])
        detail_url = reverse('ventas-detail', args=[draft['id']])
        detail = self.client.get(detail_url).json()
        self.assertEqual(detail['items'][0]['product'], self.product.pk)
        self.assertEqual(detail['items'][0]['quantity'], 2)
        response = self.client.put(detail_url, {'items': [{'product': self.product.pk, 'quantity': 3}],
                                   'payment_method': 'CASH', 'amount_received': '40.00'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()['items'][0]['quantity'], 3)
        self.assert_unchanged(draft['id'])
        response = self.complete(draft['id'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.json()['status'], response.json()['total'], response.json()['change_amount']),
                         ('COMPLETED', '30.00', '10.00'))
        sale = Venta.objects.get(pk=draft['id'])
        self.assertIsNotNone(sale.completed_at)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 2)
        movement = sale.movimientos.get()
        self.assertEqual((movement.quantity, movement.previous_stock, movement.new_stock), (-3, 5, 2))
        self.assertEqual(movement.user, self.seller)
        self.assertEqual(self.client.get(self.url).json()[0]['status'], 'COMPLETED')

    def test_dos_borradores_tienen_comprobantes_distintos(self):
        first, second = self.create_draft(), self.create_draft()
        self.assertTrue(first['receipt_number'])
        self.assertNotEqual(first['receipt_number'], second['receipt_number'])
        self.assertEqual(self.client.get(self.url).json()[0]['id'], second['id'])

    def test_confirmar_usa_precio_actual_y_no_importes_del_cliente(self):
        draft = self.create_draft(total='0.01', subtotal='0.01', status='COMPLETED',
                                  items=[{'product': self.product.pk, 'quantity': 2, 'unit_price': '0.01'}])
        self.product.precio = Decimal('12.00')
        self.product.save(user=self.admin)
        response = self.complete(draft['id'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()['total'], '24.00')
        self.assertEqual(response.json()['items'][0]['unit_price'], '12.00')

    def test_stock_disminuye_entre_borrador_y_confirmacion(self):
        draft = self.create_draft()
        self.product.stock = 1
        self.product.save(user=self.admin)
        response = self.complete(draft['id'])
        self.assertEqual(response.status_code, 400)
        self.assertIn('Stock insuficiente', str(response.data))
        self.assert_unchanged(draft['id'], stock=1)

    def test_producto_desactivado_despues_del_borrador(self):
        draft = self.create_draft()
        self.product.disponible = False
        self.product.save(user=self.admin)
        self.assertEqual(self.complete(draft['id']).status_code, 400)
        self.assert_unchanged(draft['id'])

    def test_pago_insuficiente_revierte_stock_items_y_alertas(self):
        draft = self.create_draft(amount_received='1.00')
        self.product.precio = Decimal('12.00')
        self.product.min_stock = 3
        self.product.save(user=self.admin)
        response = self.complete(draft['id'])
        self.assertEqual(response.status_code, 400)
        self.assert_unchanged(draft['id'])
        self.assertEqual(ItemVenta.objects.get(sale_id=draft['id']).unit_price, Decimal('10.00'))
        self.assertFalse(self.product.alertas.exists())

    def test_fallo_segunda_linea_revierte_primera(self):
        other = Producto.objects.create(sku='EMPTY', nombre='Sin stock', precio=Decimal('1.00'))
        draft = self.create_draft(items=[{'product': self.product.pk, 'quantity': 2},
                                        {'product': other.pk, 'quantity': 1}])
        self.assertEqual(self.complete(draft['id']).status_code, 400)
        self.assert_unchanged(draft['id'])

    def test_confirmacion_duplicada_no_descuenta_dos_veces(self):
        draft = self.create_draft()
        self.assertEqual(self.complete(draft['id']).status_code, 200)
        self.assertEqual(self.complete(draft['id']).status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(MovimientoInventario.objects.filter(sale_id=draft['id']).count(), 1)

    def test_cancelar_no_descuenta_stock_y_no_permite_confirmar(self):
        draft = self.create_draft()
        response = self.client.post(reverse('ventas-cancelar', args=[draft['id']]), {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'CANCELLED')
        self.assertEqual(self.complete(draft['id']).status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertFalse(MovimientoInventario.objects.filter(sale_id=draft['id']).exists())

    def test_venta_vacia_no_se_confirma(self):
        sale = Venta.objects.create(seller=self.seller)
        self.assertEqual(self.complete(sale.pk).status_code, 400)
        self.assert_unchanged(sale.pk)

    def test_entradas_invalidas_no_crean_venta(self):
        invalid = [[], [{'product': self.product.pk, 'quantity': 0}],
                   [{'product': self.product.pk, 'quantity': -1}],
                   [{'product': self.product.pk, 'quantity': 1.5}],
                   [{'product': self.product.pk, 'quantity': 1}] * 2,
                   [{'product': 999999, 'quantity': 1}]]
        for items in invalid:
            with self.subTest(items=items):
                response = self.client.post(self.url, {'items': items}, format='json')
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Venta.objects.exists())

    def test_edicion_invalida_conserva_borrador(self):
        draft = self.create_draft()
        other = Producto.objects.create(sku='OVERFLOW', nombre='Caro', precio=Decimal('9999999999.99'))
        response = self.client.put(reverse('ventas-detail', args=[draft['id']]),
                                   {'items': [{'product': other.pk, 'quantity': 2}]}, format='json')
        self.assertEqual(response.status_code, 400)
        item = ItemVenta.objects.get(sale_id=draft['id'])
        self.assertEqual((item.product_id, item.quantity), (self.product.pk, 2))
        self.assert_unchanged(draft['id'])

    def test_venta_completada_no_se_edita(self):
        draft = self.create_draft()
        self.complete(draft['id'])
        response = self.client.put(reverse('ventas-detail', args=[draft['id']]),
                                   {'items': [{'product': self.product.pk, 'quantity': 4}]}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ItemVenta.objects.get(sale_id=draft['id']).quantity, 2)

    def test_otro_vendedor_no_puede_operar_venta_ajena(self):
        draft = self.create_draft()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('ventas-detail', args=[draft['id']])).status_code, 404)
        self.assertEqual(self.complete(draft['id']).status_code, 404)
        self.assertEqual(self.client.get(self.url).json(), [])
        self.assert_unchanged(draft['id'])

    def test_perfil_inactivo_no_puede_confirmar(self):
        draft = self.create_draft()
        PerfilUsuario.objects.create(user=self.seller, active=False)
        self.assertEqual(self.complete(draft['id']).status_code, 403)
        self.assert_unchanged(draft['id'])

    def test_csrf_es_obligatorio(self):
        draft = self.create_draft()
        self.client.credentials()
        self.assertEqual(self.complete(draft['id']).status_code, 403)
        self.assert_unchanged(draft['id'])

    def test_vendedor_no_puede_aplicar_descuentos_o_impuestos(self):
        for data in ({'tax': '1.00', 'items': [{'product': self.product.pk, 'quantity': 1}]},
                     {'items': [{'product': self.product.pk, 'quantity': 1, 'discount': '1.00'}]}):
            response = self.client.post(self.url, data, format='json')
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Venta.objects.exists())

    def test_confirmar_revalida_descuento_con_precio_actual(self):
        self.client.force_login(self.admin)
        draft = self.create_draft(items=[{'product': self.product.pk, 'quantity': 1, 'discount': '8.00'}])
        self.product.precio = Decimal('5.00')
        self.product.save(user=self.admin)
        self.assertEqual(self.complete(draft['id']).status_code, 400)
        self.assert_unchanged(draft['id'])

    def test_tarjeta_no_requiere_monto_recibido(self):
        draft = self.create_draft(payment_method='CARD', amount_received=None)
        response = self.complete(draft['id'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.json()['change_amount'])
