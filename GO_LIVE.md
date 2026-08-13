# GO-LIVE — clean up the existing prod build and cut over to this one

Runbook for hosts that **already run an earlier build** of this app. This is a
**clean-slate cutover — no backups**: it stops the old stack, **wipes all old
data** (database, Redis, containers, images), deploys this build fresh, seeds
everything from ISE/FMC, and verifies. All app data is regenerated, so losing the
old data is intentional and safe.

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

## 1. Clean slate — no backups

You want the old build's data **gone**. Nothing is preserved: the database,
Redis, containers, images and artifacts are all wiped in §3, and the new build
re-seeds everything from ISE/FMC. All app data (inventory, snapshots, events) is
regenerated — losing it is expected and safe.

The only things to keep are the ones that aren't "old build data" and aren't in
git — your **`.env.prod`** and **`client.pkcs12`**. Leave them in `/opt/iotdash`;
the cleanup and git steps do not touch them. (Grab a copy elsewhere only if you
want to be able to reconfigure without re-entering values.)

> **Note — there is no system `postgresql` service.** Postgres is the Docker
> Compose `postgres` container (data in the `iotdash_pgdata` volume). The wipe in
> §3 removes that volume; a fresh empty DB is created when you bring the stack up.

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

## 3. Purge EVERYTHING from the old build

Uses the repo's [deploy/cleanup.sh](deploy/cleanup.sh) — **dry-run by default**;
`.env.prod` and certs are never touched. `--data` adds the destructive DB/volume
wipe you want here.

```bash
cd /opt/iotdash
bash deploy/cleanup.sh --all --data            # PREVIEW the full wipe (incl. DB)
bash deploy/cleanup.sh --all --data --yes      # DO IT: compose down -v (removes the
                                               #   postgres volume + all data), images,
                                               #   Redis flush, artifacts, legacy sqlite,
                                               #   eNcore state
```
This removes the database volume, Redis contents, containers, images, build
artifacts and eNcore state — a true clean slate. `.env.prod` and `client.pkcs12`
survive.

Retired systemd units, if any old ones linger from a prior layout:
```bash
systemctl list-unit-files | grep iotdash    # anything not in deploy/systemd/ is stale
# for each stale one:  sudo systemctl disable --now <unit>; sudo rm /etc/systemd/system/<unit>
sudo systemctl daemon-reload
```

Confirm the volume is gone:
```bash
docker volume ls | grep iotdash    # expect NO iotdash_pgdata line
```

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
The DB volume was wiped in §3, so `migrate` creates **all** tables fresh
(including the Snapshot table) — expect a full set of "Applying …" lines, not
just one.

---

## 7. Seed the fresh database

The DB is empty (wiped in §3, tables created by the §6 migrate). Populate it:

```bash
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

You chose a clean slate, so there's no old DB to restore — rollback is just
redeploying the previous code, which re-seeds itself the same way:

```bash
cd /opt/iotdash
git checkout <previous-tag-or-commit>       # e.g. the tag before v1.0.0
docker compose down -v                      # clear this attempt
docker compose build && docker compose up -d
# then re-run the §7 seed commands for that version
```
Because all data comes from ISE/FMC, a fresh seed rebuilds it — nothing is lost
by rolling forward or back.

---

## 11. Post-cutover (after it's stable a day or two)

```bash
docker image prune -f                        # drop old dangling images
git stash drop 2>/dev/null || true           # discard the §4 stash if unneeded
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| build fails: `resolve image config for docker.io/docker/dockerfile:1` | host can't pull the BuildKit frontend; the `# syntax=` line is already removed — `git pull` this build. Fallback: `DOCKER_BUILDKIT=0 docker compose build` |
| build fails pulling `python:3.12-slim` / alpine images | host can't reach docker.io — configure a registry mirror in `/etc/docker/daemon.json` (`"registry-mirrors"`) or `docker login <internal-registry>`, then rebuild |
| `pg_dump` / `sudo -u postgres` fails ("no postgres service") | Postgres is the Docker `postgres` container — use `docker compose exec -T postgres pg_dump …`; if no such container, the old build was SQLite (re-seed fresh) |
| old task still firing | restart `iotdash-beat` / `worker` — beat rebuilds its schedule on start |
| stale numbers after cutover | `bash deploy/cleanup.sh --cache --yes` (flush Redis), reload page |
| reports "Not collected yet" | run `snapshot_datasets` (or wait for the 15-min beat) |
| `ers_by_profileId` = 0 | set `ISE_IOT_LOGICAL_PROFILES=…` in `.env.prod`, re-run `sync_ise` |
| device type blank | ensure `ISE_ERS_ENRICH=True` (default) |
| eNcore permission denied | `sudo chown -R iotdash:iotdash /opt/eStreamer-eNcore` (see ESTREAMER_SETUP) |
| ISE ProxyError | set `NO_PROXY=<ise>,<fmc>,10.0.0.0/8,localhost,127.0.0.1` |

When in doubt: `manage.py probe_apis` then read `api_out/_summary.json`.
