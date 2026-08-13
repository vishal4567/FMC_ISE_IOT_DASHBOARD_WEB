# GO-LIVE — commit, deploy, and run in production

End-to-end runbook for taking this build live. Follow it top to bottom the first
time. Deep-dive docs are linked where relevant:
[DOCKER.md](DOCKER.md) · [SETUP.md](SETUP.md) · [SETUP_GUIDE.md](SETUP_GUIDE.md) ·
[ESTREAMER_SETUP.md](ESTREAMER_SETUP.md) · [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

Repo: `https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB` · branch `main` · tag `v1.0.0`

---

## 0. What you're deploying (and the one rule that matters)

```
ISE  ──REST──►  sync_iot_endpoints ─► IoTDevice (device type + site + IP)
ISE/FMC ─REST─► snapshot_datasets  ─► Snapshot table          ┐
FMC  ──eStreamer(8302)──► eNcore ──► estreamer_ingest ─► SecurityEvent │─► PostgreSQL ─► web (DB-only) ─► nginx
                                                                          Redis · Celery worker+beat
```

**The rule:** the web tier reads **only the database**. Schedulers/ingester fill
the DB; the browser never waits on ISE/FMC. So **first-run order is: migrate →
sync_ise → snapshot_datasets → eStreamer → verify.**

Pick ONE deploy path and stick to it on a host: **Docker** (§4A, recommended —
you asked for "Docker for everything") or **systemd** (§4B). Do not run both
against the same database.

---

## 1. Commit & push from your workstation

Your tree is committed and tagged. Push `main` + tags to GitHub:

```bash
cd /c/Users/vishal.bansal/Desktop/FMC_ISE_DASHBOARD_PROD_RHEL9
git status                      # expect: clean
git push origin main --tags     # pushes commits + v1.0.0
```

GitHub auth: when prompted for a password, paste a **Personal Access Token**
(Settings → Developer settings → Tokens, `repo` scope) — not your account
password. To avoid re-typing: `git config --global credential.helper manager`.

> Secrets never leave your machine: `.env`, `.env.prod`, `*.pkcs12/*.pem/*.key`
> are git-ignored. Verify: `git check-ignore -v .env.prod client.pkcs12`.

---

## 2. Prepare the production VM (RHEL 9.8)

```bash
# entitle (skip if the cloud image is already registered)
sudo subscription-manager status || sudo subscription-manager register --org <ORG_ID> --activationkey <KEY>
sudo dnf -y update && sudo dnf -y install git
sudo timedatectl set-ntp true          # correct time = correct event correlation
```

Get the code:
```bash
sudo mkdir -p /opt/iotdash && sudo chown "$USER" /opt/iotdash
git clone https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB.git /opt/iotdash
cd /opt/iotdash && git checkout v1.0.0     # pin the released build (optional)
```

Network from the VM (do this before you test APIs):
- Outbound **443** to ISE + FMC, and **8302** to FMC (eStreamer).
- If a proxy is present, bypass internal hosts in `.env.prod`:
  `NO_PROXY=<ise-ip>,<fmc-ip>,10.0.0.0/8,localhost,127.0.0.1`

---

## 3. Configure secrets — `.env.prod`

```bash
cd /opt/iotdash
cp .env.prod.example .env.prod && chmod 600 .env.prod
vi .env.prod
```

Minimum to set (this build's confirmed defaults are already in the example):
```ini
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<64+ random chars>            # python -c "import secrets;print(secrets.token_urlsafe(64))"
DJANGO_ALLOWED_HOSTS=dashboard.example.com,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=https://dashboard.example.com

POSTGRES_DB=iotdash
POSTGRES_USER=iotdash
POSTGRES_PASSWORD=<strong>
# (Docker sets POSTGRES_HOST/REDIS_URL for you; systemd uses localhost)

ISE_HOST=<ise-ip>            ISE_USERNAME=<ers-user>   ISE_PASSWORD=<pass>
ISE_ERS_PORT=443             ISE_VERIFY_TLS=True
ISE_USE_OPENAPI=False        ISE_ERS_ENRICH=True       # ERS + mfcAttributes (confirmed)
ISE_LOCATION_METHOD=session  # NAD Location via session (no device IP needed)

FMC_HOST=<fmc-ip>            FMC_USERNAME=<user>        FMC_PASSWORD=<pass>
FMC_VERIFY_TLS=True

RETENTION_THREAT_DAYS=90      RETENTION_CONNECTION_DAYS=14
# NO_PROXY=<ise-ip>,<fmc-ip>,10.0.0.0/8,localhost,127.0.0.1
```

---

## 4A. Deploy with Docker  (recommended)

Full detail + the code-change workflow: [DOCKER.md](DOCKER.md).

```bash
cd /opt/iotdash
docker compose build                 # builds iotdash:latest (web/worker/beat share it)
docker compose up -d                 # postgres, redis, web, worker, beat, nginx
docker compose ps                    # all "healthy"/"running"
docker compose logs -f web           # watch migrate + collectstatic + gunicorn
```
`migrate` and `collectstatic` run automatically on the web container. Management
commands are `docker compose run --rm web python manage.py <cmd>` → go to §5.

## 4B. Deploy with systemd  (alternative)

Full detail: [SETUP.md](SETUP.md). One script installs Python/Postgres/Redis/
nginx, the venv, runs migrate + collectstatic, sets SELinux/firewalld, and
enables the services:
```bash
POSTGRES_PASSWORD='<same as .env.prod>' sudo -E bash deploy/install_rhel9.sh
systemctl is-active postgresql redis nginx iotdash-web iotdash-worker iotdash-beat
```
Management commands are `sudo -u iotdash /opt/iotdash/.venv/bin/python manage.py <cmd>` → go to §5.

---

## 5. First-run data population — **order matters**

Run these once now (both paths also run them on a schedule afterward). Use the
command prefix for your path:
- Docker:  `docker compose run --rm web python manage.py …`
- systemd: `sudo -u iotdash /opt/iotdash/.venv/bin/python manage.py …`

```bash
# 1) Validate the Cisco APIs (read-only). All rows should say OK.
manage.py probe_apis
#    → also writes api_out/ise.iot.profile_filter.json — confirm
#      counts.ers_by_profileId > 0 (else set ISE_IOT_LOGICAL_PROFILES).

# 2) ISE reference + IoT inventory  →  IoTDevice (device type + site + IP)
manage.py sync_ise
#    → refresh_ise_reference {...} then {'iot_endpoints': N, 'with_site': M}

# 3) ISE/FMC datasets + connectivity  →  Snapshot table (what the web reads)
manage.py snapshot_datasets
#    → Done: N datasets (E with errors) + connection status written to the DB.
```

After these, the dashboard shows real numbers. Events (§6) fill W2/W4/W5 as they
arrive.

---

## 6. FMC events via eStreamer (eNcore)

Full detail: [ESTREAMER_SETUP.md](ESTREAMER_SETUP.md).

1. In FMC: **System → Integration → eStreamer** → tick event types → **Create
   Client** for THIS VM's IP → download **`client.pkcs12`**.
2. Put it in place + set the FMC IP in the config:
   ```bash
   cp /path/to/client.pkcs12 /opt/iotdash/client.pkcs12
   vi /opt/iotdash/deploy/estreamer.conf.example   # subscription.servers[0].host = FMC IP
   ```
3. Start ingestion:
   - **Docker:** `docker compose --profile estreamer build && docker compose --profile estreamer up -d estreamer`
   - **systemd:** install eNcore (see ESTREAMER_SETUP §2), then
     `sudo systemctl enable --now iotdash-estreamer`
4. Watch it: `docker compose logs -f estreamer`  /  `journalctl -u iotdash-estreamer -f`
   → eNcore handshake to :8302, then `Ingested N`.

> Capture a sample first if you want the parser tuned to your fields:
> `… estreamer_ingest --capture /tmp/encore-sample.jsonl --capture-only` — send it back.

---

## 7. Acceptance checklist

- [ ] `curl -sI http://localhost/` → 200/302
- [ ] Dashboard **W1 (Total IoT Devices)** > 0
- [ ] Device types + sites show real values (e.g. `Zebra-Device` @ `Mumbai`)
- [ ] Reports page cards show counts; opening a report lazy-loads its table
- [ ] eStreamer logs show events; W2/W4/W5 populate over time
- [ ] `systemctl is-active` / `docker compose ps` all healthy
- [ ] Celery beat scheduled: daily `refresh_ise_reference`, hourly
      `sync_iot_endpoints`, 15-min `snapshot_datasets`, hourly rollup, purge

---

## 8. Day-2 operations

**Ship a code change** (the one workflow difference between paths):
```bash
cd /opt/iotdash && git pull
# Docker:
docker compose build && docker compose up -d          # + --profile estreamer build/up if used
# systemd:
sudo -u iotdash .venv/bin/pip install -r requirements-prod.txt
sudo -u iotdash .venv/bin/python manage.py migrate
sudo -u iotdash .venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart iotdash-web iotdash-worker iotdash-beat iotdash-estreamer
```
`.env.prod`-only change: Docker `docker compose up -d`; systemd `systemctl restart`.

**Logs / status:**
```bash
docker compose logs -f web|worker|beat|estreamer      # Docker
journalctl -u iotdash-web -f                           # systemd
```

**Schedules** (tune via `.env.prod`, then restart beat):
`ISE_REFERENCE_MINUTES=1440  IOT_SYNC_MINUTES=60  POLL_CONFIG_MINUTES=15  ROLLUP_MINUTES=60  PURGE_MINUTES=720`

**TLS:** terminate at nginx — mount cert/key, add a 443 server block
(`docker/nginx.conf` or `/etc/nginx/conf.d/iotdash.conf`), reload nginx.

**Rollback:** `git checkout v1.0.0` (or the previous tag) then re-run the deploy
step for your path. DB migrations here are additive; no down-migration needed.

**Cleanup / teardown** ([deploy/cleanup.sh](deploy/cleanup.sh) — dry-run by default):
```bash
bash deploy/cleanup.sh --all            # preview
bash deploy/cleanup.sh --all --yes      # apply (keeps the database)
bash deploy/cleanup.sh --all --data --yes   # also wipe the DB (destructive)
```

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `dnf` can't install | VM not entitled → §2 register / activation key |
| probe ISE ProxyError / timeout | set `NO_PROXY` in `.env.prod`; try `ISE_ERS_PORT` 443↔9060 |
| `ers_by_profileId` count = 0 | set `ISE_IOT_LOGICAL_PROFILES=Wipro_CCTV,Wipro-Access-Control,Wipro-BMS` |
| W1 = 0 | run `sync_ise`; check ERS role/reachability in `probe_apis` |
| device type blank | `ISE_ERS_ENRICH=True` (already default) — pulls `mfcAttributes` |
| site blank for many | those NADs aren't tagged with a `Location#…#<site>` sub-group |
| reports table empty / "Not collected yet" | run `snapshot_datasets` (or wait for beat) |
| FMC auth 401/429 | verify account; one eStreamer client per FMC user; wait a minute |
| No events | `encore.sh test`; host→FMC:8302 open; cert host must match this VM |
| nginx 502 (systemd) | `systemctl status iotdash-web`; `sudo setsebool -P httpd_can_network_connect 1` |

Deeper explanations live in the linked docs. When in doubt, run `probe_apis` and
read `api_out/_summary.json`.
