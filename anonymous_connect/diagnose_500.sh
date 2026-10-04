#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# diagnose_500.sh - Diagnose and (optionally) repair the HTTP 500 errors seen
# in access.log for Anonymous Connect (decentapp.org).
#
# WHAT THE LOG SHOWED (why this script exists)
#   * 03/Oct (UTC): only /login/?next=... returned 500 with ~110 KB bodies.
#     A 110 KB response body is a Django DEBUG=True traceback page -> DEBUG was
#     ON in production, leaking source/settings. The login page itself depends
#     on the DB (SocialApp lookup + sessions).
#   * 04/Oct (IST): /, /favicon.ico and /sitemap.xml ALL returned 500 with tiny
#     (~145-465 byte) bodies -> DEBUG=False now, but the whole site is down.
#     Every one of those routes ultimately touches the database (session
#     create + UserProfile.get_or_create). A site-wide 500 like this almost
#     always means the DB/Redis backend is unreachable or migrations are
#     missing - NOT an application bug (manage.py check reports 0 issues).
#
# This script inspects the running production stack, reports the root cause,
# and - only when run with --fix - restarts/repairs the backing services.
#
# USAGE (run ON the server, as a sudo-capable user):
#   bash diagnose_500.sh            # read-only diagnosis, changes nothing
#   bash diagnose_500.sh --fix      # also attempt safe, reversible repairs
#
# It is safe to run the diagnosis repeatedly. --fix only performs idempotent,
# non-destructive actions (service restarts, running migrations, reloading
# nginx). It never drops data or rewrites secrets.
# ---------------------------------------------------------------------------

APP_DIR=${APP_DIR:-/home/ec2-user/voice/voiceapp/anonymous_connect}
VENV=${VENV:-$APP_DIR/.venv}
PYBIN=$VENV/bin/python
SERVICE=anonymous_connect
FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
problems=()

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$YELLOW" "$RESET" "$*"; }
bad()  { printf '%s[FAIL]%s %s\n' "$RED" "$RESET" "$*"; problems+=("$*"); }
hr()   { printf '%s\n' "------------------------------------------------------------"; }

# Read a KEY from the production .env (values may be base64-encoded; we only
# need to test presence / plain flags here, so we do not decode secrets).
env_get() { sudo awk -F= -v k="^$1=" '$0 ~ k {sub(/^[^=]*=/,""); print; exit}' "$APP_DIR/.env" 2>/dev/null; }

# The public domain nginx routes by name. A bare request to 127.0.0.1 carries
# Host: 127.0.0.1, which matches NO server_name -> nginx falls through to the
# stock default server and 404s. So smoke tests must send the real Host header
# (and -L to follow the HTTP->HTTPS 301 that SECURE_SSL_REDIRECT emits).
DOMAIN=$(env_get DJANGO_ALLOWED_HOSTS | tr ',' '\n' | grep -vE '^(127\.0\.0\.1|localhost|\*)$' | head -n1)
DOMAIN=${DOMAIN:-decentapp.org}

# smoke PATH -> echoes the final HTTP code reaching the Django app through
# nginx, routed by the real Host and following redirects. --resolve pins the
# domain to localhost so DNS/Cloudflare are bypassed and we test THIS box.
smoke() {
  local path=$1
  curl -sL --max-time 10 \
       --resolve "$DOMAIN:80:127.0.0.1" \
       --resolve "$DOMAIN:443:127.0.0.1" \
       -o /dev/null -w '%{http_code}' \
       "http://$DOMAIN$path" 2>/dev/null || echo "000"
}

say "${BOLD}== Anonymous Connect 500 diagnosis ==${RESET}"
say "App dir : $APP_DIR"
say "Mode    : $([ $FIX -eq 1 ] && echo 'DIAGNOSE + FIX' || echo 'DIAGNOSE ONLY (read-only)')"
hr

