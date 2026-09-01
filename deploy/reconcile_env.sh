#!/usr/bin/env bash
#
# Reconcile + configure .env.prod:
#   * comment out variables the app no longer reads (junk) as "# [unused] ..."
#     - EXCEPT proxy vars (HTTP_PROXY/HTTPS_PROXY/NO_PROXY/...) and anything in
#       KEEP_ALWAYS, which are system/infra settings and always preserved
#   * set the current pipeline variables to their intended values (add if
#     missing, update in place, uncomment if commented)
#
# Existing values for anything not in the SET list are preserved; secrets are
# never overwritten. A timestamped backup is written first. Idempotent.
#
#   bash deploy/reconcile_env.sh                 # /opt/iotdash/.env.prod
#   APP_DIR=/opt/iotdash bash deploy/reconcile_env.sh /path/to/.env.prod
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/iotdash}"
ENV_FILE="${1:-$APP_DIR/.env.prod}"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found"; exit 1; }
[ -d "$APP_DIR" ]  || { echo "ERROR: APP_DIR $APP_DIR not found"; exit 1; }

# Names never commented even if absent from the app's Python (infra/system).
KEEP_ALWAYS="DJANGO_SECRET_KEY EVENT_BACKEND"
# ...and anything whose name matches this (case-insensitive) - proxy settings.
KEEP_REGEX='PROXY'

BACKUP="${ENV_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$ENV_FILE" "$BACKUP"
echo "Backup written: $BACKUP"

# 1) Names the app reads (any UPPER token in its Python) = not junk.
USED="$(mktemp)"
grep -rhoE '"[A-Z][A-Z0-9_]{2,}"' "$APP_DIR" --include='*.py' 2>/dev/null \
  | tr -d '"' | sort -u > "$USED"
for k in $KEEP_ALWAYS; do echo "$k"; done >> "$USED"
sort -u -o "$USED" "$USED"

is_keep() {  # $1 = KEY -> keep (don't comment) if used or proxy
  grep -qxF "$1" "$USED" && return 0
  printf '%s' "$1" | grep -qiE "$KEEP_REGEX" && return 0
  return 1
}

# 2) Comment out active vars that aren't kept.
TMP="$(mktemp)"; commented=0
while IFS= read -r line || [ -n "$line" ]; do
  if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
    key="${BASH_REMATCH[1]}"
    if is_keep "$key"; then
      printf '%s\n' "$line" >> "$TMP"
    else
      printf '# [unused] %s\n' "$line" >> "$TMP"
      echo "  commented (unused): $key"
      commented=$((commented + 1))
    fi
  else
    printf '%s\n' "$line" >> "$TMP"
  fi
done < "$ENV_FILE"
mv "$TMP" "$ENV_FILE"
rm -f "$USED"

# 3) set_var: add or update KEY=VALUE in place (uncomments a commented one).
set_var() {  # $1 KEY  $2 VALUE
  local k="$1" v="$2" found=0 t; t="$(mktemp)"
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ $found -eq 0 && "$line" =~ ^[[:space:]]*#?[[:space:]]*${k}= ]]; then
      printf '%s=%s\n' "$k" "$v" >> "$t"; found=1
    else
      printf '%s\n' "$line" >> "$t"
    fi
  done < "$ENV_FILE"
  [ $found -eq 0 ] && printf '%s=%s\n' "$k" "$v" >> "$t"
  mv "$t" "$ENV_FILE"
  echo "  set: $k=$v"
}
# add_if_missing: only add when absent (never overwrites an existing value).
add_if_missing() {  # $1 KEY  $2 DEFAULT
  grep -qE "^[[:space:]]*#?[[:space:]]*$1=" "$ENV_FILE" || set_var "$1" "$2"
}

echo "Applying current pipeline configuration:"

# ---- IoT discovery by LOGICAL PROFILE (union: named list + IOT-in-name) ----
set_var ISE_DC_IOT_BY_LOGICAL      True
set_var ISE_IOT_LOGICAL_PROFILES   "Wipro_CCTV,Wipro-BMS,Wipro-Access-Control"
set_var ISE_DC_LOGICAL_MATCH       IOT
set_var ISE_ADDITIVE_SYNC          True
set_var ISE_DC_IOT_BY_AUTHZ        False

# ---- Location from the LATEST RADIUS device_name ----
set_var ISE_DC_LOCATION_BY_NAD_HOSTNAME True
set_var ISE_DC_LOC_HOST_COL        device_name
set_var ISE_DC_COL_LOC_TIME        timestamp

# ---- Admin Config page login (password never overwritten) ----
add_if_missing DASHBOARD_ADMIN_USER     admin
add_if_missing DASHBOARD_ADMIN_PASSWORD ""

echo
echo "Done: commented $commented unused var(s); pipeline vars set."
echo "Review $ENV_FILE   (backup: $BACKUP)."
echo "Proxy/NO_PROXY vars were preserved. Edit ISE_IOT_LOGICAL_PROFILES if your"
echo "logical-profile names differ, and set DASHBOARD_ADMIN_PASSWORD to enable login."
