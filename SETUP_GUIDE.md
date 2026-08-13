# Production Setup Guide — IoT Security Dashboard (ISE + FMC) on RHEL 9.8

A single end-to-end runbook: from a blank RHEL 9.8 VM to a running dashboard
ingesting live FMC events and correlating them to ISE identity.

Follow the steps **in order**. Each phase ends with a ✅ **verify** step — don't
move on until it passes. Companion docs are referenced where they go deeper:
[PREREQUISITES.md](PREREQUISITES.md) · [DEPLOY_RHEL9.md](DEPLOY_RHEL9.md) ·
[ESTREAMER_SETUP.md](ESTREAMER_SETUP.md) · [docs/DEPLOYMENT_VM_SPEC.md](docs/DEPLOYMENT_VM_SPEC.md).

---

## Architecture (what you're building)
```
                    ┌──────────── RHEL 9.8 host(s) ────────────┐
 Cisco ISE  ──REST(443)──►  sync_iot_endpoints ─► IoTDevice ┐  │
 Cisco FMC  ──REST(443)──►  config datasets                 │  │
 Cisco FMC  ──eStreamer(8302)──► eNcore ──JSON──► ingester ─┴─► PostgreSQL ─► Django/gunicorn ─► nginx ─► users
                                                              Redis (cache+broker) · Celery worker+beat
```
- **ISE/FMC config + identity**: REST APIs, polled on a schedule.
- **FMC events**: eStreamer → eNcore → `estreamer_ingest` → DB (stamped with ISE identity).
- Dashboard reads from the DB only. No synthetic data.

---

## Phase 0 — Before you start: gather these

| Item | Where from | Notes |
|---|---|---|
| VM(s) sized per environment | [docs/DEPLOYMENT_VM_SPEC.md](docs/DEPLOYMENT_VM_SPEC.md) | UAT: 1 VM (8 vCPU/16 GB/250 GB). Prod: tiered. |
| RHEL 9.8 + valid subscription | your platform | `subscription-manager` registered |
| ISE host + **ERS** read-only account | ISE admin | ERS enabled; note the ERS port (443 or 9060) |
| FMC host + **REST** read-only account | FMC admin | REST API enabled |
| FMC **eStreamer client cert** (`client.pkcs12`) + password | FMC → System → Integration → eStreamer | created against the eNcore host's IP |
| DNS name + **TLS cert** for the dashboard | your PKI | for nginx 443 |
| Firewall rules | network team | host → ISE:443, FMC:443, FMC:8302 |
| Strong secrets | you | Django `SECRET_KEY`, Postgres password |

Full detail: [PREREQUISITES.md](PREREQUISITES.md).

✅ **Verify:** you can `ping`/`nc -vz <ise-host> 443`, `<fmc-host> 443`, and
`<fmc-host> 8302` from the VM.

---

## Phase 1 — Prepare the RHEL 9.8 VM

```bash
sudo subscription-manager status          # must be registered
sudo dnf -y update
sudo hostnamectl set-hostname iotdash-prod
sudo timedatectl set-ntp true             # correct time = correct event correlation
timedatectl                               # confirm NTP synchronized
sudo dnf -y install git nc                # helpers
```
Keep **SELinux enforcing** and **firewalld enabled** (the installer configures both).

✅ **Verify:** `getenforce` → `Enforcing`; `timedatectl` → `System clock synchronized: yes`.

---

## Phase 2 — Cisco-side configuration

### ISE
1. **Administration → System → Settings → API Settings** → enable **ERS (Read/Write)**.
2. Create/confirm a read-only account with the **ERS Operator** role.
3. Note the ERS port: try **443** first (many deployments serve ERS there); **9060** otherwise.

### FMC — REST API
1. **System → Configuration → REST API Preferences** → **Enable REST API**.
2. Create a read-only FMC user for the app.

### FMC — eStreamer (events)
1. **System → Integration → eStreamer** → tick **Intrusion, Connection, File,
   Malware, Security Intelligence** → **Save**.
2. **Create Client** → enter the **RHEL host's IP/hostname** → set a **password** →
   **Save** → **download `client.pkcs12`**.

