import os
import django
from django.core.asgi import get_asgi_application
from channels.sessions import SessionMiddlewareStack

# 1. Set the settings module FIRST
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'anonymous_connect.settings')

# 2. Initialize Django SECOND
django.setup()

# 3. Import Channels components THIRD (after django.setup)
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import chat.routing

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": SessionMiddlewareStack(
        AuthMiddlewareStack(
            URLRouter(chat.routing.websocket_urlpatterns)
        )
    ),
})