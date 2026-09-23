from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from .forms import ProductoForm
from .models import PerfilUsuario, Producto, Venta
from .permissions import EsAdministrador, admin_requerido, es_admin, exigir_usuario, rol_usuario
from .reports import ESTADOS_VENDIDOS, montos_json, resumen_periodo



# 1. API para las métricas del Dashboard (Se mantiene igual de funcional)
class DashboardAnalyticsViewSet(ViewSet):
    permission_classes = (EsAdministrador,)

    @action(detail=False, methods=['get'])
    def metrics(self, request):
        hoy = timezone.localdate()
        mes = resumen_periodo(hoy.replace(day=1), hoy)
        dia = resumen_periodo(hoy, hoy)
        semana = resumen_periodo(hoy - timedelta(days=6), hoy)
        
        por_dia = {fila['dia']: fila for fila in semana['por_fecha']}
        grafico = []
        for i in range(7):
            fecha = str(hoy - timedelta(days=6 - i))
            fila = por_dia.get(fecha, {})
            grafico.append({
                'dia': fecha, 
                'ingreso': fila.get('ingresos_netos', Decimal('0')), 
                'cantidad': fila.get('transacciones', 0)
            })
            
        ingresos = mes['resumen']['ingresos_netos']
        meta = max(Decimal('0'), min((ingresos / Decimal('5000000') * 100).quantize(Decimal('.1')), Decimal('100')))
        
        # AQUÍ ESTABA EL ERROR: Cambiamos -completed_at por -created_at
        recent_activity = [
            {'id': v.id, 'total': v.total, 'fecha': v.created_at}
            for v in Venta.objects.filter(status__in=ESTADOS_VENDIDOS).order_by('-created_at')[:5]
        ]

        return Response(montos_json({
            'status': 'success',
            'criterio': 'Eventos por created_at',
            'cards_summary': {'ingresos_mes': ingresos, 'transacciones_mes': mes['resumen']['transacciones'], 'meta_progreso': meta},
            'diario': dia, 
            'mensual': mes,
            'charts': {'line_chart_semana': grafico},
            'recent_activity': recent_activity,
        }))

# 2. Vistas normales protegidas para el Administrador
@method_decorator(admin_requerido, name='dispatch')
class DashboardTemplateView(TemplateView):
    template_name = 'myapp/dashboard.html'


@admin_requerido
def inventario(request):
    productos = Producto.objects.select_related('categoria').all()
    return render(request, 'myapp/inventario.html', {'productos': productos})


# 3. Vista de Ventas para usuarios normales
@login_required(login_url='home')
def ventas_ui(request):
    exigir_usuario(request.user)
    return render(request, 'myapp/ventas.html', {
        'productos': Producto.objects.filter(disponible=True).only('pk', 'sku', 'nombre', 'precio', 'stock'),
        'es_admin': es_admin(request.user),
    })


# 4. Crear y Editar Productos (juntamos la lógica repetitiva mentalmente)
@admin_requerido
def producto_crear(request):
    return _guardar_producto(request, pk=None)


@admin_requerido
def producto_editar(request, pk):
    return _guardar_producto(request, pk=pk)


def _guardar_producto(request, pk):
    producto = get_object_or_404(Producto, pk=pk) if pk else None
    form = ProductoForm(request.POST if request.method == 'POST' else None, instance=producto)
    
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                prod = form.save(user=request.user)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'id': prod.pk, 'url': '/api/inventario/'})
            return redirect('inventario')
        except ValidationError as exc:
            form.add_error(None, exc)
        except IntegrityError as exc:
            # Solo una colisión real de SKU debe presentarse como duplicado.
            causa = exc.__cause__
            diagnostico = getattr(causa, 'diag', None)
            if (getattr(causa, 'pgcode', None) == '23505'
                    and getattr(diagnostico, 'constraint_name', None) == 'myapp_producto_sku_key'):
                form.add_error('sku', 'Ya existe un producto con ese SKU.')
            else:
                raise
            
    titulo = 'Editar producto' if pk else 'Crear producto'
    return render(request, 'myapp/producto_form.html', {'form': form, 'titulo': titulo})


# 5. Cerrar sesión
@login_required(login_url='home')
def cerrar_sesion(request):
    logout(request)
    return redirect('home')


# 6. Vista de Inicio (Registro y Login juntos)
class HomeView(View):
    def get(self, request):
        return render(request, 'myapp/home.html')

    def post(self, request):
        action = request.POST.get('action')

        # Registrarse
        if action == 'register':
            username = request.POST.get('reg_username')
            password = request.POST.get('reg_password')
            
            if not username or not password:
                return render(request, 'myapp/home.html', {'error_reg': 'Faltan datos.'})
            
            if User.objects.filter(username=username).exists():
                return render(request, 'myapp/home.html', {'error_reg': 'El usuario ya existe.'})

            try:
                user = User(username=username)
                user.full_clean(exclude=['password'])
                validate_password(password, user)
                
                with transaction.atomic():
                    user.set_password(password)
                    user.save()
                    PerfilUsuario.objects.create(user=user, role='SELLER')
                
                login(request, user)
                return redirect('ventas_ui')
                
            except ValidationError as e:
                # Esto capturará el error exacto (ej. contraseña muy común) y lo mostrará en pantalla
                errores = ', '.join(e.messages) if hasattr(e, 'messages') else str(e)
                print("Error de validación:", errores) # También lo imprime en tu terminal
                return render(request, 'myapp/home.html', {'error_reg': f'Error: {errores}'})
                
            except IntegrityError as e:
                print("Error de base de datos:", e)
                return render(request, 'myapp/home.html', {'error_reg': 'Error de integridad en la base de datos.'})
        # Iniciar sesión
        elif action == 'login':
            user = authenticate(request, username=request.POST.get('log_username'), password=request.POST.get('log_password'))
            if user is not None and rol_usuario(user):
                login(request, user)
                return redirect('dashboard_ui' if es_admin(user) else 'ventas_ui')
            
            return render(request, 'myapp/home.html', {'error_log': 'Credenciales incorrectas.'})

        return render(request, 'myapp/home.html')
