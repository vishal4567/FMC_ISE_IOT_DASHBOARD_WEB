# GO-LIVE — clean up the existing prod build and cut over to this one

Runbook for hosts that **already run an earlier build** of this app. It backs up
what matters, stops the old stack, purges leftover remnants, deploys this build,
migrates the DB (additive), re-seeds, verifies, and gives you a rollback path.

Deep-dive docs: [DOCKER.md](DOCKER.md) · [SETUP.md](SETUP.md) ·
[ESTREAMER_SETUP.md](ESTREAMER_SETUP.md) · [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

Repo: `https://github.com/vishal4567/FMC_ISE_IOT_DASHBOARD_WEB` · branch `main` · tag `v1.0.0`

---

## 0. What changes vs. the previous build (why the cleanup)

| Area | Old build | This build → cleanup implication |
|---|---|---|
| DB | had a SQLite fallback | **Postgres only** — drop any `db.sqlite3` |
| ISE poll | `poll_ise_inventory` every 15 min | retired → `sync_iot_endpoints` (hourly) + `refresh_ise_reference` (daily) |
| Web reads | called ISE/FMC live + Redis cache | **DB-only**; new `Snapshot` table (migration `0002`) — flush stale Redis keys |
| Device type / site | custom-attr guess | `mfcAttributes` + NAD Location — **re-sync `IoTDevice`** |
| eNcore state | may be root-owned from manual runs | purge bookmarks/cache, fix ownership |

**Golden rule (unchanged):** web reads only the DB. Cutover order is
**stop → clean → deploy → migrate → re-seed → verify.**

Use ONE path per host — **Docker** (§ marked A) or **systemd** (§ marked B).
Command prefixes for the seed/verify steps:
- Docker:  `docker compose run --rm web python manage.py …`
- systemd: `sudo -u iotdash /opt/iotdash/.venv/bin/python manage.py …`

---

## 1. Pre-cutover — announce a window and BACK UP

Do not skip the backups. Everything else is reversible if you have these.

```bash
cd /opt/iotdash
ts=$(date +%Y%m%d-%H%M)

# a) the database (KEEP — we migrate it, not recreate it)
# Docker:
docker compose exec -T postgres pg_dump -U iotdash iotdash > ~/iotdash-db-$ts.sql
# systemd (system Postgres):
sudo -u postgres pg_dump iotdash > ~/iotdash-db-$ts.sql

# b) secrets + cert + the current config (never in git)
cp .env.prod ~/iotdash-env-$ts.bak
[ -f client.pkcs12 ] && cp client.pkcs12 ~/iotdash-cert-$ts.pkcs12

# c) record the version you're rolling back TO
git rev-parse HEAD > ~/iotdash-prevref-$ts.txt ; git describe --tags 2>/dev/null >> ~/iotdash-prevref-$ts.txt
```

---

## 2. Stop the existing app

**A — Docker**
```bash
cd /opt/iotdash
docker compose --profile estreamer down --remove-orphans     # keeps volumes (DB safe)
```

**B — systemd**
```bash
sudo systemctl stop iotdash-web iotdash-worker iotdash-beat iotdash-estreamer
```

---

## 3. Purge leftover remnants

Uses the repo's [deploy/cleanup.sh](deploy/cleanup.sh) — **dry-run by default**,
never touches `.env.prod`/certs/DB unless you pass `--data`.

```bash
cd /opt/iotdash
bash deploy/cleanup.sh --all            # PREVIEW what will be removed
bash deploy/cleanup.sh --all --yes      # apply: build artifacts, pyc, staticfiles,
                                        #        legacy db.sqlite3, eNcore state,
                                        #        Redis cache, (Docker) stopped stack + images
```
Why the Redis flush matters: the old build cached live dataset/status results in
Redis; those keys are stale under the DB-only model. `--all` flushes them.

Retired systemd units, if any old ones linger from a prior layout:
```bash
systemctl list-unit-files | grep iotdash    # anything not in deploy/systemd/ is stale
# for each stale one:  sudo systemctl disable --now <unit>; sudo rm /etc/systemd/system/<unit>; 
sudo systemctl daemon-reload
```

> **Do NOT** run `cleanup.sh --data` here — that deletes the database. Only use it
> if you deliberately want a clean-slate DB (you have the backup from §1).

---

## 4. Get this build

```bash
cd /opt/iotdash
git fetch origin --tags
git stash push -m "prod-local $(date +%F)" 2>/dev/null || true   # set aside any local edits
git checkout v1.0.0                                              # or: git reset --hard origin/main
git log --oneline -1
```
If `git checkout` complains about the cert/env, that's expected — they're
git-ignored and stay put. If a real tracked file blocks it, inspect
`git status`; your edits are in the stash.

---

## 5. Reconcile `.env.prod` with this build's settings

Your existing `.env.prod` predates several settings. Add/confirm these (defaults
already in `.env.prod.example` — diff against it):

```bash
diff <(grep -o '^[A-Z_]*' .env.prod.example | sort -u) \
     <(grep -o '^[A-Z_]*' .env.prod        | sort -u)   # keys you're missing
```
Make sure these are present:
```ini
ISE_USE_OPENAPI=False        ISE_ERS_ENRICH=True        # ERS + mfcAttributes (confirmed)
ISE_LOCATION_METHOD=session  # NAD Location via the session (no device IP needed)
ISE_ERS_PORT=443
# optional: ISE_IOT_LOGICAL_PROFILES=Wipro_CCTV,Wipro-Access-Control,Wipro-BMS
# schedule (optional overrides):
ISE_REFERENCE_MINUTES=1440   IOT_SYNC_MINUTES=60   POLL_CONFIG_MINUTES=15
```
Remove any dead keys from the old build (harmless if left, but tidy):
`POLL_ISE_MINUTES`, `ISE_DETAIL_LIMIT`, `ISE_SITE_ATTR`, `ISE_DEVICE_TYPE_ATTR`.

---

## 6. Deploy + migrate

**A — Docker**
```bash
cd /opt/iotdash
docker compose build
docker compose up -d                    # web entrypoint runs migrate + collectstatic
docker compose ps                       # all healthy
docker compose logs -f web              # watch "Applying dashboard.0002_snapshot… OK"
```

**B — systemd**
```bash
cd /opt/iotdash
sudo -u iotdash .venv/bin/pip install -r requirements-prod.txt
sudo -u iotdash .venv/bin/python manage.py migrate            # applies 0002_snapshot (additive)
sudo -u iotdash .venv/bin/python manage.py collectstatic --noinput
sudo cp deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl restart iotdash-web iotdash-worker iotdash-beat
```
Migration `0002_snapshot` only **adds** the Snapshot table — your events and
inventory are preserved.

---

## 7. Refresh the inventory for the new model, then seed snapshots

Device type / site are computed differently now, so rebuild the inventory. This
clears only the **regenerated** tables (IoTDevice + Snapshot); it does **not**
touch your historical events.

```bash
# clear regenerated app data (optional but recommended for a clean inventory)
manage.py shell -c "from dashboard.models import IoTDevice, Snapshot; IoTDevice.objects.all().delete(); Snapshot.objects.all().delete()"

# 1) validate APIs (read-only); confirm counts.ers_by_profileId > 0
manage.py probe_apis

# 2) ISE reference + IoT inventory  →  IoTDevice (device type + site + IP)
manage.py sync_ise                # → {'iot_endpoints': N, 'with_site': M}

# 3) ISE/FMC datasets + connectivity  →  Snapshot table (what the web reads)
manage.py snapshot_datasets       # → Done: N datasets (E errors) + status
```

---

## 8. Restart eStreamer ingestion

Reuse your existing `client.pkcs12` + FMC IP in `deploy/estreamer.conf.example`.
```bash
# Docker:
docker compose --profile estreamer build && docker compose --profile estreamer up -d estreamer
docker compose logs -f estreamer
# systemd:
sudo systemctl restart iotdash-estreamer && journalctl -u iotdash-estreamer -f
```
Expect the eNcore handshake to :8302, then `Ingested N`. (The cutover reset the
bookmark, so it resumes from now — that's fine.)

---

## 9. Acceptance checklist

- [ ] `curl -sI http://localhost/` → 200/302
- [ ] **W1 (Total IoT Devices)** > 0, device types + sites look right
- [ ] Reports cards show counts; opening a report **lazy-loads** its table
- [ ] `manage.py shell -c "from dashboard.models import Snapshot; print(Snapshot.objects.count())"` > 0
- [ ] eStreamer logs show events; W2/W4/W5 populate over time
- [ ] Beat schedules present: daily `refresh_ise_reference`, hourly
      `sync_iot_endpoints`, 15-min `snapshot_datasets`, rollup, purge
- [ ] No references to `poll_ise_inventory` in `journalctl`/logs (retired)

---

## 10. Rollback (if acceptance fails)

```bash
cd /opt/iotdash
git checkout $(head -1 ~/iotdash-prevref-*.txt)      # the ref you saved in §1
# redeploy that ref: Docker  -> docker compose build && up -d
#                    systemd -> pip install -r requirements-prod.txt; migrate; restart
# only if the DB is wrong, restore the dump:
#   Docker : docker compose exec -T postgres psql -U iotdash -d iotdash < ~/iotdash-db-<ts>.sql
#   systemd: sudo -u postgres psql iotdash < ~/iotdash-db-<ts>.sql
```
This build's migration is additive, so a code-only rollback normally needs no DB
restore.

---

## 11. Post-cutover (after it's stable a day or two)

```bash
docker image prune -f                       # drop old dangling images (Docker)
rm ~/iotdash-db-*.sql ~/iotdash-env-*.bak    # remove backups once confident
git stash drop 2>/dev/null || true           # discard the §4 stash if unneeded
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| old task still firing | restart `iotdash-beat` / `worker` — beat rebuilds its schedule on start |
| stale numbers after cutover | `bash deploy/cleanup.sh --cache --yes` (flush Redis), reload page |
| reports "Not collected yet" | run `snapshot_datasets` (or wait for the 15-min beat) |
| `ers_by_profileId` = 0 | set `ISE_IOT_LOGICAL_PROFILES=…` in `.env.prod`, re-run `sync_ise` |
| device type blank | ensure `ISE_ERS_ENRICH=True` (default) |
| eNcore permission denied | `sudo chown -R iotdash:iotdash /opt/eStreamer-eNcore` (see ESTREAMER_SETUP) |
| ISE ProxyError | set `NO_PROXY=<ise>,<fmc>,10.0.0.0/8,localhost,127.0.0.1` |

When in doubt: `manage.py probe_apis` then read `api_out/_summary.json`.