✅ **Verify:** you hold `client.pkcs12` + its password, and both service accounts
can log in to their web UIs.

---

## Phase 3 — Place the code + configure

Get the code onto the RHEL host — **clone from GitHub** (recommended, so updates
are a `git pull`) or copy via `rsync`/`scp`.

```bash
sudo mkdir -p /opt/iotdash && sudo chown "$USER" /opt/iotdash

# Option A — clone from GitHub (see Appendix A for auth setup)
git clone https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB.git /opt/iotdash
#   private repo over HTTPS → Git prompts for username + a Personal Access Token
#   or use SSH:  git clone git@github.com:vishal4567/FMC_ISE_IOT_DASHBOARD_WEB.git /opt/iotdash

# Option B — copy from your workstation
#   rsync -av --exclude '.venv' --exclude '.env*' ./ user@rhel-host:/opt/iotdash/

cd /opt/iotdash
cp .env.prod.example .env.prod
```
> **Never** commit `.env.prod` or `client.pkcs12` — they're already git-ignored.
> Secrets live only on the host, not in the repo.

Edit **`/opt/iotdash/.env.prod`**:
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

ISE_HOST=<ise-host>
ISE_USERNAME=<ers-user>
ISE_PASSWORD=<ers-pass>
ISE_ERS_PORT=443            # or 9060
ISE_VERIFY_TLS=True

FMC_HOST=<fmc-host>
FMC_USERNAME=<fmc-user>
FMC_PASSWORD=<fmc-pass>
FMC_VERIFY_TLS=True

RETENTION_THREAT_DAYS=90
RETENTION_CONNECTION_DAYS=14
```
> `.env.prod` holds secrets — it's git-ignored; keep it `chmod 600`, owned by the
> app user.

✅ **Verify:** `.env.prod` has real values (no placeholders) and `HOST`s are
hostname/IP only (no `https://`, no port).

---

## Phase 4 — Install (one script)

```bash
cd /opt/iotdash
POSTGRES_PASSWORD='<strong>' sudo -E bash deploy/install_rhel9.sh
```
This installs Python 3.11 / PostgreSQL / Redis / nginx, creates the DB + app
user (`iotdash`) + virtualenv, installs `requirements-prod.txt`, runs `migrate`
and `collectstatic`, sets SELinux `httpd_can_network_connect`, opens firewalld
HTTP/HTTPS, and enables the **web / worker / beat** systemd services.

✅ **Verify:**
```bash
systemctl is-active iotdash-web iotdash-worker iotdash-beat postgresql redis nginx
curl -sI http://localhost/ | head -1        # HTTP 200 (or 302)
```

---

## Phase 5 — Validate the Cisco APIs

Confirm the app can actually read ISE/FMC before relying on it:
```bash
cd /opt/iotdash
sudo -u iotdash bash -c 'set -a; source .env.prod; set +a; .venv/bin/python manage.py probe_apis --out api_responses.json'
```
Review the summary (all `OK`) and skim `api_responses.json`. If ISE times out,
switch `ISE_ERS_PORT` (443 ↔ 9060) and re-run. **If anything errors or a field
looks unexpected, send `api_responses.json` back for parser tuning.**

✅ **Verify:** `probe_apis` shows `OK` for ISE endpoints/groups and FMC auth/devices.

---

## Phase 6 — Seed the IoT inventory

Populate `IoTDevice` from ISE so events can be correlated + typed. Imports ONLY
the allow-listed IoT profiles (`ISE_IOT_LOGICAL_PROFILES` / `ISE_IOT_PROFILES`).
Reference data refreshes daily and endpoints sync hourly via beat; run now:
```bash
sudo -u iotdash .venv/bin/python manage.py sync_ise
```
✅ **Verify:** it prints `refresh_ise_reference {...}` then
`{'iot_endpoints': <N>, 'with_site': <M>}` with N > 0.

---

## Phase 7 — eStreamer events (eNcore)

