from datetime import datetime, timedelta
from django.db.models import Count, Sum
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet
from .models import Venta

from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.views import View


class DashboardAnalyticsViewSet(ViewSet):

  @action(detail=False, methods=['get'])
  def metrics(self, request):
    hoy = timezone.now().date()
    inicio_mes = hoy.replace(day=1)

    total_ingresos_mes = (
        Venta.objects.filter(created_at__date__gte=inicio_mes).aggregate(
            Sum('total')
        )['total__sum']
        or 0
    )

    total_transacciones = Venta.objects.filter(
        created_at__date__gte=inicio_mes
    ).count()

    dias_atras = hoy - timedelta(days=7)
    ventas_recientes = (
        Venta.objects.filter(created_at__date__gte=dias_atras)
        .extra(select={'dia': 'date(created_at)'})
        .values('dia')
        .annotate(ingreso=Sum('total'), cantidad=Count('id'))
        .order_by('dia')
    )

    meta_mensual = 5000000
    porcentaje_meta = (
        min(round((float(total_ingresos_mes) / meta_mensual) * 100, 1), 100)
        if meta_mensual > 0
        else 0
    )

    return Response({
        'status': 'success',
        'cards_summary': {
            'ingresos_mes': float(total_ingresos_mes),
            'transacciones_mes': total_transacciones,
            'meta_progreso': porcentaje_meta,
        },
        'charts': {
            'line_chart_semana': list(ventas_recientes),
        },
        'recent_activity': [
            {
                'id': v.id,
                'total': float(v.total),
                'fecha': v.created_at,
            }
            for v in Venta.objects.order_by('-created_at')[:5]
        ],
    })


class DashboardTemplateView(TemplateView):
  template_name = 'myapp/dashboard.html' 
  
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
          user = User.objects.create_user(username=username, password=password)
          login(request, user)
          return redirect(
              'dashboard_ui'
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
      if user is not None:
        login(request, user)
        return redirect('dashboard_ui')  # Redirige al dashboard tras loguearse
      else:
        return render(
            request,
            'myapp/home.html',
            {'error_log': 'Credenciales incorrectas.'},
        )

    return render(request, 'myapp/home.html')