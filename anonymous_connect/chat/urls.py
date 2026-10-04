from django.urls import path, include
from . import views

urlpatterns = [
    # Main Landing Page / Dashboard
    # This handles the user profile form and the registration overlay
    path('', views.home, name='home'),
    path('hangup/', views.hangup_view, name='hangup_view'),

    path('login/', views.custom_login, name='login'),
    # Email OTP login (active)
    path('send-email-otp/', views.send_email_otp, name='send_email_otp'),
    path('verify-email-otp/', views.verify_email_otp, name='verify_email_otp'),
    # Mobile OTP login (temporarily disabled - see views.py)
    # path('send-otp/', views.send_otp, name='send_otp'),
    # path('verify-otp/', views.verify_otp, name='verify_otp'),

    # Allauth URLs (handles Google/Email callbacks)
    path('accounts/', include('allauth.urls')),

    # Matchmaking Trigger
    # <str:mode> will be 'voice' or 'chat' passed from your buttons
    path('match/<str:mode>/<str:name>/', views.match_user, name='match_user'),
    # Background matchmaking poll (JSON) for the searching overlay. Lets the
    # connecting screen check for a partner without reloading the whole page,
    # so the UI stays stable and the waiting tune plays smoothly.
    path('match-poll/<str:mode>/<str:name>/', views.match_poll, name='match_poll'),
    # Reconnect with a new partner after the current one disconnects.
    # Free members get one reconnect; further attempts are gated behind premium.
    path('reconnect/<str:mode>/<str:name>/', views.reconnect_user, name='reconnect_user'),
    # Premium Feature: Reconnect with the previous user
    path('connect-last/', views.connect_last_user, name='connect_last_user'),

    # Wallet & Premium Subscription logic
    path('premium/', views.premium_page, name='premium_page'),

    # Callback URL for successful UPI payments
    path('payment/success/', views.payment_success, name='payment_success'),

    # Password-protected activity report dashboard (hidden unless REPORT_ENABLED).
    # Served at /report.html to match the shared link.
    path('report.html', views.report_dashboard, name='report_dashboard'),

    # Health / readiness probe for cloud platforms
    path('healthz/', views.healthz, name='healthz'),

    # Short-lived WebRTC ICE servers (STUN + Cloudflare TURN) for voice calls.
    path('ice-servers/', views.ice_servers, name='ice_servers'),

    # Mandatory policy pages (linked from the footer for Razorpay activation)
    path('privacy-policy/', views.privacy_policy, name='privacy_policy'),
    path('terms-and-conditions/', views.terms_conditions, name='terms_conditions'),
    path('cancellation-refund-policy/', views.refund_policy, name='refund_policy'),
    path('contact-us/', views.contact_us, name='contact_us'),
    path('service-fulfillment/', views.service_fulfillment, name='service_fulfillment'),
]