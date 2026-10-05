#!/usr/bin/env bash
#
# Show progress of running iotdash background jobs (reenrich / sync / purge ...).
#
#   sudo bash deploy/task_progress.sh            # one snapshot
#   sudo bash deploy/task_progress.sh watch      # refresh every 5s
#   sudo bash deploy/task_progress.sh watch 10   # refresh every 10s
#
# Combines three signals: the job's own latest log line, live Postgres activity
# on the events table, and - for reenrich - the id cursor parsed from the running
# UPDATE compared to the table's id range (a true % even between log lines).
set -uo pipefail

DB="${PGDATABASE:-iotdash}"
# run psql from /tmp so "could not change directory to /opt/iotdash" never prints
psql_() { (cd /tmp && sudo -u postgres psql -d "$DB" -At -c "$1" 2>/dev/null); }

snapshot() {
  echo "================ $(date '+%Y-%m-%d %H:%M:%S') ================"

  echo "-- jobs --"
  systemctl list-units 'iotdash-job-*' --all --no-legend 2>/dev/null \
    | awk '{printf "   %-28s %s %s\n", $1, $3, $4}' || true

  echo "-- latest log line per job --"
  for u in $(systemctl list-units 'iotdash-job-*' --all --no-legend 2>/dev/null | awk '{print $1}'); do
    line=$(journalctl -u "$u" -n 1 --no-pager -o cat 2>/dev/null)
    printf "   %-24s %s\n" "${u#iotdash-job-}" "${line:-<no output yet>}"
  done

  echo "-- active DB work on events --"
  psql_ "SELECT '   pid '||pid||'  '||to_char(now()-query_start,'HH24:MI:SS')||'  '||
         left(regexp_replace(query,'\s+',' ','g'),66)
         FROM pg_stat_activity
         WHERE (query ILIKE '%dashboard_securityevent%' OR query ILIKE 'autovacuum%securityevent%')
           AND state='active' AND pid<>pg_backend_pid()
         ORDER BY query_start;" || echo "   (psql unavailable)"

  # reenrich id-cursor progress (parsed from the running UPDATE)
  cur=$(psql_ "SELECT substring(query from 'e\.id[ >=]+([0-9]+)')
               FROM pg_stat_activity
               WHERE query LIKE 'UPDATE dashboard_securityevent%' AND state='active'
               ORDER BY query_start LIMIT 1;")
  if [[ "$cur" =~ ^[0-9]+$ ]]; then
    range=$(psql_ "SELECT min(id)||' '||max(id) FROM dashboard_securityevent;")
    lo=${range% *}; hi=${range#* }
    if [[ "$hi" =~ ^[0-9]+$ ]] && (( hi > lo )); then
      pct=$(( 100 * (cur - lo) / (hi - lo) ))
      (( pct < 0 )) && pct=0; (( pct > 100 )) && pct=100
      printf -- "-- reenrich cursor: id ~%'d of [%'d .. %'d]  (~%d%%) --\n" "$cur" "$lo" "$hi" "$pct"
    fi
  fi
  echo
}

case "${1:-}" in
  watch)
    n="${2:-5}"
    while true; do clear 2>/dev/null; snapshot; sleep "$n"; done
    ;;
  *)
    snapshot
    ;;
esac
