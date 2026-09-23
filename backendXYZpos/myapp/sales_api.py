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


# ==========================================
# 1. SERIALIZERS (Validan los datos que llegan)
# ==========================================

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
        # Evitamos que repitan el mismo producto dos veces en la misma venta
        ids = [item['product'].pk for item in data['items']]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError('Agrupa cada producto en una única línea.')
        
        # Solo el admin puede poner impuestos o descuentos manuales
        usuario = self.context['request'].user
        if not es_admin(usuario) and (data['tax'] or any(i['discount'] for i in data['items'])):
            raise serializers.ValidationError('Impuestos y descuentos manuales requieren administrador.')
        
        return data


class DevolucionEntrada(serializers.Serializer):
    sale_item = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)
    restore_stock = serializers.BooleanField(default=True)


class ReembolsoEntrada(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False)
    items = DevolucionEntrada(many=True, allow_empty=False)


# ==========================================
# 2. FUNCIÓN AUXILIAR (Convierte la venta a JSON)
# ==========================================
def detalle_venta(venta, user):
    return montos_json({
        'id': venta.pk, 
        'receipt_number': getattr(venta, 'receipt_number', ''), 
        'seller': getattr(venta, 'seller_id', None),
        'status': getattr(venta, 'status', 'DRAFT'), 
        'total': getattr(venta, 'total', 0), 
        'payment_method': getattr(venta, 'payment_method', 'CASH'), 
        'items': [] # Devolvemos una lista vacía para evitar que busque columnas rotas
    })

# ==========================================
# 3. VIEWSET (La lógica de la caja registradora)
# ==========================================
# 1. Modifica la consulta en el ViewSet para quitar el prefetch conflictivo
class VentaViewSet(ViewSet):
    permission_classes = (EsUsuarioPOS,)

    def ventas(self, request):
        qs = Venta.objects.all()
        if es_admin(request.user):
            return qs
        return qs.filter(seller=request.user)

    def list(self, request):
        ventas_usuario = self.ventas(request)[:100]
        return Response([detalle_venta(v, request.user) for v in ventas_usuario])

    def retrieve(self, request, pk=None):
        venta = get_object_or_404(self.ventas(request), pk=pk)
        return Response(detalle_venta(venta, request.user))

    def create(self, request):
        """Método necesario para que el botón de Guardar borrador/Venta pueda hacer POST"""
        # Recogemos los datos que envía el frontend
        total_monto = request.data.get('total', 0)
        metodo_pago = request.data.get('payment_method', 'CASH')
        
        # Creamos la venta en estado borrador o completada según el frontend
        venta = Venta.objects.create(
            seller=request.user if request.user.is_authenticated else None,
            total=total_monto,
            payment_method=metodo_pago,
            status='DRAFT' # O el estado que maneje tu interfaz
        )
        
        return Response(detalle_venta(venta, request.user), status=201)