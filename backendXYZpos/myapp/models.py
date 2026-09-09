from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone


def numero_comprobante():
    return f'POS-{uuid4().hex.upper()}'


class OperacionesSegurasQuerySet(models.QuerySet):
    """Las escrituras masivas omiten save(), validaciones y bloqueos."""

    def update(self, **kwargs):
        raise ValidationError('Use save() o la operación de negocio; update() omite la auditoría.')

    def bulk_update(self, *args, **kwargs):
        raise ValidationError('La actualización masiva no está habilitada para datos del POS.')

    def bulk_create(self, *args, **kwargs):
        raise ValidationError('Use create()/save() para validar cada registro del POS.')

    @transaction.atomic
    def delete(self):
        total, detalle = 0, {}
        for objeto in self.order_by('pk'):
            cantidad, modelos = objeto.delete()
            total += cantidad
            for modelo, valor in modelos.items():
                detalle[modelo] = detalle.get(modelo, 0) + valor
        return total, detalle


class ModeloProtegido(models.Model):
    objects = OperacionesSegurasQuerySet.as_manager()

    class Meta:
        abstract = True


def _guardar_validado(objeto):
    """Uso interno, después de adquirir los bloqueos y validar la transición."""
    objeto.full_clean()
    models.Model.save(objeto)


def _registrar_stock(producto, delta, tipo, usuario, *, venta=None, reembolso=None, motivo=''):
    anterior = producto.stock
    producto.stock += delta
    if producto.stock < 0:
        raise ValidationError('El stock no puede ser negativo.')
    models.Model.save(producto, update_fields=['stock', 'updated_at'])
    if delta:
        movimiento = MovimientoInventario(
            product=producto, movement_type=tipo, quantity=delta,
            previous_stock=anterior, new_stock=producto.stock, user=usuario,
            sale=venta, refund=reembolso, reason=motivo,
        )
        _guardar_validado(movimiento)
    producto.actualizar_alerta_stock()


class Tarea(models.Model):
    titulo = models.CharField(max_length=200)
    completada = models.BooleanField(default=False)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.titulo


class PerfilUsuario(models.Model):
    class Rol(models.TextChoices):
        ADMIN = 'ADMIN', 'Administrador'
        SELLER = 'SELLER', 'Vendedor'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='perfil_pos')
    role = models.CharField(max_length=10, choices=Rol.choices, default=Rol.SELLER)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.get_full_name() or self.user.username} ({self.get_role_display()})'


