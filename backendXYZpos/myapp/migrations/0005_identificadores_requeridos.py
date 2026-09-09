from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def completar_datos_existentes(apps, schema_editor):

    
    alias = schema_editor.connection.alias
    Producto = apps.get_model('myapp', 'Producto')
    Venta = apps.get_model('myapp', 'Venta')
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    Perfil = apps.get_model('myapp', 'PerfilUsuario')
    Movimiento = apps.get_model('myapp', 'MovimientoInventario')
    Alerta = apps.get_model('myapp', 'Alerta')

    def libre(modelo, campo, base):
        candidato, numero = base, 0
        while modelo.objects.using(alias).filter(**{campo: candidato}).exists():
            numero += 1
            candidato = f'{base}-{numero}'
        return candidato

    for producto in Producto.objects.using(alias).filter(models.Q(sku__isnull=True) | models.Q(sku='')):
        producto.sku = libre(Producto, 'sku', f'LEGACY-{producto.pk:06d}')
        producto.save(using=alias, update_fields=['sku'])
    pendientes = Venta.objects.using(alias).filter(models.Q(seller__isnull=True) | models.Q(receipt_number__isnull=True) | models.Q(receipt_number=''))
    vendedor = None
    saldos = Producto.objects.using(alias).filter(stock__gt=0, movimientos__isnull=True)
    if pendientes.filter(seller__isnull=True).exists() or saldos.exists():
        # Nunca reutilizar ni desactivar una cuenta humana que tenga este nombre.
        nombre = libre(User, 'username', 'migration-system')
        vendedor = User.objects.using(alias).create(username=nombre, password='!', is_active=False, is_staff=False, is_superuser=False)
    for venta in pendientes:
        if not venta.receipt_number:
            venta.receipt_number = libre(Venta, 'receipt_number', f'LEGACY-{venta.pk:08d}')
        if venta.seller_id is None:
            venta.seller_id = vendedor.pk
        if venta.status == 'DRAFT':
            venta.status = 'COMPLETED'
            venta.completed_at = venta.created_at
            venta.subtotal = venta.total
        venta.save(using=alias)
    for producto in saldos:
        Movimiento.objects.using(alias).create(product_id=producto.pk, movement_type='INITIAL',
            quantity=producto.stock, previous_stock=0, new_stock=producto.stock, user_id=vendedor.pk,
            reason='Saldo inicial migrado; no se dispone de movimientos anteriores.')
    for producto in Producto.objects.using(alias).filter(disponible=True, stock__lte=models.F('min_stock')):
        if not Alerta.objects.using(alias).filter(product_id=producto.pk, type='LOW_STOCK', status='UNREAD').exists():
            Alerta.objects.using(alias).create(product_id=producto.pk, type='LOW_STOCK', status='UNREAD',
                message=f'Stock bajo de {producto.nombre}: {producto.stock} unidades.')
    for user in User.objects.using(alias).all():
        Perfil.objects.using(alias).get_or_create(user_id=user.pk, defaults={'role': 'ADMIN' if user.is_superuser else 'SELLER', 'active': user.is_active})


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('myapp', '0004_alerta_categoria_itemreembolso_itemventa_and_more')]
    operations = [
        migrations.RunPython(completar_datos_existentes, migrations.RunPython.noop),
        migrations.AlterField(model_name='producto', name='sku', field=models.CharField(max_length=50, unique=True)),
        migrations.AlterField(model_name='venta', name='receipt_number', field=models.CharField(max_length=50, unique=True)),
        migrations.AlterField(
            model_name='venta', name='seller',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ventas', to=settings.AUTH_USER_MODEL),
        ),
    ]
