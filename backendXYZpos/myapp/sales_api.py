from django.core.exceptions import ValidationError as ModelValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from .models import ItemReembolso, ItemVenta, Producto, Reembolso, Venta
from .permissions import EsUsuarioPOS, es_admin, exigir_admin
from .reports import montos_json


class LineaEntrada(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Producto.objects.filter(disponible=True))
    quantity = serializers.IntegerField(min_value=1)
    discount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)


class VentaEntrada(serializers.Serializer):
    items = LineaEntrada(many=True, allow_empty=False)
    payment_method = serializers.ChoiceField(choices=['CASH', 'CARD', 'TRANSFER'], default='CASH')
    amount_received = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, allow_null=True, default=None)
    tax = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)

    def validate(self, data):
        ids = [item['product'].pk for item in data['items']]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError('Agrupa cada producto en una única línea.')
        if not es_admin(self.context['request'].user) and (data['tax'] or any(i['discount'] for i in data['items'])):
            raise serializers.ValidationError('Impuestos y descuentos manuales requieren administrador.')
        return data


class DevolucionEntrada(serializers.Serializer):
    sale_item = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)
    restore_stock = serializers.BooleanField(default=True)


class ReembolsoEntrada(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False)
    items = DevolucionEntrada(many=True, allow_empty=False)


def detalle_venta(venta, user):
    items = []
    for item in venta.items.all():
        fila = dict(id=item.pk, product=item.product_id, product_name=item.product_name,
                    quantity=item.quantity, unit_price=item.unit_price, discount=item.discount, subtotal=item.subtotal)
        if es_admin(user):
            fila['unit_cost'] = item.unit_cost
        items.append(fila)
    return montos_json(dict(id=venta.pk, receipt_number=venta.receipt_number, seller=venta.seller_id,
                           status=venta.status, subtotal=venta.subtotal, discount=venta.discount,
                           tax=venta.tax, total=venta.total, amount_received=venta.amount_received,
                           change_amount=venta.change_amount, payment_method=venta.payment_method, items=items))


class VentaViewSet(ViewSet):
    permission_classes = (EsUsuarioPOS,)

    def ventas(self, request):
        qs = Venta.objects.prefetch_related('items')
        return qs if es_admin(request.user) else qs.filter(seller=request.user)

    def list(self, request):
        return Response([detalle_venta(v, request.user) for v in self.ventas(request)[:100]])

    def retrieve(self, request, pk=None):
        return Response(detalle_venta(get_object_or_404(self.ventas(request), pk=pk), request.user))

    def _guardar(self, request, pk=None):
        datos = VentaEntrada(data=request.data, context={'request': request})
        datos.is_valid(raise_exception=True)
        entrada = dict(datos.validated_data)
        lineas = entrada.pop('items')
        try:
            with transaction.atomic():
                if pk:
                    venta = get_object_or_404(self.ventas(request).select_for_update(), pk=pk)
                    if venta.status != 'DRAFT':
                        raise ModelValidationError('Solo se pueden editar borradores.')
                    venta.items.all().delete()
                    for campo, valor in entrada.items():
                        setattr(venta, campo, valor)
                    venta.save()
                else:
                    venta = Venta.objects.create(seller=request.user, **entrada)
                for linea in lineas:
                    ItemVenta.objects.create(sale=venta, **linea)
        except (ModelValidationError, IntegrityError) as exc:
            raise ValidationError(exc.messages if isinstance(exc, ModelValidationError) else 'Los datos entran en conflicto con otro registro.')
        venta = Venta.objects.get(pk=venta.pk)  # No reutilizar un prefetch anterior a la edición.
        return Response(detalle_venta(venta, request.user), status=200 if pk else 201)

    def create(self, request):
        return self._guardar(request)

    def update(self, request, pk=None):
        return self._guardar(request, pk)

    @action(detail=True, methods=['post'])
    def completar(self, request, pk=None):
        venta = get_object_or_404(self.ventas(request), pk=pk)
        try:
            venta.completar(user=request.user)
        except ModelValidationError as exc:
            raise ValidationError(exc.messages)
        return Response(detalle_venta(venta, request.user))

    @action(detail=True, methods=['post'])
    def cancelar(self, request, pk=None):
        venta = get_object_or_404(self.ventas(request), pk=pk)
        try:
            venta.cancelar(user=request.user)
        except ModelValidationError as exc:
            raise ValidationError(exc.messages)
        return Response(detalle_venta(venta, request.user))

    @action(detail=True, methods=['post'])
    def reembolsar(self, request, pk=None):
        exigir_admin(request.user)
        venta = get_object_or_404(self.ventas(request), pk=pk)
        datos = ReembolsoEntrada(data=request.data)
        datos.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                reembolso = Reembolso.objects.create(sale=venta, user=request.user, reason=datos.validated_data['reason'])
                for linea in datos.validated_data['items']:
                    item = get_object_or_404(ItemVenta, pk=linea['sale_item'], sale=venta)
                    ItemReembolso.objects.create(refund=reembolso, sale_item=item, product_id=item.product_id,
                                                 quantity=linea['quantity'], restore_stock=linea['restore_stock'])
                reembolso.procesar(user=request.user)
        except (ModelValidationError, IntegrityError) as exc:
            raise ValidationError(exc.messages if isinstance(exc, ModelValidationError) else 'Revisa los detalles duplicados.')
        return Response(montos_json({'id': reembolso.pk, 'amount': reembolso.amount, 'status': reembolso.status}), status=201)
