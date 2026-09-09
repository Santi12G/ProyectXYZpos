from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import DashboardAnalyticsViewSet, DashboardTemplateView
 
router = DefaultRouter()
router.register(
    r'analytics', DashboardAnalyticsViewSet, basename='dashboard-analytics'
)

urlpatterns = [
    path('', include(router.urls)),
    path(
        'dashboard-ui/', DashboardTemplateView.as_view(), name='dashboard_ui'
    ),
]