# --- 0. Basic layout sanity ------------------------------------------------
if [ ! -d "$APP_DIR" ]; then
  bad "App directory $APP_DIR not found. Set APP_DIR=... and re-run."
  exit 1
fi
[ -x "$PYBIN" ] || warn "Python venv not found at $PYBIN (migrations/ORM checks will be skipped)."

# --- 1. DEBUG must be False in production ----------------------------------
say "${BOLD}[1] DJANGO_DEBUG${RESET}"
DEBUG_VAL=$(env_get DJANGO_DEBUG)
if [ -z "$DEBUG_VAL" ]; then
  bad "DJANGO_DEBUG is not set in .env -> settings.py defaults it to True, which leaks tracebacks (the 110 KB /login/ 500 bodies). Set DJANGO_DEBUG=False."
elif printf '%s' "$DEBUG_VAL" | grep -qiE '^(1|true|yes|on)$'; then
  bad "DJANGO_DEBUG=$DEBUG_VAL (DEBUG is ON in production). Set DJANGO_DEBUG=False."
else
  ok "DJANGO_DEBUG=$DEBUG_VAL (DEBUG off)."
fi
hr

# --- 2. Application service -------------------------------------------------
say "${BOLD}[2] systemd service: $SERVICE (Daphne ASGI)${RESET}"
if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE.service"; then
  if systemctl is-active --quiet "$SERVICE"; then
    ok "$SERVICE is active."
  else
    bad "$SERVICE is NOT active - the app process is down."
  fi
  say "  recent app logs (last 15 lines):"
  sudo journalctl -u "$SERVICE" -n 15 --no-pager 2>/dev/null | sed 's/^/    /' || true
else
  bad "systemd unit $SERVICE.service not installed. Re-run deploy_finish.sh."
fi
hr

# --- 3. PostgreSQL (the most likely culprit for site-wide 500s) ------------
say "${BOLD}[3] PostgreSQL${RESET}"
DB_ENGINE=$(env_get DB_ENGINE)
if printf '%s' "$DB_ENGINE" | grep -qiE 'postgres'; then
  DB_HOST=$(env_get DB_HOST); DB_PORT=$(env_get DB_PORT)
  DB_HOST=${DB_HOST:-127.0.0.1}; DB_PORT=${DB_PORT:-5432}
  if systemctl is-active --quiet postgresql 2>/dev/null; then
    ok "postgresql service is active."
  else
    bad "postgresql service is NOT active -> every DB-backed page (/, /login/, sessions) returns 500."
  fi
  # TCP reachability
  if (exec 3<>"/dev/tcp/$DB_HOST/$DB_PORT") 2>/dev/null; then
    ok "Postgres reachable at $DB_HOST:$DB_PORT."
    exec 3>&- 2>/dev/null || true
  else
    bad "Cannot open TCP $DB_HOST:$DB_PORT - Django cannot reach Postgres."
  fi
else
  warn "DB_ENGINE is '$DB_ENGINE' (not postgres). Production is expected to use Postgres per deploy_finish.sh."
fi
hr

# --- 4. Redis (channel layer; required when DEBUG=False) -------------------
say "${BOLD}[4] Redis (channels)${RESET}"
REDIS_URL=$(env_get REDIS_URL)
if [ -z "$REDIS_URL" ]; then
  bad "REDIS_URL not set. settings.py raises ImproperlyConfigured when DEBUG=False and REDIS_URL is missing -> the app refuses to boot."
else
  if systemctl is-active --quiet redis6 2>/dev/null || systemctl is-active --quiet redis 2>/dev/null; then
    ok "redis service is active."
  else
    bad "Redis service is NOT active (REDIS_URL=$REDIS_URL set) -> WebSocket signalling and app startup affected."
  fi
fi
hr

