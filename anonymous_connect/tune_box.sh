#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# tune_box.sh - machine-level tuning for the Anonymous Connect EC2 box.
#
# Target: Amazon Linux 2023, ~913 MB RAM / 2 vCPU (1 core, 2 threads), with
# Postgres + Redis + Daphne + Nginx all co-located on this one instance.
#
# What it does (all idempotent, safe to re-run):
#   1. Lowers vm.swappiness so hot WebSocket/matchmaking pages stay in RAM and
#      only genuinely cold memory is swapped (swap stays a safety net, not a
#      capacity crutch).
#   2. Caps Redis memory so the channel layer can never balloon and trigger the
#      OOM killer; evicts least-recently-used keys instead.
#   3. Tunes Postgres down to a footprint that fits a small shared box.
#   4. Optionally grows swap to 4 GB as crash insurance (set GROW_SWAP=1).
#
# It changes NOTHING destructive: no data is dropped, services are only
# reloaded/restarted. Run as a user with sudo:  ./tune_box.sh
# ---------------------------------------------------------------------------
set -euo pipefail

GROW_SWAP=${GROW_SWAP:-0}          # set to 1 to also grow swap to 4 GB
SWAPPINESS=${SWAPPINESS:-10}
REDIS_MAXMEM=${REDIS_MAXMEM:-128mb}

log() { echo "==> $*"; }

# ---------------------------------------------------------------------------
# 1. Kernel: swappiness. Keep hot pages resident; swap only cold memory.
# ---------------------------------------------------------------------------
log "[1/4] Setting vm.swappiness=$SWAPPINESS (persistent)"
sudo sysctl -w vm.swappiness="$SWAPPINESS" >/dev/null
echo "vm.swappiness=$SWAPPINESS" | sudo tee /etc/sysctl.d/99-anonymous-connect.conf >/dev/null
# A touch of extra cache pressure reclaim headroom on a tiny box.
sudo sysctl -w vm.vfs_cache_pressure=50 >/dev/null
echo "vm.vfs_cache_pressure=50" | sudo tee -a /etc/sysctl.d/99-anonymous-connect.conf >/dev/null

# ---------------------------------------------------------------------------
# 2. Redis: hard memory cap + LRU eviction so it can't OOM the box.
#    The channel layer only holds short-lived signalling/group data, so a small
#    cap is plenty and eviction is harmless here.
# ---------------------------------------------------------------------------
log "[2/4] Capping Redis memory at $REDIS_MAXMEM with allkeys-lru eviction"
# Locate the redis config (Amazon Linux 2023 ships 'redis6').
REDIS_CONF=""
for c in /etc/redis6/redis6.conf /etc/redis/redis.conf /etc/redis.conf; do
  if [ -f "$c" ]; then REDIS_CONF="$c"; break; fi
done
if [ -n "$REDIS_CONF" ]; then
  # Replace or append the two directives idempotently.
  sudo sed -i '/^\s*maxmemory\s/d; /^\s*maxmemory-policy\s/d' "$REDIS_CONF"
  printf '\n# --- tuned by tune_box.sh ---\nmaxmemory %s\nmaxmemory-policy allkeys-lru\n' \
    "$REDIS_MAXMEM" | sudo tee -a "$REDIS_CONF" >/dev/null
  # Apply live too (no restart needed), then restart to be safe.
  REDIS_CLI=$(command -v redis6-cli || command -v redis-cli || true)
  if [ -n "$REDIS_CLI" ]; then
    "$REDIS_CLI" CONFIG SET maxmemory "$REDIS_MAXMEM" >/dev/null 2>&1 || true
    "$REDIS_CLI" CONFIG SET maxmemory-policy allkeys-lru >/dev/null 2>&1 || true
  fi
  # Restart whichever redis unit exists.
  for u in redis6 redis redis-server; do
    if systemctl list-unit-files | grep -q "^$u\.service"; then
      sudo systemctl restart "$u" && break
    fi
  done
else
  echo "WARNING: no redis config found; skipping Redis cap." >&2
fi

# ---------------------------------------------------------------------------
# 3. Postgres: shrink to fit a small shared box. Defaults assume a bigger host.
# ---------------------------------------------------------------------------
log "[3/4] Tuning Postgres for a small shared box"
PG_CONF=$(sudo -u postgres psql -tAc 'SHOW config_file;' 2>/dev/null || true)
if [ -z "$PG_CONF" ]; then
  # Fall back to common Amazon Linux 2023 locations.
  for c in /var/lib/pgsql/data/postgresql.conf /var/lib/pgsql/*/data/postgresql.conf; do
    if [ -f "$c" ]; then PG_CONF="$c"; break; fi
  done
fi
if [ -n "$PG_CONF" ] && [ -f "$PG_CONF" ]; then
  set_pg() {
    local key="$1" val="$2"
    sudo sed -i "/^\s*#\?\s*${key}\s*=/d" "$PG_CONF"
    echo "${key} = ${val}" | sudo tee -a "$PG_CONF" >/dev/null
  }
  echo "    editing $PG_CONF"
  set_pg shared_buffers 128MB
  set_pg effective_cache_size 256MB
  set_pg work_mem 4MB
  set_pg maintenance_work_mem 32MB
  set_pg max_connections 40
  # Reload (most of these need a restart; shared_buffers/max_connections do).
  for u in postgresql postgresql16 postgresql15; do
    if systemctl list-unit-files | grep -q "^$u\.service"; then
      sudo systemctl restart "$u" && break
    fi
  done
else
  echo "WARNING: could not locate postgresql.conf; skipping Postgres tuning." >&2
fi

# ---------------------------------------------------------------------------
# 4. (Optional) grow swap to 4 GB as crash insurance.
# ---------------------------------------------------------------------------
if [ "$GROW_SWAP" = "1" ]; then
  log "[4/4] Growing swap to 4 GB (/swapfile2)"
  if [ ! -f /swapfile2 ]; then
    sudo fallocate -l 4G /swapfile2 || sudo dd if=/dev/zero of=/swapfile2 bs=1M count=4096
    sudo chmod 600 /swapfile2
    sudo mkswap /swapfile2 >/dev/null
    sudo swapon /swapfile2
    if ! grep -q '/swapfile2' /etc/fstab; then
      echo '/swapfile2 none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    fi
  else
    echo "    /swapfile2 already exists; leaving it as-is."
  fi
else
  log "[4/4] Skipping swap growth (set GROW_SWAP=1 to enable)"
fi

echo
log "Done. Current memory / swap state:"
free -m
echo
echo "swappiness = $(cat /proc/sys/vm/swappiness)"
echo "Review the services above started cleanly:"
echo "  systemctl status anonymous_connect nginx"
for u in redis6 redis postgresql postgresql16; do
  systemctl is-active "$u" >/dev/null 2>&1 && echo "  $u: active"
done
