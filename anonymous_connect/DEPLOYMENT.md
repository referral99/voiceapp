# Anonymous Connect - Deployment Guide

A Django + Channels (ASGI) app for anonymously connecting users over chat and
voice (WebRTC). This guide covers running it locally and deploying it to a
cheap cloud provider.

## Architecture

| Concern           | Local (dev)                    | Production                                  |
|-------------------|--------------------------------|---------------------------------------------|
| Web/WS server     | `runserver` (Daphne built-in)  | `daphne` (serves both HTTP and WebSockets)  |
| Database          | SQLite (auto)                  | PostgreSQL (`DB_ENGINE=postgres`)           |
| Channel layer     | In-memory (single process)     | Redis (`REDIS_URL`) - **required**          |
| Static files      | Django dev server              | WhiteNoise (built in, no extra service)     |
| TLS               | none                           | Reverse proxy / platform-provided HTTPS     |

> The in-memory channel layer only works with a **single process**. Any real
> deployment (or more than one worker) **must** set `REDIS_URL`, or matched
> users on different processes won't be able to message each other.

## 1. Local development

```bash
# From the anonymous_connect/ directory, with the virtualenv active:
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # optional, for /admin
python manage.py runserver
```

Open http://127.0.0.1:8000/. Emails (OTP, allauth) are printed to the console.

## 2. Environment variables

Copy `.env.example` to `.env` and fill it in. On most cloud platforms you set
these in the dashboard instead of committing a `.env` file. The important ones:

- `DJANGO_SECRET_KEY` - long random string (never reuse the dev key).
- `DJANGO_DEBUG=False` - **always** false in production.
- `DJANGO_ALLOWED_HOSTS` - your domain(s), comma-separated.
- `CSRF_TRUSTED_ORIGINS` - `https://yourdomain.com`.
- `REDIS_URL` - your managed Redis URL.
- `DB_ENGINE=postgres` plus `DB_*` - your managed Postgres.
- `RAZOR_KEY_ID` / `RAZOR_KEY_SECRET` - from the Razorpay dashboard.

Generate a secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

## 3. Deploy to a cheap cloud provider

The repo includes a `Procfile`, `runtime.txt`, and `requirements.txt` so it
works on Railway, Render, Fly.io, Heroku-style platforms, etc.

General steps (any provider):

1. Provision a **PostgreSQL** add-on and a **Redis** add-on. Copy their
   connection details into the environment variables above.
2. Set all environment variables from `.env.example`.
3. The `release` process runs migrations automatically on each deploy.
4. The `web` process starts Daphne:
   `daphne -b 0.0.0.0 -p $PORT anonymous_connect.asgi:application`
5. Run `python manage.py collectstatic --noinput` as part of the build
   (many platforms do this automatically for Django; otherwise add it to the
   build command). WhiteNoise serves the collected files.

### Railway / Render quick notes
- Build command: `pip install -r requirements.txt && python manage.py collectstatic --noinput`
- Start command: `daphne -b 0.0.0.0 -p $PORT anonymous_connect.asgi:application`
- Add Postgres and Redis plugins; their URLs are injected as env vars you can
  map to `DB_*` and `REDIS_URL`.

## 4. Post-deploy checklist

- [ ] `DJANGO_DEBUG=False` and a unique `DJANGO_SECRET_KEY` are set.
- [ ] `DJANGO_ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` match your domain.
- [ ] `REDIS_URL` points to a running Redis instance.
- [ ] Database migrations ran (`release` phase or manual `migrate`).
- [ ] `GET /healthz/` returns HTTP 200.
- [ ] HTTPS works and `SECURE_SSL_REDIRECT=True` (once TLS is confirmed).
- [ ] Razorpay keys are the **live** keys, not test keys.
- [ ] Google login: create a Social Application in `/admin` (Provider: Google,
      client id/secret from Google Cloud Console) or the login page shows it
      disabled. The app degrades gracefully if it's not configured.

## 5. WebRTC voice note

Voice uses browser WebRTC with a public STUN server. For users behind strict
NATs you may also need a **TURN** server (e.g. coturn or a hosted TURN
service). Signalling is relayed through the chat WebSocket, so no extra server
is needed for signalling itself.

## Security highlights already implemented

- Razorpay payments are verified **server-side** via signature verification
  before premium is granted (the client cannot self-upgrade).
- Secrets, `DEBUG`, hosts, and DB/Redis config are all environment-driven.
- Production security headers (HSTS, secure cookies, nosniff, `X-Frame-Options`)
  turn on automatically when `DEBUG=False`.
- Login attempts are rate-limited via allauth.
