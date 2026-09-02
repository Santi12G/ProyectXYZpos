from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from .models import Producto, Tarea

@admin.register(Producto)
class ProductoAdmin(ImportExportModelAdmin):
    list_display = ('nombre', 'precio', 'stock')

@admin.register(Tarea)
class TareaAdmin(ImportExportModelAdmin):
    list_display = ('titulo', 'completada')