# --- 5. Django's own checks + migration state ------------------------------
say "${BOLD}[5] Django system check & migrations${RESET}"
if [ -x "$PYBIN" ]; then
  if sudo -u "$(stat -c %U "$APP_DIR")" "$PYBIN" "$APP_DIR/manage.py" check --deploy >/tmp/dj_check.txt 2>&1; then
    ok "manage.py check --deploy passed."
  else
    warn "manage.py check --deploy reported issues (see below):"
    sed 's/^/    /' /tmp/dj_check.txt || true
  fi
  if sudo -u "$(stat -c %U "$APP_DIR")" "$PYBIN" "$APP_DIR/manage.py" showmigrations --plan 2>/tmp/mig.txt | grep -q '\[ \]'; then
    bad "There are UNAPPLIED migrations -> run migrate (or use --fix)."
  else
    ok "All migrations applied."
  fi
else
  warn "Skipping Django checks (no venv python)."
fi
hr

# --- 6. nginx --------------------------------------------------------------
say "${BOLD}[6] nginx reverse proxy${RESET}"
if systemctl is-active --quiet nginx 2>/dev/null; then
  ok "nginx is active."
  sudo nginx -t 2>/tmp/nginx_t.txt && ok "nginx config valid." || { bad "nginx config test failed:"; sed 's/^/    /' /tmp/nginx_t.txt; }
else
  bad "nginx is NOT active - nothing is answering on :80/:443."
fi
hr

# --- 7. Local smoke test (bypasses the internet / Cloudflare) --------------
say "${BOLD}[7] Local smoke test via nginx (Host: $DOMAIN, following redirects)${RESET}"
for path in /healthz/ / ; do
  code=$(smoke "$path")
  case "$code" in
    2*)    ok  "GET $path -> HTTP $code" ;;
    3*)    warn "GET $path -> HTTP $code (redirect not followed to a 2xx - check SSL redirect / cert)" ;;
    404)   bad "GET $path -> HTTP 404 (nginx is not routing to Django - the stock default server block is shadowing the app; run with --fix)" ;;
    000)   bad "GET $path -> no response (connection refused - is nginx up?)" ;;
    *)     bad "GET $path -> HTTP $code" ;;
  esac
done
hr

