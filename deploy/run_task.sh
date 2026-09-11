#!/usr/bin/env bash
#
# Run a long iotdash management command DECOUPLED from the terminal, as a
# transient systemd unit. It keeps running after you log out / close the
# laptop, logs to journald, and can be checked or stopped later.
#
#   sudo bash deploy/run_task.sh sync             # sync_ise
#   sudo bash deploy/run_task.sh restamp          # restamp_sites --events
#   sudo bash deploy/run_task.sh both             # sync, then restamp (chained)
#   sudo bash deploy/run_task.sh status [name]    # status (all, or one)
#   sudo bash deploy/run_task.sh logs <name>      # follow live logs
#   sudo bash deploy/run_task.sh stop <name>      # stop a running job
#   sudo bash deploy/run_task.sh list             # list iotdash jobs
#
# Requires root (system-level systemd-run); it runs the job AS the iotdash user.
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/iotdash}"
PY="$APP_DIR/.venv/bin/python"
RUN_AS="${RUN_AS:-iotdash}"
ENV_FILE="$APP_DIR/.env.prod"
PREFIX="iotdash-job"

need_root() {
  [ "$(id -u)" -eq 0 ] || { echo "Run with sudo (needs system systemd-run)."; exit 1; }
}

start() {  # $1 short-name ; $2.. the shell command to run
  local name="$1"; shift
  local unit="${PREFIX}-${name}"
  systemctl reset-failed "$unit" 2>/dev/null || true
  systemd-run --unit="$unit" --description="iotdash $name" \
    -p "User=$RUN_AS" -p "Group=$RUN_AS" \
    -p "WorkingDirectory=$APP_DIR" -p "EnvironmentFile=$ENV_FILE" \
    /bin/bash -lc "$*"
  echo "Started $unit — detached; keeps running if you disconnect."
  echo "   logs:   sudo bash $0 logs $name"
  echo "   status: sudo bash $0 status $name"
  echo "   stop:   sudo bash $0 stop $name"
}

case "${1:-}" in
  sync)
    need_root
    start sync "$PY $APP_DIR/manage.py sync_ise"
    ;;
  fast)
    need_root
    start fast "$PY $APP_DIR/manage.py sync_iot_fast"
    ;;
  fast-add)
    need_root
    start fastadd "$PY $APP_DIR/manage.py sync_iot_fast --additive"
    ;;
  authzrule)
    need_root
    start authzrule "$PY $APP_DIR/manage.py sync_iot_authz_rule"
    ;;
  authzrule-add)
    need_root
    start authzruleadd "$PY $APP_DIR/manage.py sync_iot_authz_rule --additive"
    ;;
  restamp)
    need_root
    start restamp "$PY $APP_DIR/manage.py restamp_sites --events"
    ;;
  reenrich)
    need_root
    start reenrich "$PY $APP_DIR/manage.py reenrich_events"
    ;;
  purge)
    need_root
    start purge "$PY $APP_DIR/manage.py purge_events --days ${2:-7}"
    ;;
  rebaseline)
    need_root
    # TRUE full replace (manual): authz-rule sync + FORCED prune (guard off),
    # then re-map ALL stored events onto the new baseline, in one detached job.
    # The hourly scheduler instead does a guarded prune + delta-only re-map.
    start rebaseline "$PY $APP_DIR/manage.py sync_iot_authz_rule --prune --prune-guard 0 && \
                      $PY $APP_DIR/manage.py reenrich_events"
    ;;
  both)
    need_root
    # chain: restamp runs only if sync succeeds, in one detached job
    start both "$PY $APP_DIR/manage.py sync_ise && \
                $PY $APP_DIR/manage.py restamp_sites --events"
    ;;
  status)
    if [ -n "${2:-}" ]; then systemctl status "${PREFIX}-$2" --no-pager
    else systemctl list-units "${PREFIX}-*" --all --no-pager; fi
    ;;
  logs)
    journalctl -u "${PREFIX}-${2:?name required (sync|restamp|both)}" -f
    ;;
  stop)
    systemctl stop "${PREFIX}-${2:?name required}"; echo "stopped ${PREFIX}-$2"
    ;;
  list)
    systemctl list-units "${PREFIX}-*" --all --no-pager
    ;;
  *)
    echo "usage: sudo bash $0 {sync|fast|fast-add|authzrule|authzrule-add|reenrich|rebaseline|purge [days]|restamp|both|status [name]|logs <name>|stop <name>|list}"
    exit 1
    ;;
esac
