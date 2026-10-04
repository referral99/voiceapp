"""
Django settings for anonymous_connect project.

Configured to run out-of-the-box locally (SQLite + in-memory channel layer)
and to be deployment-ready on a cheap cloud host via environment variables.
"""
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    """Minimal, dependency-free .env loader.

    Reads simple KEY=VALUE lines from a .env file (if present) into the process
    environment, without overriding variables that are already set. Supports
    '#' comments and optional surrounding quotes. Real OS/platform environment
    variables always win, so production config is unaffected.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Load a local .env file (ignored by git) before reading any settings below.
load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(',') if item.strip()]


def env_secret(name, default=''):
    """Read a secret from the environment, transparently base64-decoding it.

    Production secrets are stored base64-ENCODED in the .env file (encoding, not
    encryption - it only obscures values from a casual glance, it does NOT make
    them secure; file permissions are the real protection). This helper decodes
    them back to their real value at load time.

    To stay backward compatible with plaintext values (e.g. the local dev .env),
    a value is only decoded when it round-trips cleanly as base64 AND decodes to
    valid UTF-8 text. Anything else is returned unchanged. Surrounding
    whitespace / trailing newlines (which base64 tools often add) are stripped.
    """
    import base64
    import binascii

    raw = os.environ.get(name, default)
    if raw is None:
        return default
    candidate = raw.strip()
    if not candidate:
        return candidate

    try:
        # validate=True rejects anything containing non-base64 characters, so
        # ordinary plaintext secrets (which usually contain -, @, !, spaces,
        # etc.) are left untouched.
        decoded_bytes = base64.b64decode(candidate, validate=True)
        decoded = decoded_bytes.decode('utf-8')
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return candidate

    # Re-encoding must reproduce the original string; otherwise the value just
    # happened to look like base64 but wasn't intended as such - keep it as-is.
    reencoded = base64.b64encode(decoded_bytes).decode('ascii')
    if reencoded != candidate:
        return candidate

    return decoded.strip()


# SECURITY WARNING: keep the secret key used in production secret!
# Stored base64-encoded in production .env; env_secret() decodes it (and leaves
# the plaintext local-dev default untouched).
SECRET_KEY = env_secret(
    'DJANGO_SECRET_KEY',
    'django-insecure-7imxz52l-!8zwg-r65zc%!g88i14gm6q((-$#a6-up#v#_-$+x',
)

# True while running the test suite (``manage.py test``). Used to neutralise
# production-only settings (HTTPS redirect, strict hosts) so tests are
# deterministic regardless of any ambient environment variables on the machine.
RUNNING_TESTS = 'test' in sys.argv

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env_bool('DJANGO_DEBUG', True)

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', '*') or ['*']

# Trusted origins for CSRF when running behind HTTPS in production.
# e.g. CSRF_TRUSTED_ORIGINS="https://myapp.example.com"
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS', '')

# Public, canonical base URL of the site (no trailing slash). Used to build
# absolute URLs for SEO tags: canonical link, Open Graph / Twitter share URLs
# and images, and JSON-LD. Set SITE_URL in production env to the real domain,
# e.g. SITE_URL=https://decentapp.org
SITE_URL = os.environ.get('SITE_URL', 'https://decentapp.org').rstrip('/')


# Application definition

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'chat',
    'activity_logging',
    'django.contrib.sites',
    'django.contrib.sitemaps',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',  # For Google Email login
]

SITE_ID = 1

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'anonymous_connect.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'chat.context_processors.seo',
            ],
        },
    },
]

WSGI_APPLICATION = 'anonymous_connect.wsgi.application'
ASGI_APPLICATION = 'anonymous_connect.asgi.application'


# Database
# Uses Postgres when DB_ENGINE=postgres and connection env vars are provided,
# otherwise falls back to SQLite so the app runs with zero setup.
if os.environ.get('DB_ENGINE', 'sqlite').lower() in ('postgres', 'postgresql'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME', 'anonymous_db'),
            'USER': os.environ.get('DB_USER', 'postgres'),
            'PASSWORD': env_secret('DB_PASSWORD', 'postgres'),
            'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
            'PORT': os.environ.get('DB_PORT', '5432'),
            'OPTIONS': {
                'connect_timeout': 5,
                'keepalives': 1,
                'keepalives_idle': 30,
                'keepalives_interval': 10,
                'keepalives_count': 5,
            },
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# Database connection persistence (in seconds).
CONN_MAX_AGE = 60


# Channel layers
# Uses Redis when REDIS_URL is set, otherwise an in-memory layer for local dev.
#
# IMPORTANT: InMemoryChannelLayer is per-process only. With more than one
# worker/instance, two matched users can land on different processes and
# group_send() will never reach the other side (no messages / no WebRTC
# signalling). Production MUST use the Redis layer so every process shares
# the same bus. We therefore require REDIS_URL whenever DEBUG is off.
REDIS_URL = os.environ.get('REDIS_URL')
if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                'hosts': [REDIS_URL],
            },
        },
    }
elif DEBUG:
    # Local single-process development only.
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }
else:
    raise ImproperlyConfigured(
        'REDIS_URL must be set in production. The in-memory channel layer '
        'is per-process and breaks messaging/WebRTC signalling across '
        'multiple workers or instances.'
    )


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# --- Activity report (/report.html) ---
# A password-protected HTML dashboard built from the activity log. Disabled by
# default; flip REPORT_ENABLED on to expose it. REPORT_PASSWORD gates access via
# HTTP Basic Auth (username is REPORT_USERNAME, default "admin"). Set
# REPORT_GENDER_BREAKDOWN to compute the visitors-by-gender section.
REPORT_ENABLED = env_bool('REPORT_ENABLED', False)
REPORT_USERNAME = os.environ.get('REPORT_USERNAME', 'admin')
REPORT_PASSWORD = env_secret('REPORT_PASSWORD', '')
REPORT_GENDER_BREAKDOWN = env_bool('REPORT_GENDER_BREAKDOWN', True)
# Resolve visitor IPs to countries using the on-disk cache / ip-api.com. Turn
# off to keep the report fully offline (countries show as "Unresolved").
REPORT_GEO_LOOKUP = env_bool('REPORT_GEO_LOOKUP', True)


# Razorpay (payments). Leave blank to disable the payment gateway gracefully.
# Keys are stored base64-encoded in production .env; env_secret() decodes them.
RAZOR_KEY_ID = env_secret('RAZOR_KEY_ID', '')
RAZOR_KEY_SECRET = env_secret('RAZOR_KEY_SECRET', '')

# Cloudflare Realtime TURN (WebRTC relay for voice calls).
# The TURN key ID and its API token are long-term secrets kept server-side.
# The backend uses them to mint short-lived ICE credentials for each call
# (see chat.views.ice_servers). Leave blank to fall back to STUN-only.
# Stored base64-encoded in production .env; env_secret() decodes them.
CLOUDFLARE_TURN_KEY_ID = env_secret('CLOUDFLARE_TURN_KEY_ID', '')
CLOUDFLARE_TURN_API_TOKEN = env_secret('CLOUDFLARE_TURN_API_TOKEN', '')
# How long issued TURN credentials remain valid, in seconds. Should comfortably
# exceed the longest expected call (default: 24h).
CLOUDFLARE_TURN_TTL = int(os.environ.get('CLOUDFLARE_TURN_TTL', '86400'))

# Twilio (OTP SMS). Leave blank to fall back to logging the OTP (dev mode).
# TWILIO_SID / TWILIO_FROM are identifiers; TWILIO_TOKEN is the real secret and
# is stored base64-encoded in production .env (decoded by env_secret()).
TWILIO_SID = os.environ.get('TWILIO_SID', '')
TWILIO_TOKEN = env_secret('TWILIO_TOKEN', '')
TWILIO_FROM = os.environ.get('TWILIO_FROM', '')

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = 'home'


AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

# --- django-allauth account configuration ---
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'username*', 'password1*', 'password2*']
# Require a verified email in production; 'optional' locally so dev signups work.
ACCOUNT_EMAIL_VERIFICATION = os.environ.get(
    'ACCOUNT_EMAIL_VERIFICATION', 'optional' if DEBUG else 'mandatory'
)
ACCOUNT_RATE_LIMITS = {
    'login_failed': '5/5m',  # throttle brute-force login attempts
}
ACCOUNT_UNIQUE_EMAIL = True
SOCIALACCOUNT_LOGIN_ON_GET = False  # avoid CSRF-style provider login via GET

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
    }
}

# --- Email backend ---
# Sends real OTP / verification emails over SMTP. Credentials are read from the
# environment ONLY (never hardcoded) - set EMAIL_HOST_USER / EMAIL_HOST_PASSWORD
# in your .env or platform env vars. Set EMAIL_BACKEND=console (env var) to print
# emails to the log instead, which is the recommended default for local dev.
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
# SMTP password/app-password is a real secret: stored base64-encoded in prod
# .env (decoded by env_secret()). Empty stays empty, so the console-backend
# fallback below still works when no SMTP password is configured.
EMAIL_HOST_PASSWORD = env_secret('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'no-reply@localhost')

# Choose the email backend. Default to the console backend whenever SMTP
# credentials are not configured so a missing password never silently breaks
# OTP delivery - the code is logged instead (see send_email_otp). Set
# EMAIL_BACKEND=smtp explicitly, or just provide EMAIL_HOST_USER/PASSWORD, to
# send real mail.
_email_backend = os.environ.get('EMAIL_BACKEND', '').lower()
if _email_backend == 'console' or (not _email_backend and not EMAIL_HOST_PASSWORD):
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'


# Production security hardening (enabled automatically when DEBUG is off).
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = env_bool('SESSION_COOKIE_SECURE', True)
    CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', True)
    SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', False)
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '0'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', True)
    SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', True)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'


# Keep the test suite deterministic. The Django test client talks plain HTTP to
# "testserver", so a production SSL redirect or a strict ALLOWED_HOSTS list
# (which can leak in from the machine's environment variables) would turn every
# request into a 301/400 and break otherwise-valid tests. Neutralise them while
# running tests only; production behaviour is unaffected.
if RUNNING_TESTS:
    SECURE_SSL_REDIRECT = False
    ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False


# --- Logging ---
# Log to stdout so cloud platforms (Railway, Render, Fly, etc.) capture it.
#
# User-activity logs (visits, call attempts, premium events, etc.) are kept
# in their OWN rotating file under ``activity_logs/`` via the dedicated
# ``activity`` logger, so they stay separate from ordinary app/server logs and
# are easy to turn into a report later.
ACTIVITY_LOG_DIR = BASE_DIR / 'activity_logs'
ACTIVITY_LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {name} {message}',
            'style': '{',
        },
        # Activity records are already fully self-describing (key=value), so the
        # file formatter stays minimal - just a timestamp prefix.
        'activity': {
            'format': '{asctime} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        # Dedicated rotating file for user-activity logs only.
        'activity_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(ACTIVITY_LOG_DIR / 'activity.log'),
            'maxBytes': 5 * 1024 * 1024,   # 5 MB per file
            'backupCount': 10,             # keep 10 rotated files
            'encoding': 'utf-8',
            'formatter': 'activity',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        # The activity logger writes to its own file AND to the console (so
        # cloud log streams still capture it). It does not propagate to root to
        # avoid duplicate console lines.
        'activity': {
            'handlers': ['activity_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