Follow [ESTREAMER_SETUP.md](ESTREAMER_SETUP.md). Summary:
1. Install eNcore to `/opt/eStreamer-eNcore`, drop in `client.pkcs12`.
2. Configure `estreamer.conf`: FMC server + port **8302**, `pkcs12Filepath`,
   subscriptions, and a **`json` → `stdout`** outputter.
3. Test the handshake: `cd /opt/eStreamer-eNcore && bash encore.sh test`.
4. **Grab a raw sample first** (no DB writes) so parsers can be verified:
   ```bash
   bash encore.sh foreground | /opt/iotdash/.venv/bin/python /opt/iotdash/manage.py \
       estreamer_ingest --capture /tmp/encore-sample.jsonl --capture-only
   ```
   Send `/tmp/encore-sample.jsonl` back if the fields need mapping tweaks.
5. Start the ingester service:
   ```bash
   sudo systemctl enable --now iotdash-estreamer
   journalctl -u iotdash-estreamer -f       # watch "Ingested N events"
   ```

✅ **Verify:** event count grows:
```bash
sudo -u iotdash bash -c 'set -a; source .env.prod; set +a; .venv/bin/python -c "import django,os;os.environ[\"DJANGO_SETTINGS_MODULE\"]=\"config.settings\";django.setup();from dashboard.models import SecurityEvent;print(SecurityEvent.objects.count())"'
```

---

## Phase 8 — TLS + hardening

1. Put your cert/key in `/etc/pki/tls/certs/iotdash.crt` and `/etc/pki/tls/private/iotdash.key`.
2. Uncomment the **443 server block** in `/etc/nginx/conf.d/iotdash.conf`, redirect 80→443, `sudo systemctl restart nginx`.
3. Confirm `.env.prod`: `DJANGO_DEBUG=False`, real `DJANGO_ALLOWED_HOSTS` +
   `DJANGO_CSRF_TRUSTED_ORIGINS`, `*_VERIFY_TLS=True`.
4. `sudo chmod 600 /opt/iotdash/.env.prod`.

✅ **Verify:** `https://dashboard.example.com/` loads over TLS; HTTP redirects to HTTPS.

---

## Phase 9 — Final acceptance

Open the dashboard and confirm:
- **W1 Total IoT Devices** > 0 (ISE inventory).
- **W2/W4/W5** populate as events flow in; charts render.
- **ISE ↔ FMC Mapping** shows Matched vs FMC-only.
- **Device search** (navbar) opens a device's **360** view.
- Click a **device-type** tile → the per-type dashboard loads.

✅ **Done.**

---

## Operations

```bash
# service control
sudo systemctl restart iotdash-web iotdash-worker iotdash-beat iotdash-estreamer
systemctl status iotdash-*                      # health
journalctl -u iotdash-estreamer -f              # live ingest
journalctl -u iotdash-web -n 100                # web errors

# scheduled jobs (Celery beat; intervals via .env.prod)
ISE_REFERENCE_MINUTES=1440  IOT_SYNC_MINUTES=60  POLL_CONFIG_MINUTES=15  ROLLUP_MINUTES=60  PURGE_MINUTES=720

# run a job on demand
sudo -u iotdash .venv/bin/celery -A config call dashboard.tasks.rollup_hourly
```

- **Backups:** nightly `pg_dump` (or PITR via WAL archiving) of the `iotdash` DB.
- **Retention:** the purge task drops raw connection events after
  `RETENTION_CONNECTION_DAYS` and threat events after `RETENTION_THREAT_DAYS`;
  `HourlyAggregate` is kept.
- **Upgrades:** pull new code → `pip install -r requirements-prod.txt` →
  `manage.py migrate` → `manage.py collectstatic` → restart services.
- **Scaling:** more gunicorn workers, worker replicas, a PG read replica, or
  Kafka in front of the ingester — see the VM spec's scaling triggers.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard 500 / won't start | DB unreachable / migrations | `systemctl status postgresql`; `manage.py migrate`; check `.env.prod` DB creds |
