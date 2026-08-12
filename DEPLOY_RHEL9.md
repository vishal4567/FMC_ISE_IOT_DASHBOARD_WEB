# Deploy on RHEL 9.8

Production install of the IoT Security Dashboard (Cisco ISE + FMC) on Red Hat
Enterprise Linux 9.8. Data path: **ISE/FMC REST → config**, **FMC eStreamer →
events**, all stored in PostgreSQL and served by Django/gunicorn behind nginx.

## 0. Prerequisites
- RHEL 9.8, sudo access, outbound reachability to **ISE (443)**, **FMC (443)**,
  and **FMC eStreamer (8302)**.
- ISE **ERS enabled** + read-only account; FMC **REST API enabled** + read-only
  account (see [PREREQUISITES.md](PREREQUISITES.md)).
- FMC **eStreamer client pkcs12 certificate** (System → Integration → eStreamer →
  select event types → Create Client → Download Certificate).
- Cisco **eNcore** collector (github.com/CiscoDevNet/eStreamer-eNcore) at
  `/opt/eStreamer-eNcore`, configured with that cert.

## 1. Place the code + config
```bash
sudo mkdir -p /opt/iotdash && sudo chown $USER /opt/iotdash
# copy this folder's contents into /opt/iotdash  (git clone / rsync / scp)
cd /opt/iotdash
cp .env.prod.example .env.prod          # then edit: DB pw, secret key, ISE/FMC creds
```
Minimum `.env.prod`:
```ini
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<long-random>
DJANGO_ALLOWED_HOSTS=dashboard.example.com
POSTGRES_HOST=localhost
POSTGRES_DB=iotdash
POSTGRES_USER=iotdash
POSTGRES_PASSWORD=<strong>
REDIS_URL=redis://localhost:6379/0
ISE_HOST=... ISE_USERNAME=... ISE_PASSWORD=... ISE_ERS_PORT=443 ISE_VERIFY_TLS=True
FMC_HOST=... FMC_USERNAME=... FMC_PASSWORD=... FMC_VERIFY_TLS=True
RETENTION_THREAT_DAYS=90
RETENTION_CONNECTION_DAYS=14
```

## 2. Install (one script)
```bash
POSTGRES_PASSWORD='<strong>' sudo -E bash deploy/install_rhel9.sh
```
It installs Python 3.11 / PostgreSQL / Redis / nginx, creates the DB + app user +
venv, runs `migrate` and `collectstatic`, sets the SELinux boolean
(`httpd_can_network_connect`), opens firewalld HTTP/HTTPS, and enables the
**web / worker / beat** systemd services.

## 3. Validate the APIs before going live
```bash
sudo -u iotdash bash -c 'set -a; source .env.prod; set +a; .venv/bin/python manage.py probe_apis --out api_responses.json'
```
Review `api_responses.json` (status/timing + raw samples). Send it over to tune
the client field mappings if anything doesn't parse.

## 4. Seed ISE inventory, then start events
```bash
# populate IoTDevice from ISE (also runs every 15 min via beat)
sudo -u iotdash .venv/bin/python -c "import django,os;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';django.setup();from dashboard.tasks import poll_ise_inventory;print(poll_ise_inventory())"

# start the eStreamer feed (eNcore -> ingester)
sudo systemctl enable --now iotdash-estreamer
```

## 5. Verify
```bash
systemctl status iotdash-web iotdash-worker iotdash-beat iotdash-estreamer
curl -sI http://localhost/ | head -1        # HTTP 200
journalctl -u iotdash-estreamer -f          # watch events flow in
```
Open `http://<host>/` (put TLS in front — uncomment the 443 block in
`deploy/nginx-iotdash.conf` and drop your cert in `/etc/pki/tls`).

## Notes
- **PostgreSQL version:** RHEL 9 ships PG via `dnf`. For TimescaleDB add its repo
  and `dnf install timescaledb-2-postgresql-<v>` (optional, for compression at
  high event volume).
- **SELinux** stays *enforcing*; only `httpd_can_network_connect` is toggled.
- **Scaling:** raise `gunicorn` workers, add worker replicas, add a PG read
  replica — see [docs/DEPLOYMENT_VM_SPEC.md](docs/DEPLOYMENT_VM_SPEC.md).
