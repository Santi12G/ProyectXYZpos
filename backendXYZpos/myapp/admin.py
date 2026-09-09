from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from .models import Producto, Tarea
from .models import Venta


@admin.register(Venta)
class VentaAdmin(admin.ModelAdmin):
  list_display = (
      'id',
      'total',
      'created_at',
  )  # Campos que se verán en la tabla de la lista
  search_fields = ('id',)  # Campos por los que podrás buscar

@admin.register(Producto)
class ProductoAdmin(ImportExportModelAdmin):
    list_display = ('nombre', 'precio', 'stock')

@admin.register(Tarea)
class TareaAdmin(ImportExportModelAdmin):
    list_display = ('titulo', 'completada')