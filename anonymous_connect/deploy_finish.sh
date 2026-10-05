#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Finish deployment of Anonymous Connect on Amazon Linux 2023.
# Idempotent: safe to re-run. Assumes system packages, Postgres, Redis, the
# code at /home/ec2-user/voice/voiceapp/anonymous_connect, and a Python 3.12
# virtualenv at that project's .venv already exist (set up in earlier steps).
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DIR=/home/ec2-user/voice/voiceapp/anonymous_connect
VENV=$APP_DIR/.venv
PYBIN=$VENV/bin/python
PIPBIN=$VENV/bin/pip
RUN_USER=ec2-user

# --- Domain / TLS configuration ---
# The public domain this app is served from. www is included as an alias.
DOMAIN=decentapp.org
WWW_DOMAIN=www.$DOMAIN
# Email used by Let's Encrypt for expiry notices. Override by exporting
# CERTBOT_EMAIL before running this script.
CERTBOT_EMAIL=${CERTBOT_EMAIL:-admin@$DOMAIN}

# --- Razorpay (payment gateway) ---
# The live key id/secret from the Razorpay dashboard. Export these before
# running the script so they get written into the production .env, e.g.:
#   export RAZOR_KEY_ID=rzp_live_xxxxxxxx
#   export RAZOR_KEY_SECRET=yyyyyyyyyyyyyyyy
# If left unset the script keeps whatever is already in the existing .env so a
# re-run never silently wipes working keys. Values in .env are base64-encoded
# (see step [2/7]), so decode them back to plaintext here; the writer below
# re-encodes once. `base64 -d` on empty/invalid input yields empty, so a
# missing key stays empty rather than corrupting.
_read_env_b64() {
  local enc
  enc=$(sudo awk -F= -v k="^$1=" '$0 ~ k {print $2; exit}' "$APP_DIR/.env" 2>/dev/null || true)
  [ -n "$enc" ] && printf '%s' "$enc" | base64 -d 2>/dev/null || true
}
RAZOR_KEY_ID=${RAZOR_KEY_ID:-$(_read_env_b64 RAZOR_KEY_ID)}
RAZOR_KEY_SECRET=${RAZOR_KEY_SECRET:-$(_read_env_b64 RAZOR_KEY_SECRET)}

# --- Email / SMTP (OTP + verification mail) ---
# Real OTP delivery needs an SMTP backend with valid credentials. Without them
# settings.py silently falls back to the console backend and NO mail is sent,
# which is exactly why users stopped receiving login codes. These values can be
# overridden by exporting them before running the script; otherwise the known
# production defaults below are used. EMAIL_HOST_PASSWORD is a real secret, so
# (like the other secrets) it is read back base64-decoded from the existing
# .env on a re-run and re-encoded once by the writer below.
EMAIL_HOST=${EMAIL_HOST:-smtp.gmail.com}
EMAIL_PORT=${EMAIL_PORT:-587}
EMAIL_USE_TLS=${EMAIL_USE_TLS:-True}
EMAIL_HOST_USER=${EMAIL_HOST_USER:-noreply.chatsanti@gmail.com}
EMAIL_HOST_PASSWORD=${EMAIL_HOST_PASSWORD:-$(_read_env_b64 EMAIL_HOST_PASSWORD)}
EMAIL_HOST_PASSWORD=${EMAIL_HOST_PASSWORD:-zbwc idxj qxlv oina}
DEFAULT_FROM_EMAIL=${DEFAULT_FROM_EMAIL:-noreply.chatsanti@gmail.com}

# --- Cloudflare Realtime TURN (WebRTC voice relay) ---
# Without these the app falls back to STUN-only, which breaks calls behind
# strict NATs. KEY_ID + API_TOKEN are long-term secrets -> base64-encoded in
# .env (decoded by env_secret()). Read back from the existing .env on re-run,
# else fall back to the known values; override by exporting before running.
CLOUDFLARE_TURN_KEY_ID=${CLOUDFLARE_TURN_KEY_ID:-$(_read_env_b64 CLOUDFLARE_TURN_KEY_ID)}
CLOUDFLARE_TURN_KEY_ID=${CLOUDFLARE_TURN_KEY_ID:-48a6b5767ff96d58da479c1e130a360b}
CLOUDFLARE_TURN_API_TOKEN=${CLOUDFLARE_TURN_API_TOKEN:-$(_read_env_b64 CLOUDFLARE_TURN_API_TOKEN)}
CLOUDFLARE_TURN_API_TOKEN=${CLOUDFLARE_TURN_API_TOKEN:-2a81a634b07be12a64e3f803029aee223877b66f599e0b91b0ca9afb696bcad1}

