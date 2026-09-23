"""Reportes sencillos que solo consultan la tabla Venta para evitar errores de columnas."""
from decimal import Decimal
from django.utils import timezone
from .models import Reembolso, Venta


CERO = Decimal('0.00')
ESTADOS_VENDIDOS = ('COMPLETED', 'PARTIALLY_REFUNDED', 'REFUNDED')


def nueva_fila():
    return {
        'ventas_brutas': CERO, 
        'reembolsos': CERO, 
        'ingresos_netos': CERO,
        'transacciones': 0, 
        'unidades_vendidas': 0
    }


def resumen_periodo(inicio, fin):
    resumen = nueva_fila()
    vendedores = {}
    fechas = {}

    def filas(seller_id, username, dia):
        vendedor = vendedores.setdefault(seller_id, {'seller_id': seller_id, 'vendedor': username, **nueva_fila()})
        fecha = fechas.setdefault(str(dia), {'dia': str(dia), **nueva_fila()})
        return resumen, vendedor, fecha

    # Consultamos únicamente las ventas (sin tocar ItemVenta para evitar errores de base de datos)
    ventas = Venta.objects.filter(status__in=ESTADOS_VENDIDOS, created_at__date__range=(inicio, fin)).select_related('seller')
    
    for venta in ventas:
        username = venta.seller.username if venta.seller else 'Sin vendedor'
        dia = timezone.localdate(venta.created_at)
        grupos = filas(venta.seller_id, username, dia)
        
        for fila in grupos:
            fila['ventas_brutas'] += venta.total
            fila['transacciones'] += 1

    # Procesamos reembolsos si los hay
    devoluciones = Reembolso.objects.filter(status='COMPLETED', created_at__date__range=(inicio, fin)).select_related('sale__seller')
    
    for reembolso in devoluciones:
        venta = reembolso.sale
        if venta and venta.seller:
            username = venta.seller.username
            dia = timezone.localdate(reembolso.created_at)
            grupos = filas(venta.seller_id, username, dia)
            for fila in grupos:
                fila['reembolsos'] += reembolso.amount

    # Calculamos ingresos netos finales
    for fila in [resumen, *vendedores.values(), *fechas.values()]:
        fila['ingresos_netos'] = fila['ventas_brutas'] - fila['reembolsos']

    return {
        'inicio': str(inicio), 
        'fin': str(fin), 
        'resumen': resumen,
        'por_vendedor': list(vendedores.values()), 
        'por_fecha': sorted(fechas.values(), key=lambda f: f['dia']),
        'productos_mas_vendidos': []
    }


def montos_json(valor):
    if isinstance(valor, Decimal):
        return format(valor, '.2f')
    if isinstance(valor, dict):
        return {k: montos_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [montos_json(v) for v in valor]
    return valor