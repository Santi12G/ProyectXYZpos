from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


def rol_usuario(user):
    if not user or not user.is_authenticated or not user.is_active:
        return None
    if user.is_superuser:
        return 'ADMIN'
    perfil = getattr(user, 'perfil_pos', None)
    if perfil is None:
        return 'SELLER'  # Usuarios legacy sin perfil: mínimo privilegio.
    return perfil.role if perfil.active else None


def es_admin(user):
    return rol_usuario(user) == 'ADMIN'


def exigir_usuario(user):
    if rol_usuario(user) not in ('ADMIN', 'SELLER'):
        raise PermissionDenied('No tienes acceso al POS.')


def exigir_admin(user):
    if not es_admin(user):
        raise PermissionDenied('Esta operación requiere un administrador.')


def exigir_venta(user, venta):
    exigir_usuario(user)
    if not es_admin(user) and user.pk != venta.seller_id:
        raise PermissionDenied('Solo puedes operar tus propias ventas.')


def admin_requerido(vista):
    @login_required(login_url='home')
    @wraps(vista)
    def protegida(request, *args, **kwargs):
        exigir_admin(request.user)
        return vista(request, *args, **kwargs)
    return protegida


class EsAdministrador(BasePermission):
    def has_permission(self, request, view):
        return es_admin(request.user)


class EsUsuarioPOS(BasePermission):
    def has_permission(self, request, view):
        return rol_usuario(request.user) in ('ADMIN', 'SELLER')
