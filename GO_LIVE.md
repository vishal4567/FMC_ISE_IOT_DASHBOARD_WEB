# GO-LIVE — clean up the old build and deploy this one (systemd / RHEL 9.8)

Runbook for a host that **already ran an earlier build**, deployed the **systemd**
way (no Docker): the app runs in a Python venv under systemd, with
**system-installed** PostgreSQL, Redis and nginx. This is a **clean-slate cutover
— no backups**: stop the old app, wipe all old data (DB, Redis, artifacts),
deploy this build, seed from ISE/FMC, verify.

Deep-dive docs: [SETUP.md](SETUP.md) · [SETUP_GUIDE.md](SETUP_GUIDE.md) ·
[ESTREAMER_SETUP.md](ESTREAMER_SETUP.md) · [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

Repo: `https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB` · branch `main` · tag `v1.0.0`

Every `manage.py` command below uses the venv + `.env.prod`:
```bash
RUN="sudo -u iotdash /opt/iotdash/.venv/bin/python manage.py"
```

---

## 0. What this does, and the one rule

```
ISE  ──REST──►  sync_iot_endpoints ─► IoTDevice (device type + site + IP)
ISE/FMC ─REST─► snapshot_datasets  ─► Snapshot table          ┐
FMC ─eStreamer(8302)─► eNcore ─► estreamer_ingest ─► SecurityEvent │─► PostgreSQL ─► gunicorn ─► nginx
                                                                     Redis · Celery worker+beat (systemd)
```

**Rule:** the web tier reads **only the database**. Cutover order is
**stop → wipe → deploy → migrate → seed → verify.** All app data is regenerated
from ISE/FMC, so discarding the old data is safe.

Keep only `/opt/iotdash/.env.prod` and `client.pkcs12` (config, not "old data",
not in git). Everything else is rebuilt.

---

## 1. Stop the old app

```bash
sudo systemctl stop iotdash-web iotdash-worker iotdash-beat iotdash-estreamer 2>/dev/null || true
```

---

## 2. Wipe all old data + remnants

### 2a. App artifacts, eNcore state, Redis, stale units (cleanup.sh)
Dry-run first (it never touches `.env.prod`/cert):
```bash
cd /opt/iotdash
bash deploy/cleanup.sh --systemd --artifacts --encore --cache        # PREVIEW
bash deploy/cleanup.sh --systemd --artifacts --encore --cache --yes  # APPLY
```
This removes the `iotdash-*` unit files, pyc/staticfiles/legacy `db.sqlite3`,
eNcore bookmarks/cache/logs, and flushes Redis (the old build cached live results
there — stale under the DB-only model).

### 2b. Drop the database (this is the "remove all old data" step)
The installer recreates it empty in §4.
```bash
sudo -u postgres psql -c "DROP DATABASE IF EXISTS iotdash;"
# (leave the iotdash role; the installer reuses it)
```
> If PostgreSQL isn't installed yet (truly first deploy), skip 2b — there's
> nothing to drop; §4 installs and creates it fresh.

### 2c. (optional) rebuild the venv from scratch
```bash
sudo rm -rf /opt/iotdash/.venv
```

---

## 3. Get this build

```bash
cd /opt/iotdash
git fetch origin --tags
git stash push -m "prod-local $(date +%F)" 2>/dev/null || true   # set aside stray local edits
git checkout v1.0.0                                              # or: git reset --hard origin/main
git log --oneline -1
```
`.env.prod` and `client.pkcs12` are git-ignored and stay put.

---

## 4. Install the stack + migrate (one script — pick a PostgreSQL source)

`deploy/install_rhel9.sh` installs Python 3.11 / Redis / nginx, installs
**PostgreSQL 16**, (re)creates the DB + role, sets `md5` auth, creates the venv,
runs migrate + collectstatic, sets SELinux + firewalld, and enables the
`iotdash-web/worker/beat` services. Already-installed packages are a no-op, so
running it now (with only Postgres missing) just fills in Postgres + the rest.

**Django 5 needs PostgreSQL ≥ 14.** Choose where Postgres comes from:

### Option A — RHEL AppStream module (default)
Postgres from your Satellite's `postgresql:16` module stream. Use this when the
`16` (or `15`) stream is **mirrored and reachable** on your Satellite.
```bash
cd /opt/iotdash
POSTGRES_PASSWORD='<same value as in .env.prod>' sudo -E bash deploy/install_rhel9.sh
#   need a different stream?  add:  PG_STREAM=15
```
Package `postgresql-server` · service `postgresql` · data `/var/lib/pgsql/data`.

### Option B — PostgreSQL's own repo (PGDG, secondary repo)
Adds `yum.postgresql.org` (like adding the MongoDB repo) and installs PG 16 from
there — use this when the RHEL module isn't mirrored but the host **can reach the
internet** (or a proxy) to `download.postgresql.org`.
```bash
cd /opt/iotdash
POSTGRES_PASSWORD='<same value as in .env.prod>' PG_SOURCE=pgdg \
  sudo -E bash deploy/install_rhel9.sh
#   different major?  add:  PG_MAJOR=15
```
Package `postgresql16-server` · service `postgresql-16` · data
`/var/lib/pgsql/16/data`. The script parameterizes all of these, so the DB/auth/
migrate steps are identical to Option A.

> **Option B note:** PGDG's `psql` isn't on `PATH`. Anywhere this guide runs a
> bare `psql` manually (e.g. §2b drop, §10 rollback), use the full path:
> `sudo -u postgres /usr/pgsql-16/bin/psql …`.

> Both options still need **`python3.11`, `redis`, `nginx`** from the RHEL repos
> (only Red Hat ships these). PGDG only removes the Satellite dependency for
> **Postgres** — if those base packages aren't already installed and the
> Satellite is down, install them first (you said they're already in).

Because the DB was dropped in §2b, `migrate` builds **all** tables fresh (expect a
full set of "Applying …" lines).

Confirm the platform is up (use your Postgres service name):
```bash
PGSVC=postgresql            # Option A;  Option B -> postgresql-16
systemctl is-active $PGSVC redis nginx iotdash-web iotdash-worker iotdash-beat
curl -sI http://localhost/ | head -1        # HTTP/1.1 200 or 302
sudo -u postgres $( [ "$PGSVC" = postgresql ] && echo psql || echo /usr/pgsql-16/bin/psql ) -c "SHOW server_version;"
```

---

## 5. Reconcile `.env.prod` with this build

Your old `.env.prod` predates several settings. Confirm these exist (defaults are
in `.env.prod.example`):
```ini
POSTGRES_HOST=localhost                       # systemd path uses the local PG
REDIS_URL=redis://localhost:6379/0
ISE_USE_OPENAPI=False   ISE_ERS_ENRICH=True   # ERS + mfcAttributes (confirmed)
ISE_LOCATION_METHOD=session                   # NAD Location via session (no device IP)
ISE_ERS_PORT=443        ISE_VERIFY_TLS=True
# optional: ISE_IOT_LOGICAL_PROFILES=Wipro_CCTV,Wipro-Access-Control,Wipro-BMS
```
Remove dead keys from the old build (harmless if left): `POLL_ISE_MINUTES`,
`ISE_DETAIL_LIMIT`, `ISE_SITE_ATTR`, `ISE_DEVICE_TYPE_ATTR`.

Spot what you're missing:
```bash
diff <(grep -o '^[A-Z_]*' .env.prod.example | sort -u) \
     <(grep -o '^[A-Z_]*' .env.prod        | sort -u)
```
Edited `.env.prod`? Apply it: `sudo systemctl restart iotdash-web iotdash-worker iotdash-beat`.

---

## 6. Seed the fresh database (order matters)

```bash
RUN="sudo -u iotdash /opt/iotdash/.venv/bin/python manage.py"

$RUN probe_apis          # read-only check; confirm counts.ers_by_profileId > 0
$RUN sync_ise            # → {'iot_endpoints': N, 'with_site': M}
$RUN snapshot_datasets   # → Done: N datasets (E errors) + connection status
```
After this the dashboard shows real numbers. Events (W2/W4/W5) fill in from §7.

---

## 7. FMC events via eStreamer (eNcore)

Full detail: [ESTREAMER_SETUP.md](ESTREAMER_SETUP.md).
1. In FMC (**System → Integration → eStreamer**) create a client for THIS host's
   IP, download `client.pkcs12`, place it at `/opt/iotdash/client.pkcs12`.
2. Install eNcore + drop in the config (from ESTREAMER_SETUP §2–3), set the FMC IP
   in `estreamer.conf`.
3. Start ingestion:
   ```bash
   sudo systemctl enable --now iotdash-estreamer
   journalctl -u iotdash-estreamer -f            # handshake to :8302, then "Ingested N"
   ```

---

## 8. Acceptance checklist

- [ ] `curl -sI http://localhost/` → 200/302
- [ ] **W1 (Total IoT Devices)** > 0; device types + sites look right
- [ ] Reports cards show counts; opening a report **lazy-loads** its table
- [ ] `$RUN shell -c "from dashboard.models import Snapshot; print(Snapshot.objects.count())"` > 0
- [ ] eStreamer logs show events; W2/W4/W5 populate over time
- [ ] `systemctl is-active $PGSVC redis nginx iotdash-web iotdash-worker iotdash-beat` all `active`
      (`$PGSVC` = `postgresql` for Option A, `postgresql-16` for Option B)
- [ ] Beat schedules: daily `refresh_ise_reference`, hourly `sync_iot_endpoints`,
      15-min `snapshot_datasets`, rollup, purge — no `poll_ise_inventory` (retired)

---

## 9. Day-2 operations

**Ship a code change:**
```bash
cd /opt/iotdash && git pull
sudo -u iotdash .venv/bin/pip install -r requirements-prod.txt
sudo -u iotdash .venv/bin/python manage.py migrate
sudo -u iotdash .venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart iotdash-web iotdash-worker iotdash-beat iotdash-estreamer
```
**Logs / status:** `journalctl -u iotdash-web -f` · `systemctl status iotdash-*`
**Schedules** (`.env.prod`, then restart beat):
`ISE_REFERENCE_MINUTES=1440  IOT_SYNC_MINUTES=60  POLL_CONFIG_MINUTES=15  ROLLUP_MINUTES=60  PURGE_MINUTES=720`
**TLS:** put cert/key under `/etc/pki/tls/`, enable the 443 block in
`/etc/nginx/conf.d/iotdash.conf`, `sudo nginx -t && sudo systemctl restart nginx`.

---

## 10. Rollback (if acceptance fails)

Clean slate = no old DB to restore; roll back the code and re-seed:
```bash
cd /opt/iotdash
git checkout <previous-tag-or-commit>
sudo -u postgres psql -c "DROP DATABASE IF EXISTS iotdash;" \
  && sudo -u postgres psql -c "CREATE DATABASE iotdash OWNER iotdash;"
sudo -u iotdash .venv/bin/pip install -r requirements-prod.txt
sudo -u iotdash .venv/bin/python manage.py migrate
sudo systemctl restart iotdash-web iotdash-worker iotdash-beat
# then re-run the §6 seed commands
```
All data comes from ISE/FMC, so a fresh seed rebuilds it.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `dnf` can't install | VM not entitled → `subscription-manager register …` (see SETUP §0) |
| install: "database exists" | expected if you skipped §2b; the script reuses it — drop it first for a clean slate |
| W1 = 0 | run `sync_ise`; check ERS role/reachability with `probe_apis` |
| `ers_by_profileId` = 0 | set `ISE_IOT_LOGICAL_PROFILES=…` in `.env.prod`, re-run `sync_ise` |
| device type blank | ensure `ISE_ERS_ENRICH=True` (default) — reads `mfcAttributes` |
| reports "Not collected yet" | run `snapshot_datasets` (or wait for the 15-min beat) |
| stale numbers after cutover | `bash deploy/cleanup.sh --cache --yes` (flush Redis), reload |
| old task still firing | restart `iotdash-beat`/`worker` — beat rebuilds its schedule on start |
| ISE ProxyError | set `NO_PROXY=<ise>,<fmc>,10.0.0.0/8,localhost,127.0.0.1` in `.env.prod` |
| nginx 502 | `systemctl status iotdash-web`; `sudo setsebool -P httpd_can_network_connect 1` |
| eNcore permission denied | `sudo chown -R iotdash:iotdash /opt/eStreamer-eNcore` |
| No events | `bash /opt/eStreamer-eNcore/encore.sh test`; host→FMC:8302 open; cert host must match this VM |

When in doubt: `$RUN probe_apis` then read `api_out/_summary.json`.
