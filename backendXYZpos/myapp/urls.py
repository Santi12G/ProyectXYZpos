from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (DashboardAnalyticsViewSet, DashboardTemplateView,
                    inventario, producto_crear, producto_editar, ventas_ui, cerrar_sesion)
from .sales_api import VentaViewSet
 
router = DefaultRouter()
router.register(r'ventas', VentaViewSet, basename='ventas')
router.register(
    r'analytics', DashboardAnalyticsViewSet, basename='dashboard-analytics'
)

urlpatterns = [
    path('', include(router.urls)),
    path(
        'dashboard-ui/', DashboardTemplateView.as_view(), name='dashboard_ui'
    ),
    path('inventario/', inventario, name='inventario'),
    path('inventario/nuevo/', producto_crear, name='producto_crear'),
    path('inventario/<int:pk>/editar/', producto_editar, name='producto_editar'),
    path('ventas-ui/', ventas_ui, name='ventas_ui'),
    path('logout/', cerrar_sesion, name='logout'),
]
