# backendXYZpos

Backend desarrollado en Django para el sistema de punto de venta (POS). Utiliza PostgreSQL (Neon) como base de datos en la nube compartida para todo el equipo de trabajo.

## Prerrequisitos

- Python 3.10 o superior instalado.
- Git instalado.

---

## Guía de Instalación Rápida para el Equipo

Sigue estos pasos en tu terminal para clonar y poner en marcha el proyecto en tu computadora:

### 1. Clonar el repositorio

```bash
git clone <URL_DEL_REPOSITORIO>
cd backendXYZpos
```

2. Crear y activar el entorno virtual
   En Windows (CMD / PowerShell):

```bash
python -m venv venv
venv\Scripts\activate
```

3. Instalar las dependencias

```bash
pip install django psycopg2-binary pandas openpyxl django-import-export
```

4. Configuración de la Base de Datos
   El proyecto utiliza una base de datos centralizada en la nube. Verifica que tu archivo backendXYZpos/backendXYZpos/settings.py tenga el siguiente bloque de conexión con las credenciales compartidas:

```py
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'neondb',
        'USER': 'tu_usuario_tecnico',
        'PASSWORD': 'tu_contraseña',
        'HOST': 'ep-blue-scene-ayo2gq73-pooler.c-5.us-east-2.aws.neon.tech',
        'PORT': '5432',
        'OPTIONS': {
            'sslmode': 'require',
        },
    }
}
```

5. Sincronizar las migraciones
   Ejecuta el siguiente comando para conectar con la base de datos compartida:

```bash
python manage.py migrate
```

6. Crear tu propio superusuario (Opcional)
   Para poder entrar al panel de administración de Django con tus propias credenciales:

```bash
python manage.py createsuperuser
```

7. Ejecutar el servidor de desarrollo

```bash
python manage.py runserver
```

Abre tu navegador y entra a:

Home del Backend: http://127.0.0.1:8000/

Panel de Administración: http://127.0.0.1:8000/admin/

```

```