class Categoria(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class Producto(ModeloProtegido):
    sku = models.CharField(max_length=50, unique=True)
    barcode = models.CharField(max_length=100, unique=True, null=True, blank=True)
    nombre = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True, blank=True, related_name='productos')
    precio = models.DecimalField(max_digits=12, decimal_places=2)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    stock = models.PositiveIntegerField(default=0)
    min_stock = models.PositiveIntegerField(default=0)
    image_url = models.URLField(max_length=500, blank=True)
    disponible = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('nombre',)
        constraints = [
            models.CheckConstraint(condition=~Q(sku=''), name='producto_sku_no_vacio'),
            models.CheckConstraint(condition=Q(precio__gte=0), name='producto_precio_no_negativo'),
            models.CheckConstraint(condition=Q(cost_price__isnull=True) | Q(cost_price__gte=0), name='producto_costo_no_negativo'),
        ]

    def clean(self):
        self.sku = (self.sku or '').strip()
        self.barcode = (self.barcode or '').strip() or None
        if not self.sku:
            raise ValidationError({'sku': 'El SKU es obligatorio.'})

    @transaction.atomic
    def save(self, *args, user=None, reason='', expected_stock=None, **kwargs):
        from .permissions import exigir_admin
        previo = None
        if self.pk:
            previo = Producto.objects.select_for_update().get(pk=self.pk)
        anterior = previo.stock if previo else 0
        if expected_stock is not None and expected_stock != anterior:
            raise ValidationError('El stock cambió mientras editabas. Recarga el producto.')
        campos = kwargs.get('update_fields')
        destino = self.stock if campos is None or 'stock' in campos else anterior
        if destino != anterior:
            exigir_admin(user)
        self.full_clean()
        self.stock = anterior
        super().save(*args, **kwargs)
        if destino != anterior:
            tipo = 'INITIAL' if previo is None else ('ADJUSTMENT_IN' if destino > anterior else 'ADJUSTMENT_OUT')
            _registrar_stock(self, destino - anterior, tipo, user, motivo=reason or 'Edición de inventario')
        else:
            self.actualizar_alerta_stock()

    @transaction.atomic
    def reabastecer(self, cantidad, user, reason=''):
        from .permissions import exigir_admin
        exigir_admin(user)
        if isinstance(cantidad, bool) or not isinstance(cantidad, int) or cantidad <= 0:
            raise ValidationError('La reposición requiere una cantidad entera positiva.')
        producto = Producto.objects.select_for_update().get(pk=self.pk)
        _registrar_stock(producto, cantidad, 'RESTOCK', user, motivo=reason)
        self.refresh_from_db()

    @transaction.atomic
    def delete(self, *args, **kwargs):
        producto = Producto.objects.select_for_update().get(pk=self.pk)
        # Baja lógica uniforme, también desde acciones masivas; conserva todo el rastro.
        producto.disponible = False
        producto.save(update_fields=['disponible', 'updated_at'])
        self.disponible = False
        return 0, {}

    @property
    def stock_bajo(self):
        return self.stock <= self.min_stock

    def actualizar_alerta_stock(self):
        if self.stock_bajo and self.disponible:
            Alerta.objects.update_or_create(
                product=self, type='LOW_STOCK', status=Alerta.Estado.UNREAD,
                defaults={'message': f'Stock bajo de {self.nombre}: {self.stock} unidades.'},
            )
        else:
            self.alertas.filter(type='LOW_STOCK', status=Alerta.Estado.UNREAD).update(
                status=Alerta.Estado.READ, read_at=timezone.now()
            )

    def __str__(self):
        return f'{self.sku} - {self.nombre}'