# --- Activity report (/report.html) ---
# Password-protected analytics dashboard. REPORT_PASSWORD is a secret ->
# base64-encoded (decoded by env_secret()); the others are plaintext flags.
REPORT_ENABLED=${REPORT_ENABLED:-True}
REPORT_USERNAME=${REPORT_USERNAME:-admin}
REPORT_PASSWORD=${REPORT_PASSWORD:-$(_read_env_b64 REPORT_PASSWORD)}
REPORT_PASSWORD=${REPORT_PASSWORD:-Report2@5314}

# Set ENABLE_TLS=0 to skip the Certbot/HTTPS step (e.g. before DNS points at
# this box). The app will then be served over plain HTTP on the domain/IP.
ENABLE_TLS=${ENABLE_TLS:-1}

# DB password created in an earlier step (also stored at /root/.app_dbpass).
DBPASS=$(sudo awk -F= '/^DBPASS=/{print $2}' /root/.app_dbpass 2>/dev/null || true)
if [ -z "${DBPASS}" ]; then
  echo "ERROR: could not read DB password from /root/.app_dbpass" >&2
  exit 1
fi

PUBLIC_IP=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/public-ipv4 || echo "13.60.20.71")

echo "==> [1/7] Ensuring Python requirements are installed"
cd "$APP_DIR"
"$PIPBIN" install -r requirements.txt
"$PIPBIN" check || true

echo "==> [2/7] Writing production .env"
SECRET=$("$PYBIN" -c "from django.core.management.utils import get_random_secret_key as g; print(g())")

if [ "$ENABLE_TLS" = "1" ]; then
  # HTTPS: trust the https origins and turn on secure cookies + SSL redirect.
  SCHEME=https
  SECURE_COOKIES=True
  SSL_REDIRECT=True
  HSTS_SECONDS=31536000
else
  # Plain HTTP (no cert yet): keep secure-cookie/redirect off or the site breaks.
  SCHEME=http
  SECURE_COOKIES=False
  SSL_REDIRECT=False
  HSTS_SECONDS=0
fi

# Secrets are stored base64-ENCODED in the .env file. This is encoding, not
# encryption: it only obscures values from a casual glance (chmod 600 below is
# the real protection). settings.py's env_secret() decodes them at load time.
# `base64 -w0` keeps the output on a single line (GNU coreutils on AL2023).
b64() { printf '%s' "$1" | base64 -w0; }
SECRET_B64=$(b64 "$SECRET")
DBPASS_B64=$(b64 "$DBPASS")
RAZOR_KEY_ID_B64=$(b64 "$RAZOR_KEY_ID")
RAZOR_KEY_SECRET_B64=$(b64 "$RAZOR_KEY_SECRET")
# The SMTP app password is a real secret -> base64-encode it like the others so
# settings.py's env_secret() decodes it back to the real Gmail app password.
EMAIL_HOST_PASSWORD_B64=$(b64 "$EMAIL_HOST_PASSWORD")
# Cloudflare TURN + report secrets are also base64-encoded like the rest.
CLOUDFLARE_TURN_KEY_ID_B64=$(b64 "$CLOUDFLARE_TURN_KEY_ID")
CLOUDFLARE_TURN_API_TOKEN_B64=$(b64 "$CLOUDFLARE_TURN_API_TOKEN")
REPORT_PASSWORD_B64=$(b64 "$REPORT_PASSWORD")

cat > "$APP_DIR/.env" <<ENVEOF
DJANGO_SECRET_KEY=$SECRET_B64
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=$DOMAIN,$WWW_DOMAIN,$PUBLIC_IP,127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=$SCHEME://$DOMAIN,$SCHEME://$WWW_DOMAIN
DJANGO_LOG_LEVEL=INFO
SITE_URL=$SCHEME://$DOMAIN

