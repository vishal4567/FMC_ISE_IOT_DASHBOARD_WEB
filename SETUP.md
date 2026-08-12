# SETUP — fresh RHEL 9.8 VM → running dashboard

Copy-paste runbook. Run every command as a **sudo-capable user**. For the *why*
behind each step, and eStreamer/TLS detail, see [SETUP_GUIDE.md](SETUP_GUIDE.md).

Repo: `https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB`

---

## 0. Entitle the VM (so `dnf` works)
On a fresh RHEL box `dnf install` fails until the system is registered.
**Check first — you may already be entitled (cloud RHUI image):**
```bash
sudo subscription-manager status && sudo dnf repolist   # if repos list, SKIP to step 1
```
If not registered, use your org's method (ask your RHEL/platform admin):
```bash
# enterprise / Satellite (preferred): activation key + org ID
sudo subscription-manager register --org <ORG_ID> --activationkey <KEY>
#   — or — individual Red Hat account:
sudo subscription-manager register            # prompts Red Hat portal username + password
sudo subscription-manager status              # expect: Overall Status: Current
```

## 1. Base OS prep
```bash
sudo dnf -y update
sudo dnf -y install git chrony
sudo hostnamectl set-hostname iotdash-prod
sudo systemctl enable --now chronyd
sudo timedatectl set-ntp true                 # correct time = correct event correlation
getenforce                                     # expect: Enforcing
sudo systemctl enable --now firewalld
```

## 2. Get the code
```bash
sudo mkdir -p /opt/iotdash && sudo chown "$USER" /opt/iotdash
git clone https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB.git /opt/iotdash
cd /opt/iotdash
```

## 3. Configure secrets
```bash
cp .env.prod.example .env.prod
chmod 600 .env.prod
vi .env.prod     # fill in the values below
```
Minimum to set in `.env.prod`:
```ini
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<64+ random chars>
DJANGO_ALLOWED_HOSTS=dashboard.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://dashboard.example.com

POSTGRES_HOST=localhost
POSTGRES_DB=iotdash
POSTGRES_USER=iotdash
POSTGRES_PASSWORD=<strong>
REDIS_URL=redis://localhost:6379/0

ISE_HOST=<ise-host>            # hostname/IP only
ISE_USERNAME=<ers-user>
ISE_PASSWORD=<ers-pass>
ISE_ERS_PORT=443              # try 443 first; 9060 if that times out
ISE_VERIFY_TLS=True
# ISE endpoint custom-attribute names that hold site + device type (the
# "Location" / "Device Type" columns in ISE Context Visibility). Defaults shown;
# override only if your org named them differently — confirm with:
#   manage.py probe_apis --mac <a-known-MAC>
ISE_SITE_ATTR=Location
ISE_DEVICE_TYPE_ATTR=Device Type

FMC_HOST=<fmc-host>
FMC_USERNAME=<fmc-user>
FMC_PASSWORD=<fmc-pass>
FMC_VERIFY_TLS=True

RETENTION_THREAT_DAYS=90
RETENTION_CONNECTION_DAYS=14
```

## 4. Install app + services (one script)
Installs Python 3.11 / PostgreSQL / Redis / nginx, creates the DB + app user +
venv, installs deps, runs migrate + collectstatic, sets SELinux/firewalld, and
enables the web/worker/beat services.
```bash
POSTGRES_PASSWORD='<same as .env.prod>' sudo -E bash deploy/install_rhel9.sh
```

## 5. Verify the platform is up
```bash
systemctl is-active postgresql redis nginx iotdash-web iotdash-worker iotdash-beat
curl -sI http://localhost/ | head -1          # expect HTTP/1.1 200 (or 302)
```

## 6. Validate the Cisco APIs (read-only)
The app auto-loads `.env.prod` (via python-dotenv), so no `source` is needed —
just run:
```bash
sudo -u iotdash .venv/bin/python manage.py probe_apis --out api_responses.json
```
All rows should say `OK`. ISE timing out → flip `ISE_ERS_PORT` (443 ↔ 9060) in
`.env.prod` and re-run. Odd fields? send `api_responses.json` for parser tuning.

