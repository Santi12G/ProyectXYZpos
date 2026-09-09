"""Se omiten explícitamente en SQLite: no implementa select_for_update."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature

from .models import ItemReembolso, ItemVenta, Producto, Reembolso, Venta


class ConcurrenciaTests(TransactionTestCase):
    def preparar(self):
        self.user = get_user_model().objects.create_superuser(username='concurrente', password='clave-segura')
        self.producto = Producto(sku='UNICO', nombre='Última unidad', precio='10.00', stock=1)
        self.producto.save(user=self.user)

    def venta(self):
        venta = Venta.objects.create(seller=self.user)
        ItemVenta.objects.create(sale=venta, product=self.producto, quantity=1)
        return venta

    def competir(self, operaciones):
        barrera = Barrier(2, timeout=10)

        def ejecutar(operacion):
            close_old_connections()
            try:
                barrera.wait()
                with transaction.atomic():
                    if connection.vendor == 'postgresql':
                        with connection.cursor() as cursor:
                            cursor.execute("SET LOCAL lock_timeout = '5s'")
                    operacion()
                return 'OK'
            except ValidationError:
                return 'RECHAZADA'
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(ejecutar, operaciones, timeout=20))
        self.assertCountEqual(resultados, ['OK', 'RECHAZADA'])

    @skipUnlessDBFeature('has_select_for_update')
    def test_dos_ventas_no_venden_la_misma_unidad(self):
        self.preparar()
        a, b = self.venta(), self.venta()
        self.competir([lambda: Venta.objects.get(pk=a.pk).completar(), lambda: Venta.objects.get(pk=b.pk).completar()])
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 0)
        self.assertEqual(Venta.objects.filter(status='COMPLETED').count(), 1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_dos_reembolsos_no_devuelven_dos_veces_la_unidad(self):
        self.preparar()
        venta = self.venta()
        venta.completar()
        pendientes = []
        for _ in range(2):
            reembolso = Reembolso.objects.create(sale=venta, user=self.user, reason='Devolución')
            ItemReembolso.objects.create(refund=reembolso, sale_item=venta.items.get(), product=self.producto, quantity=1)
            pendientes.append(reembolso.pk)
        self.competir([lambda: Reembolso.objects.get(pk=pendientes[0]).procesar(), lambda: Reembolso.objects.get(pk=pendientes[1]).procesar()])
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 1)
        self.assertEqual(Reembolso.objects.filter(status='COMPLETED').count(), 1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_cancelacion_y_confirmacion_comparten_bloqueo(self):
        self.preparar()
        venta = self.venta()
        self.competir([lambda: Venta.objects.get(pk=venta.pk).completar(), lambda: Venta.objects.get(pk=venta.pk).cancelar()])
        venta.refresh_from_db()
        self.producto.refresh_from_db()
        self.assertEqual(self.producto.stock, 0 if venta.status == 'COMPLETED' else 1)