class Venta(ModeloProtegido):
    class Estado(models.TextChoices):
        DRAFT = 'DRAFT', 'Borrador'
        COMPLETED = 'COMPLETED', 'Completada'
        CANCELLED = 'CANCELLED', 'Cancelada'
        PARTIALLY_REFUNDED = 'PARTIALLY_REFUNDED', 'Parcialmente reembolsada'
        REFUNDED = 'REFUNDED', 'Reembolsada'

    receipt_number = models.CharField(max_length=50, unique=True, default=numero_comprobante)
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='ventas')
    status = models.CharField(max_length=25, choices=Estado.choices, default=Estado.DRAFT)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=30, blank=True)
    amount_received = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    legacy = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.CheckConstraint(condition=~Q(receipt_number=''), name='venta_comprobante_no_vacio'),
            models.CheckConstraint(condition=Q(status__in=['DRAFT', 'COMPLETED', 'CANCELLED', 'PARTIALLY_REFUNDED', 'REFUNDED']), name='venta_estado_valido'),
            models.CheckConstraint(condition=Q(status__in=['DRAFT', 'CANCELLED'], completed_at__isnull=True) | Q(status__in=['COMPLETED', 'PARTIALLY_REFUNDED', 'REFUNDED'], completed_at__isnull=False), name='venta_fecha_coherente'),
            models.CheckConstraint(condition=Q(amount_received__isnull=True) | Q(amount_received__gte=0), name='venta_recibido_no_negativo'),
            models.CheckConstraint(condition=Q(change_amount__isnull=True) | Q(change_amount__gte=0), name='venta_cambio_no_negativo'),
            models.CheckConstraint(condition=Q(subtotal__gte=0), name='venta_subtotal_no_negativo'),
            models.CheckConstraint(condition=Q(discount__gte=0), name='venta_descuento_no_negativo'),
            models.CheckConstraint(condition=Q(tax__gte=0), name='venta_impuesto_no_negativo'),
            models.CheckConstraint(condition=Q(total__gte=0), name='venta_total_no_negativo'),
        ]

    @transaction.atomic
    def save(self, *args, **kwargs):
        from .permissions import exigir_usuario
        if not self.seller_id:
            raise ValidationError('El vendedor es obligatorio.')
        if self.pk:
            anterior = Venta.objects.select_for_update().get(pk=self.pk)
            if anterior.status != self.Estado.DRAFT or anterior.legacy:
                raise ValidationError('Una venta cerrada no se puede editar.')
            protegidos = ('status', 'seller_id', 'receipt_number', 'subtotal', 'discount', 'total', 'change_amount', 'created_at', 'completed_at', 'legacy')
            if any(getattr(self, campo) != getattr(anterior, campo) for campo in protegidos):
                raise ValidationError('Los estados y totales solo cambian mediante las operaciones de venta.')
        else:
            exigir_usuario(self.seller)
            if self.status != self.Estado.DRAFT or self.legacy or self.subtotal or self.discount or self.total or self.completed_at or self.change_amount is not None:
                raise ValidationError('Las ventas nuevas deben ser borradores sin totales calculados.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Las ventas se cancelan; no se eliminan.')

    @transaction.atomic
    def completar(self, user=None):
        from .permissions import exigir_venta
        venta = Venta.objects.select_for_update().get(pk=self.pk)
        exigir_venta(user or venta.seller, venta)
        if venta.status != self.Estado.DRAFT:
            raise ValidationError('Solo se puede completar una venta en borrador.')
        if venta.legacy:
            raise ValidationError('La venta histórica no tiene detalles verificables.')
        items = list(venta.items.order_by('product_id'))
        if not items:
            raise ValidationError('La venta debe tener al menos un producto.')
        subtotal, descuento = Decimal('0.00'), Decimal('0.00')
        for item in items:
            producto = Producto.objects.select_for_update().get(pk=item.product_id)
            if not producto.disponible:
                raise ValidationError(f'El producto {producto.nombre} no está activo.')
            if item.quantity > producto.stock:
                raise ValidationError(f'Stock insuficiente para {producto.nombre}: disponible {producto.stock}.')
            item.product_name = producto.nombre
            item.unit_price = producto.precio
            item.unit_cost = producto.cost_price
            item.subtotal = item.unit_price * item.quantity - item.discount
            item.full_clean()
            models.Model.save(item)
            _registrar_stock(producto, -item.quantity, 'SALE', user or venta.seller, venta=venta)
            subtotal += item.unit_price * item.quantity
            descuento += item.discount
        venta.subtotal, venta.discount = subtotal, descuento
        if subtotal == descuento and venta.tax:
            raise ValidationError('No se pueden cobrar impuestos con una base de venta cero.')
        venta.total = subtotal - descuento + venta.tax
        if venta.amount_received is not None:
            if venta.amount_received < venta.total:
                raise ValidationError('El monto recibido no cubre el total de la venta.')
            venta.change_amount = venta.amount_received - venta.total
        venta.status = self.Estado.COMPLETED
        venta.completed_at = timezone.now()
        venta.full_clean()
        models.Model.save(venta)
        self.refresh_from_db()
        return self

    @transaction.atomic
    def cancelar(self, user=None):
        from .permissions import exigir_venta
        venta = Venta.objects.select_for_update().get(pk=self.pk)
        exigir_venta(user or venta.seller, venta)
        if venta.status != self.Estado.DRAFT:
            raise ValidationError('Una venta completada debe procesarse mediante un reembolso.')
        venta.status = self.Estado.CANCELLED
        _guardar_validado(venta)
        self.refresh_from_db()

    def __str__(self):
        return f'Venta {self.receipt_number} - {self.total}'


