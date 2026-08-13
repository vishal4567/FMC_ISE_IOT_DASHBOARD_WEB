#!/usr/bin/env bash
#
# cleanup.sh — purge leftover remnants of this app from a host.
#
# Safe by default: prints what it WOULD remove (dry-run). Nothing is deleted
# unless you pass --yes. Destructive data deletion (Postgres/volumes) needs the
# separate --data flag. Secrets (.env.prod, *.pkcs12/pem/key) are NEVER touched.
#
# Usage:
#   bash deploy/cleanup.sh [scopes] [--yes] [--data]
#
# Scopes (default = --artifacts only):
#   --docker      stop/remove the compose stack + iotdash images, prune dangling
#   --systemd     stop/disable/remove iotdash-* systemd units
#   --artifacts   pyc/__pycache__, staticfiles, captures, probe output, legacy db
#   --encore      eNcore runtime state (bookmarks/cache/logs) — keeps cert + conf
#   --cache       flush the app's Redis DB (cache + Celery broker)
#   --all         docker + systemd + artifacts + encore + cache  (NOT --data)
#   --data        DESTRUCTIVE: delete Postgres data (compose volume / PGDATA)
#   --yes         actually perform the actions (otherwise dry-run)
#
# Examples:
#   bash deploy/cleanup.sh --all                 # preview a full cleanup
#   bash deploy/cleanup.sh --all --yes           # do it (keeps the database)
#   bash deploy/cleanup.sh --docker --data --yes # also wipe the DB volume
set -u

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
ENCORE_DIR="${ENCORE_DIR:-/opt/eStreamer-eNcore}"
DRY=1; DO_DOCKER=0; DO_SYSTEMD=0; DO_ARTIFACTS=0; DO_ENCORE=0; DO_CACHE=0; DO_DATA=0
ANY_SCOPE=0

for a in "$@"; do
  case "$a" in
    --yes) DRY=0 ;;
    --docker)    DO_DOCKER=1;    ANY_SCOPE=1 ;;
    --systemd)   DO_SYSTEMD=1;   ANY_SCOPE=1 ;;
    --artifacts) DO_ARTIFACTS=1; ANY_SCOPE=1 ;;
    --encore)    DO_ENCORE=1;    ANY_SCOPE=1 ;;
    --cache)     DO_CACHE=1;     ANY_SCOPE=1 ;;
    --data)      DO_DATA=1 ;;
    --all) DO_DOCKER=1; DO_SYSTEMD=1; DO_ARTIFACTS=1; DO_ENCORE=1; DO_CACHE=1; ANY_SCOPE=1 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (see --help)"; exit 2 ;;
  esac
done
[ "$ANY_SCOPE" -eq 0 ] && DO_ARTIFACTS=1   # default scope

say()  { printf '  %s\n' "$*"; }
run()  { if [ "$DRY" -eq 1 ]; then echo "  DRY  $*"; else echo "  RUN  $*"; eval "$@"; fi; }
have() { command -v "$1" >/dev/null 2>&1; }
hdr()  { echo; echo "== $* =="; }

echo "cleanup.sh  (APP_DIR=$APP_DIR)"
[ "$DRY" -eq 1 ] && echo "MODE: dry-run (add --yes to apply)" || echo "MODE: APPLY"

# --- Docker ---------------------------------------------------------------
if [ "$DO_DOCKER" -eq 1 ]; then
  hdr "Docker"
  if have docker; then
    if [ -f "$APP_DIR/docker-compose.yml" ]; then
      if [ "$DO_DATA" -eq 1 ]; then
        run "docker compose -f '$APP_DIR/docker-compose.yml' --profile estreamer down -v --remove-orphans"
      else
        run "docker compose -f '$APP_DIR/docker-compose.yml' --profile estreamer down --remove-orphans"
        say "(kept named volumes incl. pgdata — add --data to remove them)"
      fi
    fi
    run "docker image rm -f iotdash:latest iotdash-encore:latest 2>/dev/null || true"
    run "docker image prune -f"
    run "docker builder prune -f"
  else
    say "docker not installed — skipping"
  fi