# --- Summary ---------------------------------------------------------------
say "${BOLD}== Summary ==${RESET}"
if [ ${#problems[@]} -eq 0 ]; then
  ok "No problems detected. If users still see 500s, capture a live traceback:"
  say "    sudo journalctl -u $SERVICE -f    # then reproduce the request"
  exit 0
fi
say "${RED}Detected ${#problems[@]} problem(s):${RESET}"
for p in "${problems[@]}"; do say "  - $p"; done
hr

# --- Optional repair --------------------------------------------------------
if [ $FIX -eq 0 ]; then
  say "Re-run with ${BOLD}--fix${RESET} to attempt safe, reversible repairs:"
  say "  * start/restart postgresql, redis, $SERVICE and nginx"
  say "  * apply pending migrations"
  say "  * force DJANGO_DEBUG=False in .env (keeps a .env.bak backup)"
  exit 1
fi

say "${BOLD}== Applying fixes (safe / reversible) ==${RESET}"

# 7a. Ensure DEBUG=False (keeps a timestamped backup; never touches secrets).
DEBUG_VAL=$(env_get DJANGO_DEBUG)
if [ -z "$DEBUG_VAL" ] || printf '%s' "$DEBUG_VAL" | grep -qiE '^(1|true|yes|on)$'; then
  ts=$(date +%Y%m%d%H%M%S)
  sudo cp -a "$APP_DIR/.env" "$APP_DIR/.env.bak.$ts"
  if sudo grep -q '^DJANGO_DEBUG=' "$APP_DIR/.env"; then
    sudo sed -i 's/^DJANGO_DEBUG=.*/DJANGO_DEBUG=False/' "$APP_DIR/.env"
  else
    echo 'DJANGO_DEBUG=False' | sudo tee -a "$APP_DIR/.env" >/dev/null
  fi
  ok "Set DJANGO_DEBUG=False (backup: .env.bak.$ts)."
fi

# 7b. Bring backing services up (order matters: DB + Redis before the app).
for svc in postgresql redis6 redis; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^$svc.service"; then
    sudo systemctl enable --now "$svc" 2>/dev/null || true
    sudo systemctl restart "$svc" 2>/dev/null || true
    systemctl is-active --quiet "$svc" && ok "$svc running." || warn "$svc still not active - check 'journalctl -u $svc'."
  fi
done

# 7c. Apply migrations.
if [ -x "$PYBIN" ]; then
  owner=$(stat -c %U "$APP_DIR")
  if sudo -u "$owner" "$PYBIN" "$APP_DIR/manage.py" migrate --noinput; then
    ok "Migrations applied."
  else
    warn "migrate failed - likely the DB is still unreachable. Fix Postgres first."
  fi
fi

# 7c-bis. Neutralise the stock nginx default server block if it is shadowing
# the app. The AL2023 default /etc/nginx/nginx.conf ships a `server { listen
# 80; server_name _; ... }` block that answers host-less requests (curl to
# 127.0.0.1, health probes) and 404s them before they ever reach Django.
# We comment the whole block out (idempotent) and keep a timestamped backup so
# it is fully reversible. Named requests to $DOMAIN are unaffected either way.
NGINX_MAIN=/etc/nginx/nginx.conf
if sudo test -f "$NGINX_MAIN" && sudo grep -qE '^\s*listen\s+80\s*;' "$NGINX_MAIN"; then
  ts=$(date +%Y%m%d%H%M%S)
  sudo cp -a "$NGINX_MAIN" "$NGINX_MAIN.bak.$ts"
  # Comment every line of the first `server { ... }` block that contains a
  # bare `listen 80;` (the stock default). awk tracks brace depth so we only
  # touch that one block and leave the rest of nginx.conf intact.
  sudo awk '
    BEGIN{inblk=0; depth=0}
    /^[[:space:]]*server[[:space:]]*\{/ && inblk==0 {
      # peek: start buffering; decide once we know it is the default block
      inblk=1; depth=1; buf=$0 ORS; isdef=0; next
    }
    inblk==1 {
      buf=buf $0 ORS
      n=gsub(/\{/,"{"); depth+=n
      m=gsub(/\}/,"}"); depth-=m
      if ($0 ~ /listen[[:space:]]+80[[:space:]]*;/) isdef=1
      if (depth<=0) {
        if (isdef) { gsub(/\n/,"\n# ",buf); sub(/^/,"# ",buf) }
        printf "%s", buf
        inblk=0; buf=""
        next
      }
      next
    }
    { print }
  ' "$NGINX_MAIN" | sudo tee "$NGINX_MAIN.new" >/dev/null
  if sudo nginx -t -c "$NGINX_MAIN.new" 2>/dev/null; then
    sudo mv "$NGINX_MAIN.new" "$NGINX_MAIN"
    ok "Commented out the stock default server block (backup: nginx.conf.bak.$ts)."
  else
    sudo rm -f "$NGINX_MAIN.new"
    warn "Skipped nginx default-block edit (test of modified config failed; original left untouched)."
  fi
fi

# 7d. Restart the app and reload nginx.
sudo systemctl restart "$SERVICE" 2>/dev/null && ok "$SERVICE restarted." || warn "Could not restart $SERVICE."
sudo nginx -t 2>/dev/null && sudo systemctl reload nginx 2>/dev/null && ok "nginx reloaded." || warn "nginx reload skipped (config test failed)."

# 7e. Re-run the smoke test.
hr
say "${BOLD}== Post-fix smoke test (Host: $DOMAIN) ==${RESET}"
for path in /healthz/ / ; do
  code=$(smoke "$path")
  case "$code" in
    2*)    ok  "GET $path -> HTTP $code" ;;
    *)     bad "GET $path -> HTTP $code (still failing - inspect: sudo journalctl -u $SERVICE -n 50)" ;;
  esac
done
