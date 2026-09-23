from .settings import *  # noqa: F403

SECRET_KEY = 'solo-pruebas-locales-no-utilizar-en-produccion'
DEBUG = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
