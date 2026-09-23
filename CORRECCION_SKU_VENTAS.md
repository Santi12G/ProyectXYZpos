# Corrección de inventario y ventas

## Diagnóstico y causa raíz

La inspección de PostgreSQL se ejecutó en una transacción de **solo lectura**.
No se insertaron datos ni se ejecutaron migraciones contra Neon.

### SKU

Recorrido: `/api/inventario/` → enlace a `/api/inventario/nuevo/` →
`producto_crear()` → `_guardar_producto()` → `ProductoForm` → `Producto`.
El diálogo de inventario envía el formulario mediante `FormData`.

La validación de unicidad de `ModelForm` funcionaba: en una reproducción aislada
aceptaba un SKU nuevo y rechazaba el mismo SKU con espacios alrededor. PostgreSQL
tenía dos productos, dos SKUs distintos y ningún SKU vacío.

El modelo simplificado había perdido columnas que PostgreSQL todavía exige sin
default SQL: `fecha_creacion`, `description`, `image_url`, `min_stock` y
`updated_at`. El INSERT omitía esas columnas. La vista atrapaba **cualquier**
`IntegrityError` y lo presentaba como un código duplicado. Una reproducción local
del esquema obligatorio falló con `NOT NULL constraint failed:
myapp_producto.fecha_creacion`, aunque el formulario era válido.

Se recuperó el esquema original y el guardado de producto. Se mantiene
`sku.unique=True`, la normalización de espacios y la restricción de SKU no vacío.
Solo una colisión PostgreSQL `23505` contra `myapp_producto_sku_key` se transforma
en un error de SKU durante el guardado. Otras excepciones de integridad se
propagan para conservar el diagnóstico real.

### Ventas

Recorrido: `/api/ventas-ui/` → `ventas_ui()` → `ventas.html` →
`/api/ventas/` y sus acciones → `VentaViewSet` → `Venta` / `ItemVenta`.

Había varios cortes del mismo flujo:

- La API creaba `Venta` con `amount_received`, `discount`, `subtotal` y `tax`,
  campos eliminados del modelo. Esto producía `TypeError` antes del INSERT.
- Faltaban otros campos obligatorios y la generación de comprobantes únicos.
- `ItemVenta` usaba `venta_id`, aunque la columna desplegada es `sale_id`, y
  no conservaba `product_id`, `quantity` ni `discount`.
- El detalle devolvía siempre `items: []`, impidiendo recuperar el borrador.
- La interfaz llamaba PUT y las acciones `completar` y `cancelar`, pero esas
  operaciones habían desaparecido del viewset y del modelo.
- Un `except Exception` convertía errores de programación en errores de entrada.

Ahora se guarda una venta `DRAFT` con sus líneas sin descontar stock. Al confirmar,
una transacción bloquea la venta y los productos, consulta de nuevo precio y
stock, valida cantidades, descuentos y pago, calcula los importes en el backend,
registra movimientos y deja la venta `COMPLETED` con su fecha. Un fallo revierte
todos esos cambios; una segunda confirmación se rechaza. La edición y cancelación
solo operan sobre borradores accesibles al usuario.

## Archivos

| Archivo | Cambio |
|---|---|
| `backendXYZpos/myapp/models.py` | Recupera el esquema desplegado, las restricciones y las operaciones de producto, detalle, inventario y venta. |
| `backendXYZpos/myapp/forms.py` | Conserva los campos visibles; pasa el actor y el stock original al guardado y muestra el error específico de SKU. |
| `backendXYZpos/myapp/views.py` | Guarda atómicamente y distingue una colisión real de SKU de otros errores. |
| `backendXYZpos/myapp/admin.py` | Pasa el administrador al guardar productos para conservar la validación y auditoría del stock. |
| `backendXYZpos/myapp/sales_api.py` | Recupera detalle, creación, edición, confirmación y cancelación; conserva permisos y validaciones. |
| `backendXYZpos/myapp/templates/myapp/ventas.html` | Distingue guardado de borrador de confirmación, conserva descuentos/impuesto al recuperar y limpia un borrador cerrado del editor. |
| `backendXYZpos/myapp/migrations/0004_*.py` a `0007_*.py` | Restaura exactamente los archivos de Git que ya constan aplicados en PostgreSQL. No son migraciones nuevas. |
| `backendXYZpos/backendXYZpos/test_settings.py` | Recupera la configuración de pruebas con SQLite en memoria. |
| `backendXYZpos/myapp/tests.py` | Añade 30 pruebas de regresión de los dos flujos. |

La referencia recuperada es el commit `06c15b2`. Las declaraciones relacionadas
con categorías, perfiles, alertas y reembolsos vuelven a coincidir con el esquema
existente para mantener un historial de migraciones coherente. No se incorpora
un endpoint de reembolsos ni se modifica la lógica de reportes.

## Verificación

Desde `backendXYZpos`:

```powershell
python manage.py check --settings=backendXYZpos.test_settings
python manage.py makemigrations --check --dry-run --settings=backendXYZpos.test_settings
python manage.py test myapp --settings=backendXYZpos.test_settings -v 2
```

Resultados: sin problemas de configuración, sin diferencias de migraciones y
30 pruebas aprobadas. Se aplicó el historial real de migraciones a la base temporal,
sin deshabilitar migraciones para las pruebas. Se comprobó también la sintaxis
JavaScript de ambas pantallas mediante `node --check`.

Las pruebas cubren SKU nuevo y duplicado, conservación de productos al editar,
unicidad en la base, stock desactualizado, sesión y CSRF, recuperación y edición,
confirmación, precio actualizado, stock insuficiente, producto desactivado, pago
insuficiente, rollback, doble confirmación, cancelación y permisos.

## Límites y pendientes

- Las escrituras se verificaron con SQLite local; no se verificaron carreras
  simultáneas ni bloqueos reales en PostgreSQL. Eso requiere una base PostgreSQL
  local aislada.
- Se probaron las páginas y sus peticiones mediante el cliente de integración
  de Django; no se automatizó un navegador.
- La configuración principal sigue apuntando a Neon. Usar expresamente
  `--settings=backendXYZpos.test_settings` para repetir las pruebas aisladas.
- No hace falta aplicar migraciones nuevas en la base inspeccionada: ya registra
  `0001` a `0007`. No se cambió ningún producto existente en ella.