DB_ENGINE=postgres
DB_NAME=anonymous_db
DB_USER=appuser
DB_PASSWORD=$DBPASS_B64
DB_HOST=127.0.0.1
DB_PORT=5432

REDIS_URL=redis://127.0.0.1:6379/0

# Secure cookies / SSL redirect follow whether TLS is enabled (see ENABLE_TLS).
SESSION_COOKIE_SECURE=$SECURE_COOKIES
CSRF_COOKIE_SECURE=$SECURE_COOKIES
SECURE_SSL_REDIRECT=$SSL_REDIRECT
SECURE_HSTS_SECONDS=$HSTS_SECONDS

ACCOUNT_EMAIL_VERIFICATION=optional

RAZOR_KEY_ID=$RAZOR_KEY_ID_B64
RAZOR_KEY_SECRET=$RAZOR_KEY_SECRET_B64

# Email / SMTP. EMAIL_BACKEND=smtp forces real delivery (otherwise settings.py
# falls back to the console backend when no password is set). The password is
# stored base64-encoded; env_secret() decodes it at load time.
EMAIL_BACKEND=smtp
EMAIL_HOST=$EMAIL_HOST
EMAIL_PORT=$EMAIL_PORT
EMAIL_USE_TLS=$EMAIL_USE_TLS
EMAIL_HOST_USER=$EMAIL_HOST_USER
EMAIL_HOST_PASSWORD=$EMAIL_HOST_PASSWORD_B64
DEFAULT_FROM_EMAIL=$DEFAULT_FROM_EMAIL

# Cloudflare Realtime TURN (WebRTC relay). Secrets are base64-encoded.
CLOUDFLARE_TURN_KEY_ID=$CLOUDFLARE_TURN_KEY_ID_B64
CLOUDFLARE_TURN_API_TOKEN=$CLOUDFLARE_TURN_API_TOKEN_B64

# Activity report dashboard (/report.html). REPORT_PASSWORD is base64-encoded.
REPORT_ENABLED=$REPORT_ENABLED
REPORT_USERNAME=$REPORT_USERNAME
REPORT_PASSWORD=$REPORT_PASSWORD_B64
ENVEOF
chmod 600 "$APP_DIR/.env"

echo "==> [3/7] Running migrations"
"$PYBIN" manage.py migrate --noinput

echo "==> [4/7] Collecting static files"
"$PYBIN" manage.py collectstatic --noinput

echo "==> [5/7] Installing systemd service for Daphne"
sudo tee /etc/systemd/system/anonymous_connect.service >/dev/null <<UNITEOF
[Unit]
Description=Anonymous Connect (Daphne ASGI)
After=network.target postgresql.service redis6.service
Requires=postgresql.service redis6.service

[Service]
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/daphne -b 127.0.0.1 -p 8000 anonymous_connect.asgi:application
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNITEOF

sudo systemctl daemon-reload
sudo systemctl enable --now anonymous_connect
sleep 3
sudo systemctl is-active anonymous_connect

