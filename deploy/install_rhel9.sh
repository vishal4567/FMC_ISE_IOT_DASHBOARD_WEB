#!/usr/bin/env bash
# Install the IoT Security Dashboard on RHEL 9.x.
# Run as a sudo-capable user from the app directory (default /opt/iotdash).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/iotdash}"
DB_NAME="${POSTGRES_DB:-iotdash}"
DB_USER="${POSTGRES_USER:-iotdash}"
DB_PASS="${POSTGRES_PASSWORD:-CHANGE_ME}"

# ---------------------------------------------------------------------------
# PostgreSQL source (Django 5 needs PG >= 14):
#   PG_SOURCE=module (default) - RHEL AppStream module stream. Needs the stream
#                                mirrored on your Satellite (PG_STREAM, default 16).
#   PG_SOURCE=pgdg             - PostgreSQL's own repo (yum.postgresql.org). Needs
#                                internet to that host (PG_MAJOR, default 16).
# ---------------------------------------------------------------------------
PG_SOURCE="${PG_SOURCE:-module}"
PG_STREAM="${PG_STREAM:-16}"
PG_MAJOR="${PG_MAJOR:-16}"

echo "==> Installing base packages (dnf)"
sudo dnf -y install python3.11 python3.11-pip redis nginx \
    policycoreutils-python-utils gcc

if [ "${PG_SOURCE}" = "pgdg" ]; then
  echo "==> PostgreSQL ${PG_MAJOR} from PGDG (yum.postgresql.org)"
  sudo dnf -y install \
    "https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm"
  sudo dnf -qy module disable postgresql || true
  sudo dnf -y install "postgresql${PG_MAJOR}-server" "postgresql${PG_MAJOR}-contrib"
  PGSVC="postgresql-${PG_MAJOR}"
  PGDATA="/var/lib/pgsql/${PG_MAJOR}/data"
  PSQL="/usr/pgsql-${PG_MAJOR}/bin/psql"
  [ -s "${PGDATA}/PG_VERSION" ] || \
    sudo "/usr/pgsql-${PG_MAJOR}/bin/postgresql-${PG_MAJOR}-setup" initdb
else
  echo "==> PostgreSQL ${PG_STREAM} from the RHEL AppStream module"
  sudo dnf -y module reset postgresql || true
  sudo dnf -y module enable "postgresql:${PG_STREAM}" || true
  sudo dnf -y install postgresql-server postgresql-contrib
  PGSVC="postgresql"
  PGDATA="/var/lib/pgsql/data"
  PSQL="psql"
  [ -s "${PGDATA}/PG_VERSION" ] || sudo postgresql-setup --initdb
fi

echo "==> Enable + start services (${PGSVC}, redis, nginx)"
sudo systemctl enable --now "${PGSVC}" redis nginx

echo "==> Create database + user"
# Run psql from a dir the postgres user can enter, else it warns
# "could not change directory to /opt/iotdash" (harmless, but noisy).
cd /tmp
# Create the role if missing; always (re)set its password to match .env.prod so
# a leftover role from a previous build can't cause a password mismatch.
if sudo -u postgres ${PSQL} -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
  sudo -u postgres ${PSQL} -c "ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"
else
  sudo -u postgres ${PSQL} -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"
fi
sudo -u postgres ${PSQL} -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
  sudo -u postgres ${PSQL} -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

echo "==> Enable password auth for local TCP (pg_hba.conf)"
# RHEL's default pg_hba.conf uses 'ident' for 127.0.0.1/::1, which rejects
# Django's password login ("Ident authentication failed"). Switch the local TCP
# rules to md5 (md5 also transparently accepts scram-stored passwords on PG10+).
# SHOW hba_file makes this work for either install path (module or PGDG data dir).
HBA="$(sudo -u postgres ${PSQL} -tAc 'SHOW hba_file;')"
sudo cp "${HBA}" "${HBA}.bak.$(date +%s)"
sudo sed -ri 's#^(host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1/32|::1/128)[[:space:]]+)(ident|peer|scram-sha-256)#\1md5#' "${HBA}"
sudo systemctl reload "${PGSVC}"

echo "==> App user + virtualenv"
id iotdash &>/dev/null || sudo useradd -r -m -d "${APP_DIR}" iotdash
sudo chown -R iotdash:iotdash "${APP_DIR}"
sudo -u iotdash python3.11 -m venv "${APP_DIR}/.venv"
sudo -u iotdash "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u iotdash "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements-prod.txt"

echo "==> Migrate + collectstatic"
cd "${APP_DIR}"
# settings.py auto-loads .env.prod via python-dotenv, so NO fragile `source`
# (which breaks on passwords containing special shell characters).
sudo -u iotdash "${APP_DIR}/.venv/bin/python" "${APP_DIR}/manage.py" migrate --noinput
sudo -u iotdash "${APP_DIR}/.venv/bin/python" "${APP_DIR}/manage.py" collectstatic --noinput

echo "==> SELinux: allow nginx to proxy to gunicorn"
sudo setsebool -P httpd_can_network_connect 1

echo "==> firewalld: open HTTP/HTTPS"
sudo firewall-cmd --permanent --add-service=http --add-service=https || true
sudo firewall-cmd --reload || true

echo "==> systemd services"
sudo cp "${APP_DIR}/deploy/systemd/"*.service /etc/systemd/system/
sudo cp "${APP_DIR}/deploy/nginx-iotdash.conf" /etc/nginx/conf.d/iotdash.conf
sudo systemctl daemon-reload
sudo systemctl enable --now iotdash-web iotdash-worker iotdash-beat
sudo systemctl restart nginx

echo "==> Done. Enable the eStreamer ingester once eNcore is configured:"
echo "    sudo systemctl enable --now iotdash-estreamer"
echo "Dashboard: http://<host>/   (put TLS in front for production)"
