# 1. Define Project Name
$ProjectName = "anonymous_connect"
$AppName = "chat"

# 2. Create Directory Structure
New-Item -ItemType Directory -Path "$ProjectName/$ProjectName" -Force
New-Item -ItemType Directory -Path "$ProjectName/$AppName/templates/$AppName" -Force
New-Item -ItemType Directory -Path "$ProjectName/static/js" -Force
New-Item -ItemType Directory -Path "$ProjectName/static/css" -Force

# 3. Create core Django files with placeholder content
# settings.py (Crucial for Channels & Redis)
$SettingsContent = @"
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = 'django-insecure-your-key-here'
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    '$AppName',
    'channels',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = '$ProjectName.urls'
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = '$ProjectName.wsgi.application'
ASGI_APPLICATION = '$ProjectName.asgi.application'

# Redis Channel Layer for Matchmaking
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            "hosts": [('127.0.0.1', 6379)],
        },
    },
}

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
"@

$SettingsContent | Out-File -FilePath "$ProjectName/$ProjectName/settings.py" -Encoding utf8

# 4. Create models.py (Wallet & Profiles)
$ModelsContent = @"
from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    age = models.IntegerField(null=True, blank=True)
    sex = models.CharField(max_length=10, default='any')
    profession = models.CharField(max_length=50, default='any')
    is_premium = models.BooleanField(default=False)
    wallet_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    last_connected_user = models.ForeignKey(User, null=True, related_name='last_conn', on_delete=models.SET_NULL)

    # Preferences (Premium)
    pref_sex = models.CharField(max_length=10, default='any')
    pref_profession = models.CharField(max_length=50, default='any')

    def __str__(self):
        return self.user.username
"@

$ModelsContent | Out-File -FilePath "$ProjectName/$AppName/models.py" -Encoding utf8

# 5. Create asgi.py
$AsgiContent = @"
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import $AppName.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', '$ProjectName.settings')

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            $AppName.routing.websocket_urlpatterns
        )
    ),
})
"@

$AsgiContent | Out-File -FilePath "$ProjectName/$ProjectName/asgi.py" -Encoding utf8

# 6. Create requirements.txt
"django`ndaphne`nchannels`nchannels-redis`nrazorpay`nrequests" | Out-File -FilePath "$ProjectName/requirements.txt" -Encoding utf8

Write-Host "Project Structure Created Successfully!" -ForegroundColor Green