| W1 = 0, no devices | ISE poll failing | run `probe_apis`; check `ISE_ERS_PORT` (443↔9060), ERS role, firewall |
| `probe_apis` ISE timeout | wrong ERS port / blocked | flip `ISE_ERS_PORT`; open host→ISE:443/9060 |
| FMC auth 401/429 | bad creds / token limit | verify account; avoid many clients on one FMC user; wait a minute |
| No events arriving | eNcore not connected | `bash encore.sh test`; check host→FMC:8302; cert host must match; `journalctl -u iotdash-estreamer` |
| Events stored but device_type blank | device not in ISE (FMC-only) | expected — it's a shadow device; runs ISE poll to enrich enrolled ones |
| Fields parsed wrong | eNcore JSON schema differs | capture with `estreamer_ingest --capture … --capture-only`, send sample, tune `dashboard/estreamer/mapping.py` |
| nginx 502 | gunicorn down / SELinux | `systemctl status iotdash-web`; `setsebool -P httpd_can_network_connect 1` |
| Static/CSS missing | collectstatic not run | `manage.py collectstatic --noinput`; restart web |

---

## Quick reference — ports & accounts
| From | To | Port | Purpose |
|---|---|---|---|
| App host | ISE | 443 (or 9060) | ERS + MnT |
| App host | FMC | 443 | REST config |
| eNcore host | FMC | 8302 | eStreamer events |
| Users | nginx | 443 | Dashboard (TLS) |

Accounts/certs: ISE ERS (read-only) · FMC REST (read-only) · FMC eStreamer
**pkcs12** client cert · dashboard **TLS** cert.

---

## Appendix A — Source control (GitHub)

Repo: **https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB**

### A.1 First-time push (publish this build)
The project is already a git repo (`main`, secrets git-ignored). Point it at
GitHub and push:

```bash
cd /opt/iotdash          # or your working copy
git remote add origin https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB.git
git branch -M main
git push -u origin main
```
If the GitHub repo was created **with** a README/.gitignore, reconcile first:
```bash
git pull --rebase origin main
git push -u origin main
```

### A.2 Authentication (pick one — GitHub no longer accepts passwords)
| Method | Setup | Notes |
|---|---|---|
| **HTTPS + PAT** | GitHub → Settings → Developer settings → **Personal Access Token** (scope `repo`). On `git push`, enter your **username** + paste the **PAT as the password**. | Cache it: `git config --global credential.helper store` (or Git Credential Manager on Windows). |
| **SSH key** | `ssh-keygen -t ed25519 -C you@org`; add the **public** key to GitHub → Settings → **SSH keys**; test `ssh -T git@github.com`. Use the `git@github.com:...` remote URL. | No token prompts after setup. |
| **GitHub CLI** | `dnf install gh` (RHEL) / installer; `gh auth login`; then `gh repo create ... --push` or normal `git push`. | Easiest if you use `gh`. |

> Enter your PAT into **git's own prompt / your OS credential manager** — never
> paste it into a chat or commit it.

### A.3 Set your commit author (optional)
The initial commit is authored with a personal email. For an org repo, set your
work identity (repo-local):
```bash
git config user.name  "Your Name"
git config user.email "you@yourorg.com"
# re-author the last commit if desired:
git commit --amend --reset-author --no-edit
```

### A.4 Ongoing workflow
```bash
git add -A && git commit -m "describe change"
git push
# on the RHEL host, to deploy an update:
cd /opt/iotdash && git pull && \
  sudo -u iotdash .venv/bin/pip install -r requirements-prod.txt && \
  sudo -u iotdash .venv/bin/python manage.py migrate && \
  sudo -u iotdash .venv/bin/python manage.py collectstatic --noinput && \
  sudo systemctl restart iotdash-web iotdash-worker iotdash-beat iotdash-estreamer
```

### A.5 What's kept out of git (safety)
`.env` / `.env.prod` · `client.pkcs12` / `*.pem` / `*.key` · `api_responses.json`
· `*.jsonl` captures · `.venv/` · `staticfiles/`. Verify anytime:
```bash
git check-ignore -v .env.prod client.pkcs12 api_responses.json
git ls-files | grep -Ei 'env\.prod|pkcs12|\.key$' || echo "clean - no secrets tracked"
```