class ItemVenta(ModeloProtegido):
    sale = models.ForeignKey(Venta, on_delete=models.PROTECT, related_name='items')
    product = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='items_venta')
    product_name = models.CharField(max_length=100, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(unit_price__gte=0) & Q(subtotal__gte=0), name='item_venta_valores_validos'),
            models.CheckConstraint(condition=Q(unit_cost__isnull=True) | Q(unit_cost__gte=0), name='item_venta_costo_valido'),
            models.CheckConstraint(condition=Q(quantity__gt=0), name='item_venta_cantidad_positiva'),
            models.CheckConstraint(condition=Q(discount__gte=0), name='item_venta_descuento_no_negativo'),
            models.UniqueConstraint(fields=('sale', 'product'), name='producto_unico_por_venta'),
        ]

    def clean(self):
        if self.sale_id and self.sale.status != Venta.Estado.DRAFT:
            raise ValidationError('Los detalles solo pueden modificarse en una venta en borrador.')
        if self.quantity is not None and self.unit_price is not None and self.discount is not None and self.discount > self.unit_price * self.quantity:
            raise ValidationError('El descuento no puede superar el valor del producto.')

    @transaction.atomic
    def save(self, *args, **kwargs):
        venta = Venta.objects.select_for_update().get(pk=self.sale_id)
        if venta.status != Venta.Estado.DRAFT:
            raise ValidationError('Los detalles de ventas cerradas son inmutables.')
        if self.pk and ItemVenta.objects.get(pk=self.pk).sale_id != self.sale_id:
            raise ValidationError('No se puede trasladar un detalle a otra venta.')
        self.sale = venta
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValidationError('La cantidad debe ser un entero positivo.')
        producto = Producto.objects.get(pk=self.product_id)
        if not producto.disponible:
            raise ValidationError('El producto está inactivo.')
        self.product_name, self.unit_price, self.unit_cost = producto.nombre, producto.precio, producto.cost_price
        self.discount = Decimal(str(self.discount))
        self.subtotal = self.unit_price * self.quantity - self.discount
        self.full_clean()
        return super().save(*args, **kwargs)

    @transaction.atomic
    def delete(self, *args, **kwargs):
        venta = Venta.objects.select_for_update().get(pk=self.sale_id)
        if venta.status != Venta.Estado.DRAFT:
            raise ValidationError('Los detalles de ventas cerradas son inmutables.')
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f'{self.quantity} x {self.product_name or self.product.nombre}'


