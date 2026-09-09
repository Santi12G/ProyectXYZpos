from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework.decorators import action
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.contrib.auth import logout
from django.contrib.auth.password_validation import validate_password
from django.views.decorators.http import require_POST, require_http_methods
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet
from .forms import ProductoForm
from .models import PerfilUsuario, Producto, Venta
from .permissions import EsAdministrador, admin_requerido, es_admin, exigir_usuario, rol_usuario
from .reports import ESTADOS_VENDIDOS, montos_json, resumen_periodo

from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View


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
      grafico.append({'dia': fecha, 'ingreso': fila.get('ingresos_netos', Decimal('0')), 'cantidad': fila.get('transacciones', 0)})
    ingresos = mes['resumen']['ingresos_netos']
    meta = max(Decimal('0'), min((ingresos / Decimal('5000000') * 100).quantize(Decimal('.1')), Decimal('100')))
    return Response(montos_json({
        'status': 'success',
        'criterio': 'Eventos por completed_at: cobros y reembolsos en su fecha; ingresos netos incluyen impuestos. Utilidad sin impuestos; solo revierte costo si se restaura stock.',
        'cards_summary': {
            'ingresos_mes': ingresos,
            'transacciones_mes': mes['resumen']['transacciones'],
            'meta_progreso': meta,
        },
        'diario': dia, 'mensual': mes,
        'charts': {'line_chart_semana': grafico},
        'recent_activity': [
            {
                'id': v.id,
                'total': v.total,
                'fecha': v.created_at,
            }
            for v in Venta.objects.filter(status__in=ESTADOS_VENDIDOS).order_by('-completed_at')[:5]
        ],
    }))


@method_decorator(admin_requerido, name='dispatch')
class DashboardTemplateView(TemplateView):
  template_name = 'myapp/dashboard.html' 


@admin_requerido
def inventario(request):
  productos = Producto.objects.select_related('categoria').all()
  return render(request, 'myapp/inventario.html', {'productos': productos})


@login_required(login_url='home')
def ventas_ui(request):
  exigir_usuario(request.user)
  return render(request, 'myapp/ventas.html', {
      'productos': Producto.objects.filter(disponible=True).only('pk', 'sku', 'nombre', 'precio', 'stock'),
      'es_admin': es_admin(request.user),
  })


@admin_requerido
@require_http_methods(['GET', 'POST'])
def producto_crear(request):
  form = ProductoForm(request.POST if request.method == 'POST' else None)
  if request.method == 'POST' and form.is_valid():
    try:
      producto = form.save(user=request.user)
      if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'id': producto.pk, 'url': '/api/inventario/'})
      return redirect('inventario')
    except ValidationError as exc:
      form.add_error(None, exc)
    except IntegrityError:
      form.add_error(None, 'Otro producto utiliza este código. Revisa los datos.')
  return render(request, 'myapp/producto_form.html', {'form': form, 'titulo': 'Crear producto'})


@admin_requerido
@require_http_methods(['GET', 'POST'])
def producto_editar(request, pk):
  producto = get_object_or_404(Producto, pk=pk)
  form = ProductoForm(request.POST if request.method == 'POST' else None, instance=producto)
  if request.method == 'POST' and form.is_valid():
    try:
      producto = form.save(user=request.user)
      if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'id': producto.pk, 'url': '/api/inventario/'})
      return redirect('inventario')
    except ValidationError as exc:
      form.add_error(None, exc)
    except IntegrityError:
      form.add_error(None, 'Otro producto utiliza este código. Revisa los datos.')
  return render(request, 'myapp/producto_form.html', {'form': form, 'titulo': 'Editar producto'})
  
@login_required(login_url='home')
@require_POST
def cerrar_sesion(request):
  logout(request)
  return redirect('home')


class HomeView(View):

  def get(self, request):
    return render(request, 'myapp/home.html')  # O el nombre de tu template home

  def post(self, request):
    action = request.POST.get('action')

    # Lógica para Crear Usuario Nuevo
    if action == 'register':
      username = request.POST.get('reg_username')
      password = request.POST.get('reg_password')
      if username and password:
        if not User.objects.filter(username=username).exists():
          try:
            user = User(username=username)
            user.full_clean(exclude=['password'])
            validate_password(password, user)
            with transaction.atomic():
              user.set_password(password)
              user.save()
              PerfilUsuario.objects.create(user=user, role=PerfilUsuario.Rol.SELLER)
          except (ValidationError, IntegrityError):
            return render(request, 'myapp/home.html', {'error_reg': 'Revisa el usuario y usa una contraseña segura de al menos 8 caracteres.'})
          login(request, user)
          return redirect(
              'ventas_ui'
          )  # Redirige al dashboard tras crear cuenta
        else:
          return render(
              request,
              'myapp/home.html',
              {'error_reg': 'El usuario ya existe.'},
          )

    # Lógica para Iniciar Sesión
    elif action == 'login':
      username = request.POST.get('log_username')
      password = request.POST.get('log_password')
      user = authenticate(request, username=username, password=password)
      if user is not None and rol_usuario(user):
        login(request, user)
        return redirect('dashboard_ui' if es_admin(user) else 'ventas_ui')
      else:
        return render(
            request,
            'myapp/home.html',
            {'error_log': 'Credenciales incorrectas.'},
        )

    return render(request, 'myapp/home.html')
