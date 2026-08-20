#!/usr/bin/env bash
#
# Reconcile .env.prod with the current app:
#   * comment out variables whose name appears NOWHERE in the app's Python
#     (i.e. the app no longer reads them - junk), prefixed "# [unused] "
#   * add current/new variables that are missing (with safe defaults)
#
# Existing values are preserved; a timestamped backup is written first.
# Idempotent - safe to run repeatedly.
#
#   bash deploy/reconcile_env.sh                 # /opt/iotdash/.env.prod
#   APP_DIR=/opt/iotdash bash deploy/reconcile_env.sh /path/to/.env.prod
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/iotdash}"
ENV_FILE="${1:-$APP_DIR/.env.prod}"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found"; exit 1; }
[ -d "$APP_DIR" ]  || { echo "ERROR: APP_DIR $APP_DIR not found"; exit 1; }

BACKUP="${ENV_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$ENV_FILE" "$BACKUP"
echo "Backup written: $BACKUP"

# 1) Every UPPER_CASE token that appears in the app's Python = a name the code
#    may read. A var is "junk" only if its key appears nowhere here (safe: we
#    never comment a variable the code references).
USED_FILE="$(mktemp)"
grep -rhoE '"[A-Z][A-Z0-9_]{2,}"' "$APP_DIR" --include='*.py' 2>/dev/null \
  | tr -d '"' | sort -u > "$USED_FILE"

# 2) Walk the file: comment out active vars whose key is not used anywhere.
TMP="$(mktemp)"; commented=0
while IFS= read -r line || [ -n "$line" ]; do
  if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
    key="${BASH_REMATCH[1]}"
    if grep -qxF "$key" "$USED_FILE"; then
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

# 3) Ensure current/new vars exist (skip if already present, active or commented).
added=0
add_var() {  # name  default  [comment]
  local name="$1" def="$2" cmt="${3:-}"
  if grep -qE "^[[:space:]]*#?[[:space:]]*${name}=" "$TMP"; then return; fi
  [ -n "$cmt" ] && printf '# %s\n' "$cmt" >> "$TMP"
  printf '%s=%s\n' "$name" "$def" >> "$TMP"
  echo "  added: $name"
  added=$((added + 1))
}

printf '\n# ===== reconcile_env.sh additions (%s) =====\n' "$(date +%F)" >> "$TMP"

# --- Admin Config page (⚙ Config) login ---
add_var DASHBOARD_ADMIN_USER     admin "Config page login user"
add_var DASHBOARD_ADMIN_PASSWORD ""    "Set to require login on the Config page; blank = open"

# --- IoT discovery by AUTHORIZATION profile (RADIUS summary) ---
add_var ISE_DC_IOT_BY_AUTHZ          False "Discover IoT by authz profile containing a token"
add_var ISE_DC_AUTHZ_MATCH           IOT
add_var ISE_DC_COL_AUTHZ             authorization_profiles
add_var ISE_DC_AUTHZ_COL_PROFILE     endpoint_profile
add_var ISE_DC_AUTHZ_COL_DEVICETYPE  device_type
add_var ISE_DC_AUTHZ_COL_IP          "" "Blank: RADIUS summary has no endpoint IP (backfilled from endpoints_data)"

# --- Location from NAD hostname (site-code map, editable on /config) ---
add_var ISE_DC_LOCATION_BY_NAD_HOSTNAME False "Resolve site from NAD hostname"
add_var ISE_DC_ND_VIEW      network_devices
add_var ISE_DC_ND_NAME_COL  name
add_var ISE_DC_ND_IP_COL    ip_mask
add_var ISE_DC_COL_NAS_IP   nas_ip_address

mv "$TMP" "$ENV_FILE"
rm -f "$USED_FILE"
echo
echo "Done: commented $commented unused var(s), added $added new var(s)."
echo "Review $ENV_FILE   (backup: $BACKUP)."
echo "Nothing is deleted - '# [unused]' lines are just commented; restore from backup if needed."