class Reembolso(ModeloProtegido):
    class Estado(models.TextChoices):
        PENDING = 'PENDING', 'Pendiente'
        COMPLETED = 'COMPLETED', 'Completado'
        CANCELLED = 'CANCELLED', 'Cancelado'

    sale = models.ForeignKey(Venta, on_delete=models.PROTECT, related_name='reembolsos')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='reembolsos')
    reason = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name='refund_total_valido'),
            models.CheckConstraint(condition=Q(status__in=['PENDING', 'CANCELLED'], completed_at__isnull=True) | Q(status='COMPLETED', completed_at__isnull=False), name='refund_estado_fecha_valida'),
        ]

    def clean(self):
        if self.sale_id and self.status == self.Estado.PENDING:
            venta = Venta.objects.get(pk=self.sale_id)
            if venta.status not in (Venta.Estado.COMPLETED, Venta.Estado.PARTIALLY_REFUNDED) or venta.legacy:
                raise ValidationError({'sale': 'Selecciona una venta confirmada con detalles verificables.'})

    @transaction.atomic
    def save(self, *args, **kwargs):
        from .permissions import exigir_admin
        exigir_admin(self.user)
        venta = Venta.objects.select_for_update().get(pk=self.sale_id)
        if venta.status not in (Venta.Estado.COMPLETED, Venta.Estado.PARTIALLY_REFUNDED) or venta.legacy:
            raise ValidationError('Solo se reembolsan ventas confirmadas con detalles verificables.')
        if self.pk:
            previo = Reembolso.objects.select_for_update().get(pk=self.pk)
            if previo.status != self.Estado.PENDING or any(getattr(self, f) != getattr(previo, f) for f in ('sale_id', 'user_id', 'status', 'amount', 'completed_at')):
                raise ValidationError('El reembolso cerrado y sus importes son inmutables.')
        elif self.status != self.Estado.PENDING or self.amount or self.completed_at:
            raise ValidationError('El reembolso debe comenzar pendiente, sin importe calculado.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Los reembolsos se conservan para auditoría.')

    @transaction.atomic
    def procesar(self, user=None):
        from .permissions import exigir_admin
        # Toda operación toma primero la venta común: serializa reembolsos distintos.
        sale_id = Reembolso.objects.values_list('sale_id', flat=True).get(pk=self.pk)
        venta = Venta.objects.select_for_update().get(pk=sale_id)
        reembolso = Reembolso.objects.select_for_update().get(pk=self.pk)
        exigir_admin(user or reembolso.user)
        if reembolso.status != self.Estado.PENDING:
            raise ValidationError('Este reembolso ya fue procesado.')
        if venta.status not in (Venta.Estado.COMPLETED, Venta.Estado.PARTIALLY_REFUNDED) or venta.legacy:
            raise ValidationError('La venta no permite reembolsos.')
        items = list(reembolso.items.select_related('sale_item').order_by('product_id'))
        if not items:
            raise ValidationError('El reembolso debe tener al menos un detalle.')
        total = Decimal('0.00')
        # Distribuye impuestos en centavos por acumulados: la devolución total suma
        # exactamente el total pagado, incluso con descuentos y redondeos.
        bases, acumulado = {}, Decimal('0.00')
        base_venta = venta.subtotal - venta.discount
        impuesto_anterior = Decimal('0.00')
        for detalle in venta.items.order_by('pk'):
            acumulado += detalle.subtotal
            impuesto = (venta.tax * acumulado / base_venta).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if base_venta else Decimal('0.00')
            bases[detalle.pk] = detalle.subtotal + impuesto - impuesto_anterior
            impuesto_anterior = impuesto
        for item in items:
            item.full_clean()
            devuelto = ItemReembolso.objects.filter(
                sale_item=item.sale_item, refund__status=self.Estado.COMPLETED
            ).exclude(refund=reembolso).aggregate(total=models.Sum('quantity'))['total'] or 0
            if devuelto + item.quantity > item.sale_item.quantity:
                raise ValidationError('La cantidad reembolsada supera la cantidad vendida.')
            base_item = bases[item.sale_item_id]
            previo = (base_item * devuelto / item.sale_item.quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            nuevo = (base_item * (devuelto + item.quantity) / item.sale_item.quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            item.amount = nuevo - previo
            base_previa = (item.sale_item.subtotal * devuelto / item.sale_item.quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            base_nueva = (item.sale_item.subtotal * (devuelto + item.quantity) / item.sale_item.quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            item.base_amount = base_nueva - base_previa
            models.Model.save(item, update_fields=['amount', 'base_amount'])
            total += item.amount
            if item.restore_stock:
                producto = Producto.objects.select_for_update().get(pk=item.product_id)
                _registrar_stock(producto, item.quantity, 'REFUND', user or reembolso.user,
                                 venta=venta, reembolso=reembolso, motivo=reembolso.reason)
        reembolso.amount, reembolso.status = total, self.Estado.COMPLETED
        reembolso.completed_at = timezone.now()
        _guardar_validado(reembolso)
        unidades = venta.items.aggregate(total=models.Sum('quantity'))['total']
        devueltas = ItemReembolso.objects.filter(refund__sale=venta, refund__status=self.Estado.COMPLETED).aggregate(total=models.Sum('quantity'))['total'] or 0
        venta.status = Venta.Estado.REFUNDED if devueltas == unidades else Venta.Estado.PARTIALLY_REFUNDED
        _guardar_validado(venta)
        self.refresh_from_db()
        return self


class ItemReembolso(ModeloProtegido):
    refund = models.ForeignKey(Reembolso, on_delete=models.PROTECT, related_name='items')
    sale_item = models.ForeignKey(ItemVenta, on_delete=models.PROTECT, related_name='items_reembolso')
    product = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='items_reembolso')
    quantity = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    base_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False)
    restore_stock = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('refund', 'sale_item'), name='detalle_unico_por_reembolso'),
            models.CheckConstraint(condition=Q(base_amount__gte=0), name='refund_base_valida'),
            models.CheckConstraint(condition=Q(quantity__gt=0), name='reembolso_cantidad_positiva'),
            models.CheckConstraint(condition=Q(amount__gte=0), name='reembolso_importe_no_negativo'),
        ]

    def clean(self):
        if self.refund_id and self.sale_item_id:
            if self.sale_item.sale_id != self.refund.sale_id:
                raise ValidationError('El detalle no pertenece a la venta reembolsada.')
            if self.product_id != self.sale_item.product_id:
                raise ValidationError('El producto no coincide con el detalle vendido.')

    @transaction.atomic
    def save(self, *args, **kwargs):
        sale_id = Reembolso.objects.values_list('sale_id', flat=True).get(pk=self.refund_id)
        Venta.objects.select_for_update().get(pk=sale_id)
        reembolso = Reembolso.objects.select_for_update().get(pk=self.refund_id)
        if reembolso.status != Reembolso.Estado.PENDING:
            raise ValidationError('El reembolso procesado es inmutable.')
        if self.pk and ItemReembolso.objects.get(pk=self.pk).refund_id != self.refund_id:
            raise ValidationError('No se puede trasladar el detalle a otro reembolso.')
        self.refund = reembolso
        self.amount = Decimal('0.00')  # El importe definitivo se calcula al procesar.
        self.base_amount = Decimal('0.00')
        self.full_clean()
        return super().save(*args, **kwargs)

    @transaction.atomic
    def delete(self, *args, **kwargs):
        sale_id = Reembolso.objects.values_list('sale_id', flat=True).get(pk=self.refund_id)
        Venta.objects.select_for_update().get(pk=sale_id)
        reembolso = Reembolso.objects.select_for_update().get(pk=self.refund_id)
        if reembolso.status != Reembolso.Estado.PENDING:
            raise ValidationError('El reembolso procesado es inmutable.')
        return super().delete(*args, **kwargs)


