from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class MigracionesInventarioTests(TransactionTestCase):
    ultima = ('myapp', '0007_unicidad_movimientos')

    def migrar(self, nombre):
        executor = MigrationExecutor(connection)
        destino = ('myapp', nombre)
        executor.migrate([destino])
        return executor.loader.project_state([destino]).apps

    def tearDown(self):
        self.migrar(self.ultima[1])
        super().tearDown()

    def test_desde_esquema_previo_preserva_datos_y_marca_legacy(self):
        apps = self.migrar('0003_venta')
        producto = apps.get_model('myapp', 'Producto').objects.create(nombre='Antiguo', precio='12.50', stock=7)
        venta = apps.get_model('myapp', 'Venta').objects.create(total='25.00')
        apps = self.migrar(self.ultima[1])
        producto_nuevo = apps.get_model('myapp', 'Producto').objects.get(pk=producto.pk)
        venta_nueva = apps.get_model('myapp', 'Venta').objects.get(pk=venta.pk)
        self.assertEqual(producto_nuevo.stock, 7)
        self.assertTrue(producto_nuevo.sku.startswith('LEGACY-'))
        self.assertEqual(str(venta_nueva.total), '25.00')
        self.assertTrue(venta_nueva.legacy)
        self.assertEqual(venta_nueva.status, 'COMPLETED')
        self.assertEqual(venta_nueva.completed_at, venta_nueva.created_at)
        self.assertFalse(venta_nueva.seller.is_active)
        self.assertEqual(venta_nueva.seller.password, '!')
        movimiento = apps.get_model('myapp', 'MovimientoInventario').objects.get(product_id=producto.pk)
        self.assertEqual((movimiento.previous_stock, movimiento.quantity, movimiento.new_stock), (0, 7, 7))

    def test_datos_parciales_colisiones_cuenta_humana_e_idempotencia(self):
        apps = self.migrar('0004_alerta_categoria_itemreembolso_itemventa_and_more')
        Producto = apps.get_model('myapp', 'Producto')
        Venta = apps.get_model('myapp', 'Venta')
        User = apps.get_model('auth', 'User')
        humano = User.objects.create(username='migration-system', is_active=True)
        faltante = Producto.objects.create(nombre='Faltante', precio='1', stock=0)
        ocupado = Producto.objects.create(nombre='Código ocupado', precio='1', stock=0, sku=f'LEGACY-{faltante.pk:06d}')
        original = Venta.objects.create(total='10')
        Venta.objects.create(total='20', seller=humano, receipt_number=f'LEGACY-{original.pk:08d}')
        sin_vendedor = Venta.objects.create(total='30', receipt_number='YA-NUMERADA')
        apps = self.migrar(self.ultima[1])
        Producto = apps.get_model('myapp', 'Producto')
        Venta = apps.get_model('myapp', 'Venta')
        self.assertNotEqual(Producto.objects.get(pk=faltante.pk).sku, ocupado.sku)
        self.assertNotEqual(Venta.objects.get(pk=original.pk).receipt_number, f'LEGACY-{original.pk:08d}')
        tecnica = Venta.objects.get(pk=sin_vendedor.pk).seller
        self.assertFalse(tecnica.is_active)
        self.assertNotEqual(tecnica.pk, humano.pk)
        self.assertTrue(apps.get_model('auth', 'User').objects.get(pk=humano.pk).is_active)
        # La transformación de datos puede repetirse sin recrear usuarios ni IDs.
        modulo = import_module('myapp.migrations.0005_identificadores_requeridos')
        antes = list(Venta.objects.values_list('pk', 'receipt_number', 'seller_id'))
        with connection.schema_editor() as schema_editor:
            modulo.completar_datos_existentes(apps, schema_editor)
        self.assertEqual(antes, list(Venta.objects.values_list('pk', 'receipt_number', 'seller_id')))

    def test_datos_negativos_previos_fallan_sin_corregir_silenciosamente(self):
        apps = self.migrar('0003_venta')
        Producto = apps.get_model('myapp', 'Producto')
        producto = Producto.objects.create(nombre='Negativo', precio='1', stock=-2)
        with self.assertRaisesMessage(RuntimeError, 'Conciliar antes de migrar'):
            self.migrar(self.ultima[1])
        self.assertEqual(Producto.objects.get(pk=producto.pk).stock, -2)
        Producto.objects.filter(pk=producto.pk).update(stock=0)

    def test_reembolsos_previos_requieren_conciliar_datos_no_verificables(self):
        apps = self.migrar('0005_identificadores_requeridos')
        user = apps.get_model('auth', 'User').objects.create(username='tecnico')
        venta = apps.get_model('myapp', 'Venta').objects.create(seller=user, receipt_number='EXISTENTE', total='5')
        Reembolso = apps.get_model('myapp', 'Reembolso')
        reembolso = Reembolso.objects.create(sale=venta, user=user, reason='Histórico', status='COMPLETED', amount='5')
        with self.assertRaisesMessage(RuntimeError, 'conciliar importes'):
            self.migrar(self.ultima[1])
        self.assertEqual(Reembolso.objects.get(pk=reembolso.pk).amount, 5)
        # Corrige únicamente la fixture para permitir la restauración del esquema.
        Reembolso.objects.filter(pk=reembolso.pk).update(status='PENDING')

    def test_alertas_previas_duplicadas_se_conservan_cerradas(self):
        apps = self.migrar('0005_identificadores_requeridos')
        producto = apps.get_model('myapp', 'Producto').objects.create(sku='ALERTA', nombre='Prueba', precio='1', stock=0)
        Alerta = apps.get_model('myapp', 'Alerta')
        for _ in range(2):
            Alerta.objects.create(product=producto, message='Stock bajo')
        apps = self.migrar(self.ultima[1])
        Alerta = apps.get_model('myapp', 'Alerta')
        self.assertEqual(Alerta.objects.filter(product_id=producto.pk).count(), 2)
        self.assertEqual(Alerta.objects.filter(product_id=producto.pk, status='UNREAD').count(), 1)

    def test_0006_repara_filas_generadas_por_0005_original(self):
        apps = self.migrar('0005_identificadores_requeridos')
        user = apps.get_model('auth', 'User').objects.create(username='migration-system', is_active=False)
        venta = apps.get_model('myapp', 'Venta').objects.create(seller=user, receipt_number='LEGACY-00000099', total='13.00', status='DRAFT')
        apps = self.migrar(self.ultima[1])
        actual = apps.get_model('myapp', 'Venta').objects.get(pk=venta.pk)
        self.assertTrue(actual.legacy)
        self.assertEqual(actual.status, 'COMPLETED')
        self.assertEqual(actual.subtotal, actual.total)
