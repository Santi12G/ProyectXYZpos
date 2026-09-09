"""Reportes por fecha del evento: cobros al completar, devoluciones al procesar."""
from decimal import Decimal

from .models import Reembolso, Venta


CERO = Decimal('0.00')
ESTADOS_VENDIDOS = (Venta.Estado.COMPLETED, Venta.Estado.PARTIALLY_REFUNDED, Venta.Estado.REFUNDED)


def nueva_fila():
    return dict(ventas_brutas=CERO, reembolsos=CERO, ingresos_netos=CERO,
                ingresos_sin_impuesto=CERO, costo_conocido=CERO, utilidad_bruta=None,
                transacciones=0, unidades_vendidas=0, unidades_reembolsadas=0,
                unidades_sin_costo=0, ventas_legacy=0)


def resumen_periodo(inicio, fin):
    resumen, vendedores, fechas, productos = nueva_fila(), {}, {}, {}

    def filas(seller_id, username, dia):
        vendedor = vendedores.setdefault(seller_id, dict(seller_id=seller_id, vendedor=username, **nueva_fila()))
        fecha = fechas.setdefault(str(dia), dict(dia=str(dia), **nueva_fila()))
        return resumen, vendedor, fecha

    ventas = Venta.objects.filter(status__in=ESTADOS_VENDIDOS, completed_at__date__range=(inicio, fin)).select_related('seller').prefetch_related('items')
    from django.utils import timezone
    for venta in ventas:
        grupos = filas(venta.seller_id, venta.seller.username, timezone.localdate(venta.completed_at))
        for fila in grupos:
            fila['ventas_brutas'] += venta.total
            fila['ingresos_sin_impuesto'] += venta.total - venta.tax
            fila['transacciones'] += 1
            fila['ventas_legacy'] += int(venta.legacy)
        for item in venta.items.all():
            producto = productos.setdefault(item.product_id, dict(product_id=item.product_id, nombre=item.product_name, unidades=0))
            producto['unidades'] += item.quantity
            for fila in grupos:
                fila['unidades_vendidas'] += item.quantity
                if item.unit_cost is None:
                    fila['unidades_sin_costo'] += item.quantity
                else:
                    fila['costo_conocido'] += item.unit_cost * item.quantity

    devoluciones = Reembolso.objects.filter(status=Reembolso.Estado.COMPLETED, completed_at__date__range=(inicio, fin)).select_related('sale__seller').prefetch_related('items__sale_item')
    for reembolso in devoluciones:
        venta = reembolso.sale
        grupos = filas(venta.seller_id, venta.seller.username, timezone.localdate(reembolso.completed_at))
        # La base sin impuestos se calcula por acumulados, igual que el importe.
        for fila in grupos:
            fila['reembolsos'] += reembolso.amount
        for item in reembolso.items.all():
            original = item.sale_item
            for fila in grupos:
                fila['unidades_reembolsadas'] += item.quantity
                fila['ingresos_sin_impuesto'] -= item.base_amount
                if item.restore_stock:
                    if original.unit_cost is None:
                        # Una devolución de costo desconocido también vuelve incierta la utilidad.
                        fila['unidades_sin_costo'] += item.quantity
                    else:
                        fila['costo_conocido'] -= original.unit_cost * item.quantity

    for fila in [resumen, *vendedores.values(), *fechas.values()]:
        fila['ingresos_netos'] = fila['ventas_brutas'] - fila['reembolsos']
        if not fila['unidades_sin_costo'] and not fila['ventas_legacy']:
            fila['utilidad_bruta'] = fila['ingresos_sin_impuesto'] - fila['costo_conocido']
    return dict(inicio=str(inicio), fin=str(fin), resumen=resumen,
                por_vendedor=list(vendedores.values()), por_fecha=sorted(fechas.values(), key=lambda f: f['dia']),
                productos_mas_vendidos=sorted(productos.values(), key=lambda p: (-p['unidades'], p['product_id']))[:10])


def montos_json(valor):
    if isinstance(valor, Decimal):
        return format(valor, '.2f')
    if isinstance(valor, dict):
        return {k: montos_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [montos_json(v) for v in valor]
    return valor