## 7. Seed ISE device inventory
(also runs every 15 min via Celery beat)
```bash
sudo -u iotdash .venv/bin/python -c "import django,os;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';django.setup();from dashboard.tasks import poll_ise_inventory;print(poll_ise_inventory())"
# expect: {'ise_devices': N}  with N > 0
```

## 8. FMC events via eStreamer (eNcore)  → full detail in ESTREAMER_SETUP.md
```bash
# install eNcore + drop in client.pkcs12
sudo dnf -y install python3.11
sudo git clone https://github.com/CiscoSecurity/fp-05-microsoft-sentinel-connector.git /opt/eStreamer-eNcore
sudo chown -R iotdash:iotdash /opt/eStreamer-eNcore
cp /path/to/client.pkcs12 /opt/eStreamer-eNcore/client.pkcs12
# edit /opt/eStreamer-eNcore/estreamer.conf: FMC server + port 8302, pkcs12 path,
#   subscriptions, and a  json -> stdout  outputter
cd /opt/eStreamer-eNcore && bash encore.sh test          # verify TLS handshake to :8302

# grab a raw sample first (no DB writes) for parser tuning:
bash encore.sh foreground | /opt/iotdash/.venv/bin/python /opt/iotdash/manage.py \
    estreamer_ingest --capture /tmp/encore-sample.jsonl --capture-only

# start the ingester service:
sudo firewall-cmd --permanent --add-port=8302/tcp; sudo firewall-cmd --reload   # if outbound is filtered
sudo systemctl enable --now iotdash-estreamer
journalctl -u iotdash-estreamer -f                    # watch "Ingested N events"
```

## 9. TLS (production)
```bash
sudo cp iotdash.crt /etc/pki/tls/certs/ ; sudo cp iotdash.key /etc/pki/tls/private/
sudo vi /etc/nginx/conf.d/iotdash.conf     # enable the 443 server block, redirect 80->443
sudo nginx -t && sudo systemctl restart nginx
```

## 10. Acceptance
Open `https://dashboard.example.com/` and confirm: W1 (Total IoT Devices) > 0;
events flow into W2/W4/W5; ISE↔FMC Mapping shows Matched vs FMC-only; device
search opens a Device 360.

---

## Everyday operations
```bash
systemctl status iotdash-*                     # health
journalctl -u iotdash-estreamer -f             # live ingest
sudo systemctl restart iotdash-web iotdash-worker iotdash-beat iotdash-estreamer

# deploy an update
cd /opt/iotdash && git pull && \
  sudo -u iotdash .venv/bin/pip install -r requirements-prod.txt && \
  sudo -u iotdash .venv/bin/python manage.py migrate && \
  sudo -u iotdash .venv/bin/python manage.py collectstatic --noinput && \
  sudo systemctl restart iotdash-web iotdash-worker iotdash-beat iotdash-estreamer
```

## If something's wrong
| Symptom | Fix |
|---|---|
| `dnf` can't install | VM not entitled → step 0 (register / activation key) |
| W1 = 0 | ISE poll failing → `probe_apis`; flip `ISE_ERS_PORT`; check ERS role/firewall |
| FMC auth 401/429 | verify account; one client per FMC user; wait a minute |
| No events | `bash encore.sh test`; host→FMC:8302 open; cert host must match eNcore host |
| nginx 502 | `systemctl status iotdash-web`; `sudo setsebool -P httpd_can_network_connect 1` |
| disk filling | shorten retention / drop `connection` subscription; see SETUP_GUIDE troubleshooting |

Full explanations + VM sizing: [SETUP_GUIDE.md](SETUP_GUIDE.md) ·
[ESTREAMER_SETUP.md](ESTREAMER_SETUP.md) · [PREREQUISITES.md](PREREQUISITES.md) ·
[docs/DEPLOYMENT_VM_SPEC.md](docs/DEPLOYMENT_VM_SPEC.md).
