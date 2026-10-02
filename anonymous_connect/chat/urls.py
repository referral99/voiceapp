from django.urls import path, include
from . import views

urlpatterns = [
    # Main Landing Page / Dashboard
    # This handles the user profile form and the registration overlay
    path('', views.home, name='home'),
    path('hangup/', views.hangup_view, name='hangup_view'),

    path('login/', views.custom_login, name='login'),
    path('send-otp/', views.send_otp, name='send_otp'),
    path('verify-otp/', views.verify_otp, name='verify_otp'),

    # Allauth URLs (handles Google/Email callbacks)
    path('accounts/', include('allauth.urls')),

    # Matchmaking Trigger
    # <str:mode> will be 'voice' or 'chat' passed from your buttons
    path('match/<str:mode>/<str:name>/', views.match_user, name='match_user'),
    # Reconnect with a new partner after the current one disconnects.
    # Free members get one reconnect; further attempts are gated behind premium.
    path('reconnect/<str:mode>/<str:name>/', views.reconnect_user, name='reconnect_user'),
    # Premium Feature: Reconnect with the previous user
    path('connect-last/', views.connect_last_user, name='connect_last_user'),

    # Wallet & Premium Subscription logic
    path('premium/', views.premium_page, name='premium_page'),

    # Callback URL for successful UPI payments
    path('payment/success/', views.payment_success, name='payment_success'),

    # Health / readiness probe for cloud platforms
    path('healthz/', views.healthz, name='healthz'),
]