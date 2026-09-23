from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


# 1. Función para averiguar el rol del usuario
def rol_usuario(user):
    # Si no existe, no está logueado o está inactivo, fuera
    if not user or not user.is_authenticated or not user.is_active:
        return None
    
    # Si es superusuario de Django, es ADMIN de una vez
    if user.is_superuser:
        return 'ADMIN'
    
    # Buscamos su perfil
    perfil = getattr(user, 'perfil_pos', None)
    
    # Si no tiene perfil, le damos el rol de vendedor por defecto (privilegio mínimo)
    if perfil is None:
        return 'SELLER' 
        
    return perfil.role if perfil.active else None


# 2. Saber si es admin rápidamente
def es_admin(user):
    return rol_usuario(user) == 'ADMIN'


# 3. Obligar a que el usuario sea parte del POS (Admin o Vendedor)
def exigir_usuario(user):
    rol = rol_usuario(user)
    if rol != 'ADMIN' and rol != 'SELLER':
        raise PermissionDenied('No tienes acceso al POS.')


# 4. Obligar a que sea administrador sí o sí
def exigir_admin(user):
    if not es_admin(user):
        raise PermissionDenied('Esta operación requiere un administrador.')


# 5. Verificar que la venta sea del vendedor actual o de un administrador
def exigir_venta(user, venta):
    exigir_usuario(user)
    if not es_admin(user) and user.pk != venta.seller_id:
        raise PermissionDenied('Solo puedes operar tus propias ventas.')


# 6. Decorador para proteger páginas web normales en Django
def admin_requerido(vista):
    @login_required(login_url='home')
    @wraps(vista)
    def protegida(request, *args, **kwargs):
        exigir_admin(request.user)
        return vista(request, *args, **kwargs)
    return protegida


# 7. Permisos para la API (Django Rest Framework)
class EsAdministrador(BasePermission):
    def has_permission(self, request, view):
        return es_admin(request.user)


class EsUsuarioPOS(BasePermission):
    def has_permission(self, request, view):
        rol = rol_usuario(request.user)
        return rol == 'ADMIN' or rol == 'SELLER'