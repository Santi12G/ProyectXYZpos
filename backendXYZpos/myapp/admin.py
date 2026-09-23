from django.contrib import admin
from .models import Categoria, Producto, Venta, Reembolso, ItemVenta, MovimientoInventario


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    # Solo usamos campos reales de Producto
    list_display = ('sku', 'nombre', 'categoria', 'precio', 'stock', 'disponible')
    search_fields = ('sku', 'nombre')


@admin.register(Venta)
class VentaAdmin(admin.ModelAdmin):
    # Solo usamos campos reales de Venta
    list_display = ('receipt_number', 'seller', 'status', 'total', 'created_at')
    search_fields = ('receipt_number',)


# Registramos los demás modelos de forma sencilla
admin.site.register(Categoria)
admin.site.register(Reembolso)
admin.site.register(ItemVenta)
admin.site.register(MovimientoInventario)