class MovimientoInventario(ModeloProtegido):
    class Tipo(models.TextChoices):
        INITIAL = 'INITIAL', 'Inicial'
        RESTOCK = 'RESTOCK', 'Reabastecimiento'
        SALE = 'SALE', 'Venta'
        REFUND = 'REFUND', 'Reembolso'
        ADJUSTMENT_IN = 'ADJUSTMENT_IN', 'Ajuste de entrada'
        ADJUSTMENT_OUT = 'ADJUSTMENT_OUT', 'Ajuste de salida'

    product = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='movimientos')
    movement_type = models.CharField(max_length=20, choices=Tipo.choices)
    quantity = models.IntegerField()
    previous_stock = models.PositiveIntegerField()
    new_stock = models.PositiveIntegerField()
    sale = models.ForeignKey(Venta, on_delete=models.PROTECT, null=True, blank=True, related_name='movimientos')
    refund = models.ForeignKey(Reembolso, on_delete=models.PROTECT, null=True, blank=True, related_name='movimientos')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='movimientos_inventario')
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.CheckConstraint(condition=~Q(quantity=0), name='movimiento_cantidad_no_cero'),
            models.UniqueConstraint(fields=('sale', 'product'), condition=Q(movement_type='SALE'), name='movimiento_venta_unico'),
            models.UniqueConstraint(fields=('refund', 'product'), condition=Q(movement_type='REFUND'), name='movimiento_reembolso_unico'),
            models.CheckConstraint(condition=Q(new_stock=F('previous_stock') + F('quantity')), name='movimiento_balance_valido'),
            models.CheckConstraint(condition=Q(movement_type__in=['SALE', 'ADJUSTMENT_OUT'], quantity__lt=0) | Q(movement_type__in=['INITIAL', 'RESTOCK', 'REFUND', 'ADJUSTMENT_IN'], quantity__gt=0), name='movimiento_signo_valido'),
            models.CheckConstraint(condition=Q(movement_type='SALE', sale__isnull=False, refund__isnull=True) | Q(movement_type='REFUND', sale__isnull=False, refund__isnull=False) | Q(movement_type__in=['INITIAL', 'RESTOCK', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT'], sale__isnull=True, refund__isnull=True), name='movimiento_referencia_valida'),
        ]

    def clean(self):
        if self.refund_id and self.refund.sale_id != self.sale_id:
            raise ValidationError('La devolución y la venta no coinciden.')
        if self.movement_type == 'INITIAL' and self.previous_stock != 0:
            raise ValidationError('El movimiento inicial debe partir de cero.')

    def save(self, *args, **kwargs):
        raise ValidationError('Los movimientos se crean únicamente al modificar inventario, vender o reembolsar.')

    def delete(self, *args, **kwargs):
        raise ValidationError('Los movimientos son un registro histórico inmutable.')


class Alerta(models.Model):
    class Estado(models.TextChoices):
        UNREAD = 'UNREAD', 'No leída'
        READ = 'READ', 'Leída'

    type = models.CharField(max_length=30, default='LOW_STOCK')
    product = models.ForeignKey(Producto, on_delete=models.CASCADE, null=True, blank=True, related_name='alertas')
    message = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Estado.choices, default=Estado.UNREAD)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(fields=('product', 'type'), condition=Q(status='UNREAD'), name='alerta_abierta_unica'),
        ]