fi

# --- systemd --------------------------------------------------------------
if [ "$DO_SYSTEMD" -eq 1 ]; then
  hdr "systemd units"
  if have systemctl; then
    for u in iotdash-web iotdash-worker iotdash-beat iotdash-estreamer; do
      if systemctl list-unit-files 2>/dev/null | grep -q "^${u}.service"; then
        run "sudo systemctl disable --now ${u}.service 2>/dev/null || true"
        run "sudo rm -f /etc/systemd/system/${u}.service"
      else
        say "${u}: not installed"
      fi
    done
    run "sudo systemctl daemon-reload"
    run "sudo systemctl reset-failed 2>/dev/null || true"
  else
    say "systemctl not present — skipping"
  fi
fi

# --- Build / runtime artifacts (safe; never secrets or source) ------------
if [ "$DO_ARTIFACTS" -eq 1 ]; then
  hdr "Artifacts in $APP_DIR"
  run "find '$APP_DIR' -type d -name __pycache__ -not -path '*/.venv/*' -prune -exec rm -rf {} + 2>/dev/null || true"
  run "find '$APP_DIR' -type f -name '*.py[co]' -not -path '*/.venv/*' -delete 2>/dev/null || true"
  run "rm -rf '$APP_DIR/staticfiles'"
  run "rm -rf '$APP_DIR/api_out'"
  run "rm -f  '$APP_DIR/api_responses.json' '$APP_DIR/device_types.json'"
  run "rm -f  '$APP_DIR'/*.jsonl '$APP_DIR'/encore-sample*"
  run "rm -f  '$APP_DIR/db.sqlite3'"          # legacy POC DB (prod is Postgres)
  say "(kept: .env.prod, *.pkcs12/*.pem/*.key, source, .venv)"
fi

# --- eNcore runtime state (keeps cert + estreamer.conf) -------------------
if [ "$DO_ENCORE" -eq 1 ]; then
  hdr "eNcore state in $ENCORE_DIR"
  if [ -d "$ENCORE_DIR" ]; then
    run "sudo rm -f '$ENCORE_DIR'/*_bookmark.dat '$ENCORE_DIR'/*.log '$ENCORE_DIR'/*_pkcs.key '$ENCORE_DIR'/*_pkcs.cert 2>/dev/null || true"
    run "sudo rm -rf '$ENCORE_DIR'/cache '$ENCORE_DIR'/data 2>/dev/null || true"
    say "(kept: client.pkcs12, estreamer.conf, encore code)"
  else
    say "$ENCORE_DIR not present — skipping"
  fi
fi

# --- Redis app cache / broker --------------------------------------------
if [ "$DO_CACHE" -eq 1 ]; then
  hdr "Redis cache/broker"
  if have redis-cli; then
    run "redis-cli -n 0 FLUSHDB"
  elif have docker && [ -f "$APP_DIR/docker-compose.yml" ]; then
    run "docker compose -f '$APP_DIR/docker-compose.yml' exec -T redis redis-cli FLUSHALL 2>/dev/null || true"
  else
    say "no redis-cli / redis container — skipping"
  fi
fi

# --- Postgres data (destructive; only outside Docker — compose handled above)
if [ "$DO_DATA" -eq 1 ] && [ "$DO_DOCKER" -eq 0 ]; then
  hdr "Postgres data (DESTRUCTIVE)"
  say "Refusing to auto-drop a system Postgres. To wipe the app DB manually:"
  say "  sudo -u postgres psql -c \"DROP DATABASE IF EXISTS iotdash;\""
  say "  sudo -u postgres psql -c \"CREATE DATABASE iotdash OWNER iotdash;\""
fi

echo
if [ "$DRY" -eq 1 ]; then
  echo "Dry-run complete. Re-run with --yes to apply."
else
  echo "Cleanup applied."
fi
