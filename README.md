# IoT Security Dashboard — Production (RHEL 9.8)

Production build of the Cisco **ISE + FMC** IoT Security Dashboard. **No simulator
/ synthetic data** — every figure comes from live Cisco APIs and ingested events.

- **ISE / FMC configuration & identity** — REST APIs (`integrations/`), polled on a
  schedule into the DB.
- **FMC events** — streamed via **eStreamer → eNcore → `estreamer_ingest` → PostgreSQL**
  (`dashboard/estreamer/`), each event stamped with ISE identity for real
  ISE↔FMC correlation.
- **Dashboard** reads from the database (`dashboard/analytics.py`).

## Deploy
👉 **Start here: [SETUP_GUIDE.md](SETUP_GUIDE.md)** — the full end-to-end
production runbook (VM → Cisco config → install → validate → eStreamer → verify →
operate → troubleshoot).

[DEPLOY_RHEL9.md](DEPLOY_RHEL9.md) is the condensed install reference; one script
(`deploy/install_rhel9.sh`) installs Python 3.11 / PostgreSQL / Redis / nginx and
the systemd services.

```
web (gunicorn) · worker (Celery) · beat (Celery) · estreamer (eNcore→ingest) · nginx
```

## Key modules
```
config/                settings (Postgres/Redis/Celery), celery app
dashboard/
  analytics.py         DB-only analytics + REAL ISE↔FMC correlation (matched vs FMC-only)
  event_store.py       read events as dicts · ISE enrichment · rollups · retention purge
  models.py            SecurityEvent · HourlyAggregate · IoTDevice
  tasks.py             poll ISE inventory · refresh FMC config · rollup · purge
  estreamer/           mapping.py (eNcore JSON -> event) · collector.py (connectivity)
  management/commands/
    estreamer_ingest.py   eNcore JSON -> SecurityEvent  (stdin | file)
    probe_apis.py         API diagnostic harness -> api_responses.json
integrations/          ISE (ERS+MnT) and FMC (REST) clients
deploy/                install_rhel9.sh · systemd units · nginx · gunicorn conf
```

## Tools you'll use
| Command | Purpose |
|---|---|
| `manage.py probe_apis --out api_responses.json` | Test/fetch every ISE/FMC API response (+ eStreamer reachability) → a file to review or hand back for tuning |
| `manage.py estreamer_ingest --source stdin` | Ingest live eNcore JSON events |
| `manage.py estreamer_ingest --source file --path events.jsonl` | Replay captured events |
| `manage.py sync_ise` | Sync the IoT endpoint inventory from ISE (allow-listed profiles only) |

## Customising the parsers
`dashboard/estreamer/mapping.py` maps eNcore/eStreamer fields → the event model —
**this is the file to adjust** once you capture real events. `probe_apis` output
(`api_responses.json`) shows the real ISE/FMC field names for the same purpose.

## Docs
- [DEPLOY_RHEL9.md](DEPLOY_RHEL9.md) — RHEL 9.8 install
- [PREREQUISITES.md](PREREQUISITES.md) — ISE/FMC enablement, accounts, ports, eStreamer cert
- [docs/DEPLOYMENT_VM_SPEC.md](docs/DEPLOYMENT_VM_SPEC.md) — VM sizing