echo "==> [6/7] Configuring Nginx reverse proxy (HTTP + WebSocket)"
# Written with the domain substituted in for server_name. Certbot (below) will
# add the :443 TLS server block and an HTTP->HTTPS redirect to this file.
sudo tee /etc/nginx/conf.d/anonymous_connect.conf >/dev/null <<NGINXEOF
map \$http_upgrade \$connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80 default_server;
    server_name $DOMAIN $WWW_DOMAIN;

    client_max_body_size 10m;

    location /static/ {
        alias /home/ec2-user/voice/voiceapp/anonymous_connect/staticfiles/;
        access_log off;
        expires 30d;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINXEOF

# Neutralise the stock default server block if present. The AL2023 nginx.conf
# ships `server { listen 80; listen [::]:80; server_name _; ... }`. Because our
# app block (above) is `listen 80 default_server`, the stock block still answers
# host-less requests (curl to 127.0.0.1, health/uptime probes, Let's Encrypt's
# own validation) and 404s them before they reach Django. We comment that one
# block out (idempotent, with a backup) so EVERY :80 request routes to the app.
# NOTE: the previous `sed` here looked for `listen 80 default_server;`, which
# the stock file never contains (it uses a bare `listen 80;`), so it silently
# did nothing - that mismatch is exactly what left the 404s in place.
if [ -f /etc/nginx/nginx.conf ] && grep -qE '^\s*listen\s+80\s*;' /etc/nginx/nginx.conf; then
  sudo cp -a /etc/nginx/nginx.conf "/etc/nginx/nginx.conf.bak.$(date +%Y%m%d%H%M%S)"
  sudo awk '
    BEGIN{inblk=0; depth=0}
    /^[[:space:]]*server[[:space:]]*\{/ && inblk==0 { inblk=1; depth=1; buf=$0 ORS; isdef=0; next }
    inblk==1 {
      buf=buf $0 ORS
      depth += gsub(/\{/,"{"); depth -= gsub(/\}/,"}")
      if ($0 ~ /listen[[:space:]]+80[[:space:]]*;/) isdef=1
      if (depth<=0) {
        if (isdef) { gsub(/\n/,"\n# ",buf); sub(/^/,"# ",buf) }
        printf "%s", buf; inblk=0; buf=""; next
      }
      next
    }
    { print }
  ' /etc/nginx/nginx.conf | sudo tee /etc/nginx/nginx.conf.new >/dev/null
  # Only swap in the edited file if it still passes nginx's own validation.
  if sudo nginx -t -c /etc/nginx/nginx.conf.new >/dev/null 2>&1; then
    sudo mv /etc/nginx/nginx.conf.new /etc/nginx/nginx.conf
  else
    sudo rm -f /etc/nginx/nginx.conf.new
    echo "WARNING: edited nginx.conf failed validation; left the original in place." >&2
  fi
fi

sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl restart nginx

if [ "$ENABLE_TLS" = "1" ]; then
  echo "==> [6b/7] Obtaining Let's Encrypt certificate for $DOMAIN"
  # Install certbot + the nginx plugin (Amazon Linux 2023).
  if ! command -v certbot >/dev/null 2>&1; then
    sudo dnf install -y certbot python3-certbot-nginx || \
      echo "WARNING: certbot install failed; skipping TLS. Re-run after fixing." >&2
  fi
  if command -v certbot >/dev/null 2>&1; then
    # --nginx edits the server block above to add :443 and an HTTP->HTTPS
    # redirect. Non-interactive; idempotent (reuses the cert on re-run).
    sudo certbot --nginx \
      -d "$DOMAIN" -d "$WWW_DOMAIN" \
      --non-interactive --agree-tos --redirect \
      -m "$CERTBOT_EMAIL" || \
      echo "WARNING: certbot failed (is DNS for $DOMAIN pointing at this box, and is TCP 443 open in the Security Group?). Site is still up on HTTP." >&2
    sudo nginx -t && sudo systemctl reload nginx || true
    # Ensure the auto-renew timer is active.
    sudo systemctl enable --now certbot-renew.timer 2>/dev/null || \
      sudo systemctl enable --now certbot.timer 2>/dev/null || true
  fi
else
  echo "==> [6b/7] ENABLE_TLS=0 -> skipping Certbot (serving plain HTTP)"
fi

# Let nginx read the static dir and traverse ec2-user's home.
sudo chmod o+x /home/ec2-user /home/ec2-user/voice /home/ec2-user/voice/voiceapp || true

echo "==> [7/7] Local smoke test"
sleep 2
echo "--- GET /healthz/ via nginx (localhost) ---"
curl -s -o /dev/null -w "healthz HTTP %{http_code}\n" http://127.0.0.1/healthz/ || true
echo "--- GET / via nginx (localhost) ---"
curl -s -o /dev/null -w "home HTTP %{http_code}\n" http://127.0.0.1/ || true

if [ "$ENABLE_TLS" = "1" ]; then SITE_URL="https://$DOMAIN"; else SITE_URL="http://$DOMAIN"; fi

echo
echo "============================================================"
echo "DONE. Domain: $DOMAIN  (public IP: $PUBLIC_IP)"
echo "Open $SITE_URL/ in a browser."
echo
echo "BEFORE THIS WORKS, make sure:"
echo "  1. DNS A records for $DOMAIN and $WWW_DOMAIN point to $PUBLIC_IP."
echo "  2. The EC2 Security Group allows inbound TCP 80 AND 443."
echo "  3. (TLS) Certbot succeeded above; if not, fix DNS/ports and re-run."
echo "============================================================"
