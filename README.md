# backendXYZpos

Mini-POS con Django, Django REST Framework y plantillas Django/Tailwind/Chart.js. El despliegue existente utiliza PostgreSQL en Neon. Las verificaciones de esta auditoría utilizan exclusivamente SQLite en memoria, sin conectarse a Neon.

## Preparación

Python 3.12 o superior (requisito de la versión de Django instalada). Desde la raíz de ProyectXYZpos:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cd backendXYZpos
```

La configuración se obtiene del entorno; no se carga ningún archivo .env automáticamente. Ver .env.example. La antigua configuración local se conserva en myapp/.env.local, ignorada por Git. Sus credenciales deben rotarse porque estuvieron versionadas.

Para arrancar una instancia de desarrollo, configura una clave nueva y una base PostgreSQL **local y exclusiva**. Ejemplo de variables (sustituye los valores):

```powershell
$env:DJANGO_SECRET_KEY='clave-nueva-aleatoria'
$env:DB_NAME='xyzpos_local'
$env:DB_USER='usuario_local'
$env:DB_PASSWORD='contrasena_local'
$env:DB_HOST='localhost'
$env:DB_PORT='5432'
$env:DB_SSLMODE='prefer'
```

Solo cuando esa base local exista y hayas comprobado el destino, puedes ejecutar migrate, createsuperuser y runserver. No aplicar migraciones sobre Neon como parte de la auditoría.

## Verificación aislada

Desde la carpeta que contiene manage.py:

```powershell
python manage.py check --settings=backendXYZpos.test_settings
python manage.py makemigrations --check --dry-run --settings=backendXYZpos.test_settings
python manage.py test myapp --settings=backendXYZpos.test_settings -v 2
```

La suite crea y elimina su base temporal. test_settings es exclusivamente para pruebas: emplea un hash rápido y una clave pública de pruebas. No sirve para desplegar el POS ni para mantener datos entre comandos.

Las pruebas de concurrencia se omiten en SQLite. Para verificarlas hace falta un PostgreSQL local aislado y configurar un módulo de pruebas que apunte únicamente a ese servidor.

## Usuarios y permisos

Django conserva nombre, username, contraseña cifrada e is_active. PerfilUsuario agrega ADMIN/SELLER y su propio indicador de acceso. Un superusuario Django activo es administrador. Un usuario sin perfil recibe acceso de vendedor; is_staff por sí solo no lo convierte en administrador.

El administrador puede modificar inventario, consultar métricas y procesar reembolsos. El vendedor consulta y opera exclusivamente sus ventas. Para entrar al Django Admin se necesitan rol administrador e is_staff; los perfiles se gestionan por superusuarios. El registro público existente crea únicamente vendedores y aplica validación de contraseñas.

## Rutas y operaciones

| Ruta | Uso |
|---|---|
| / | Inicio, registro e inicio de sesión |
| /api/dashboard-ui/ | Reportes administrativos |
| /api/analytics/metrics/ | Métricas, únicamente ADMIN |
| /api/inventario/ | Tabla, búsqueda local y formularios en diálogo |
| /api/inventario/nuevo/ | Alta de producto, también funciona sin JavaScript |
| /api/inventario/ID/editar/ | Edición con comprobación de stock desactualizado |
| /api/ventas-ui/ | Ventas y recuperación de borradores |
| /api/ventas/ | GET listado / POST borrador |
| /api/ventas/ID/ | GET detalle / PUT reemplazo completo del borrador |
| /api/ventas/ID/completar/ | POST confirmación transaccional |
| /api/ventas/ID/cancelar/ | POST cancelación de borrador |
| /api/ventas/ID/reembolsar/ | POST devolución administrativa |
| /api/logout/ | POST cierre de sesión con CSRF |

Todas las escrituras web requieren sesión y CSRF. Los importes JSON son cadenas decimales. No enviar total, nombre snapshot, costo o precio unitario: el servidor los obtiene de los productos. El precio del borrador se vuelve a consultar al confirmar.

Ejemplo de borrador:

```json
{"items":[{"product":1,"quantity":2}],"payment_method":"CASH","amount_received":"25.00"}
```

Los campos tax y discount por línea son importes manuales no negativos reservados a ADMIN; no se configuró un motor fiscal. No son porcentajes.

Ejemplo de devolución:

```json
{"reason":"Devolución de producto","items":[{"sale_item":1,"quantity":1,"restore_stock":true}]}
```

## Prueba funcional en una instancia local

1. Iniciar sesión como administrador y crear un producto de precio 10.00, costo 6.00, stock 5 y mínimo 3.
2. Entrar a Ventas como vendedor, añadir dos unidades y guardar el borrador. El stock sigue en 5.
3. Recuperar y confirmar el borrador. Total 20.00, stock 3, movimiento SALE de -2 y alerta de stock bajo.
4. Como administrador, reembolsar una unidad mediante la API o Django Admin. Importe calculado 10.00; stock 4 si restore_stock=true, movimiento REFUND de +1 y estado PARTIALLY_REFUNDED.
5. Intentar confirmar o procesar el mismo registro de nuevo: debe rechazarse sin alterar inventario.

Los cambios de stock deben pasar por Producto.save(user=administrador) o reabastecer(cantidad, user, reason). Los imports conservan el actor y los movimientos. Los métodos update/bulk_update/bulk_create se rechazan para los modelos protegidos. Los productos se dan de baja mediante desactivación, incluso en operaciones masivas.

## Reportes y migraciones

Las ventas confirmadas, parcialmente reembolsadas y reembolsadas cuentan como transacciones. Los cobros se contabilizan en completed_at de la venta y las devoluciones en completed_at del reembolso. Se exponen bruto, devoluciones y neto; la utilidad excluye impuestos y solo revierte costo cuando se restaura stock. Sin costo histórico completo, utilidad_bruta es null. Productos más vendidos significa unidades brutas vendidas del período.

0004 amplía el esquema y detecta datos negativos previos; 0005 asigna identificadores sin colisiones, conserva ventas históricas como legacy y crea saldos iniciales de inventario; 0006 y 0007 agregan restricciones y protecciones adicionales. Las ventas legacy no tienen detalle reconstruible: cuentan en ingresos, pero no permiten reembolsos operativos ni cálculos completos de utilidad. No se inventan productos vendidos.

Si una instalación intermedia ya contiene reembolsos completados con la implementación anterior, 0006 se detiene para solicitar conciliación de importes, bases y fechas. No modifica importes históricos automáticamente.

Ver [AUDITORIA_TECNICA.md](AUDITORIA_TECNICA.md) para resultados, límites y pendientes.
