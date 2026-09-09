from types import MethodType
from django.contrib import admin, messages
from django.core.exceptions import ValidationError, PermissionDenied
from django.shortcuts import redirect
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .forms import ProductoForm
from .permissions import es_admin
from .models import (Alerta, Categoria, ItemReembolso, ItemVenta, MovimientoInventario,
                     PerfilUsuario, Producto, Reembolso, Tarea, Venta)


def acceso_admin(site, request):
    return request.user.is_staff and es_admin(request.user)


admin.site.has_permission = MethodType(acceso_admin, admin.site)


class PermisosPOS:
    def has_module_permission(self, request):
        return request.user.is_staff and es_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


class SoloLectura(PermisosPOS, admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ItemVentaInline(admin.TabularInline):
    model = ItemVenta
    extra = 1
    readonly_fields = ('product_name', 'unit_price', 'unit_cost', 'subtotal')

    def has_add_permission(self, request, obj=None):
        return es_admin(request.user) and (obj is None or obj.status == 'DRAFT')

    def has_change_permission(self, request, obj=None):
        return self.has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self.has_add_permission(request, obj)


class ItemReembolsoInline(admin.TabularInline):
    model = ItemReembolso
    extra = 1
    readonly_fields = ('amount', 'base_amount')

    def has_add_permission(self, request, obj=None):
        return es_admin(request.user) and (obj is None or obj.status == 'PENDING')

    def has_change_permission(self, request, obj=None):
        return self.has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self.has_add_permission(request, obj)


@admin.register(Venta)
class VentaAdmin(PermisosPOS, admin.ModelAdmin):
    list_display = ('receipt_number', 'seller', 'status', 'total', 'created_at')
    list_filter = ('status', 'payment_method', 'created_at')
    search_fields = ('receipt_number', 'seller__username')
    inlines = (ItemVentaInline,)
    actions = ('completar_ventas', 'cancelar_ventas')
    readonly_fields = ('receipt_number', 'seller', 'status', 'subtotal', 'discount', 'total', 'change_amount', 'created_at', 'completed_at', 'updated_at', 'legacy')

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.status == 'DRAFT')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.seller = request.user
        obj.save()

    @admin.action(description='Completar ventas seleccionadas')
    def completar_ventas(self, request, queryset):
        for venta in queryset:
            try:
                venta.completar(user=request.user)
            except (ValidationError, PermissionDenied) as exc:
                self.message_user(request, str(exc), messages.ERROR)

    @admin.action(description='Cancelar borradores seleccionados')
    def cancelar_ventas(self, request, queryset):
        for venta in queryset:
            try:
                venta.cancelar(user=request.user)
            except (ValidationError, PermissionDenied) as exc:
                self.message_user(request, str(exc), messages.ERROR)


class ProductoResource(resources.ModelResource):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    class Meta:
        model = Producto
        import_id_fields = ('sku',)
        exclude = ('id', 'fecha_creacion', 'updated_at')
        use_transactions = True
        clean_model_instances = True

    def do_instance_save(self, instance, is_create):
        instance.save(user=self.user, reason='Importación de inventario')


@admin.register(Producto)
class ProductoAdmin(PermisosPOS, ImportExportModelAdmin):
    list_display = ('sku', 'nombre', 'categoria', 'precio', 'cost_price', 'stock', 'min_stock', 'disponible')
    list_filter = ('disponible', 'categoria')
    search_fields = ('sku', 'barcode', 'nombre')
    form = ProductoForm
    resource_classes = (ProductoResource,)
    actions = ('desactivar',)

    def get_import_resource_kwargs(self, request, **kwargs):
        return {'user': request.user}

    def has_import_permission(self, request):
        return es_admin(request.user)

    def has_export_permission(self, request):
        return es_admin(request.user)

    def save_model(self, request, obj, form, change):
        obj.save(user=request.user, expected_stock=form.cleaned_data.get('stock_original'))

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as exc:
            self.message_user(request, str(exc), messages.ERROR)
            return redirect(request.path)

    @admin.action(description='Desactivar productos seleccionados')
    def desactivar(self, request, queryset):
        queryset.delete()


@admin.register(Reembolso)
class ReembolsoAdmin(PermisosPOS, admin.ModelAdmin):
    list_display = ('id', 'sale', 'user', 'amount', 'status', 'created_at')
    inlines = (ItemReembolsoInline,)
    actions = ('procesar_reembolsos',)
    readonly_fields = ('user', 'amount', 'status', 'created_at', 'completed_at')

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.status == 'PENDING')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.user = request.user
        obj.save()

    @admin.action(description='Procesar reembolsos seleccionados')
    def procesar_reembolsos(self, request, queryset):
        for reembolso in queryset:
            try:
                reembolso.procesar(user=request.user)
            except (ValidationError, PermissionDenied) as exc:
                self.message_user(request, str(exc), messages.ERROR)


@admin.register(MovimientoInventario)
class MovimientoInventarioAdmin(SoloLectura):
    list_display = ('product', 'movement_type', 'quantity', 'previous_stock', 'new_stock', 'user', 'created_at')
    list_filter = ('movement_type', 'created_at')
    search_fields = ('product__sku', 'product__nombre', 'reason')


@admin.register(Categoria)
class CategoriaAdmin(PermisosPOS, admin.ModelAdmin):
    list_display = ('name', 'active')


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'active')

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Alerta, SoloLectura)


@admin.register(Tarea)
class TareaAdmin(ImportExportModelAdmin):
    list_display = ('titulo', 'completada')
