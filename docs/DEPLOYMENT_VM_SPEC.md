# IoT Security Dashboard — VM / Infrastructure Specification

Deployment sizing for the ISE + FMC IoT Security Dashboard. Two environments:

1. **UAT / POC** — a single small site, all roles on one VM.
2. **Production** — hundreds of devices, real FMC event stream, 90-day retention, tiered VMs.

> **Architecture assumed:** the app runs in *ingest → store → query* mode (not
> the POC's live-fetch-per-page). FMC events are received by an **ingester**,
> buffered, written to a **database** (raw + hourly aggregates); the Django app
> reads the DB. ISE/FMC **config** is polled on a schedule, not per request.

---

## 0. Sizing assumptions (validate before final order)

| Parameter | UAT / POC | Production |
|---|---|---|
| IoT devices in scope | up to **100** | up to **~1,000** |
| Assumed event rate (mixed, mostly connection) | ~500–1,000 events/min | ~5,000–15,000 events/min |
| Threat events (IPS/malware/file/SI) | hundreds/day | thousands–tens of thousands/day |
| Retention | 30 days | **90 days** (tiered) |
| Availability target | best-effort (single VM) | HA-capable (LB + DB replica optional) |

> ⚠️ The single biggest sizing driver is the **real FMC events/sec** (connection
> events dominate). Measure it from FMC / a short eStreamer capture and re-confirm
> the DB tier before production procurement.

---

## 1. UAT / POC — Single all-in-one VM

One VM hosts every role. Simple to stand up, ideal to validate real ISE + FMC
integration at one small site.

### VM spec
| Resource | Spec |
|---|---|
| vCPU | **8** |
| RAM | **16 GB** |
| Disk | **250 GB SSD** (OS 40 GB · data ~150 GB · logs/headroom ~60 GB) |
| OS | Ubuntu Server 22.04 LTS (or RHEL 8/9) |
| Network | 1 GbE; static IP; DNS + NTP |

### Roles co-located on this VM
- **Web/App:** nginx + gunicorn (Django) + Celery worker & beat
- **Database:** PostgreSQL 14 (TimescaleDB optional at this scale)
- **Cache/Queue:** Redis
- **Ingester:** eStreamer **eNcore** client (or syslog collector)

### Retention (UAT)
- Raw **threat** events: 30 days
- Raw **connection** events: 7 days
- Hourly aggregates: 30 days
- Estimated stored data: **~20–40 GB** (well within 150 GB data allowance)

### Deployment
`docker-compose` on the single VM (app · postgres · redis · celery · encore) —
fastest to rebuild and matches the production service layout.

---

## 2. Production — Tiered VMs

Separates the DB and ingester from the app so event I/O doesn't starve the UI.
Sized for hundreds of devices with full event fidelity and 90-day retention.

### VM tiers
| # | VM role | vCPU | RAM | Disk | Notes |
|---|---|---|---|---|---|
| 1 | **App / Web** | 4 | 16 GB | 80 GB SSD | nginx + gunicorn + Celery. Run **2×** behind a load balancer for HA. |
| 2 | **Database** | 8 | **32 GB** | **1 TB NVMe** | PostgreSQL 14 + **TimescaleDB**. RAM sized to hold recent events + indexes. |
| 2b | *(opt) DB replica* | 8 | 32 GB | 1 TB NVMe | Streaming replica for HA + read-heavy reporting. |
| 3 | **Ingestion** | 4 | 16 GB | 150 GB SSD | eNcore/eStreamer client + local buffer. Must reach **FMC:8302**. |
|      |                    |      |           |               |                                                              |

**Minimum viable production** = VMs 1 + 2 + 3
**HA production** = 2× App + LB, VM2 + VM2b replica

### Storage sizing (DB VM)
| Data class | Retention | Est. size |
|---|---|---|
| Raw threat events (IPS/malware/file/SI) | 90 days | < 10 GB |
| Raw connection events (drill-down) | 7–14 days | 50–150 GB (rate-dependent) |
| Hourly aggregates (per device/app/severity) | 90 days | a few GB |
| Indexes + WAL + overhead | — | +30–50% |
| **Provision** | | **1 TB NVMe** (start ~500 GB used, room to grow); TimescaleDB compression reduces this materially |

## 3. Shared prerequisites (both environments)

### Software
- Linux: Ubuntu 22.04 LTS / RHEL 8–9
- Python **3.11+**, PostgreSQL **14+** (TimescaleDB for prod), Redis **6+**
- nginx, gunicorn/uvicorn, Celery (+ beat)
- eStreamer **eNcore** (Python) — or SAL connector / syslog collector
- Docker + docker-compose (recommended for consistent deploys)

### Network / firewall
| From | To | Port | Purpose |
|---|---|---|---|
| Ingester | FMC | **TCP 8302** | eStreamer event stream (pkcs12 cert) |
| App / Celery | ISE | **TCP 9060 & 443** | ERS + MnT config/identity polling (read) |
| App / Celery | FMC | **TCP 443** | FMC REST config polling (read) |
| Users | App/LB | **TCP 443** | Dashboard (TLS) |
| App ↔ DB ↔ Redis | internal | 5432 / 6379 | data tier (private subnet) |

### Accounts & certs
- **ISE** ERS read-only service account (ERS Operator role) + MnT access for sessions.
- **FMC** REST read-only service account.
- **eStreamer** client **pkcs12 certificate** generated in FMC (System → Integration → eStreamer).
- **TLS certificate** for the dashboard web endpoint.

### Operational
- Backups: nightly DB dump/snapshot (prod: PITR via WAL archiving).
- Monitoring: host metrics + Postgres + ingester lag (queue depth) + Celery.
- NTP on all hosts (event timestamp correlation).
- Log rotation; centralised logging optional.

---

## 4. Quick bill of materials

| Environment | VMs | Total vCPU | Total RAM | Total disk |
|---|---|---|---|---|
| **UAT / POC** | 1 | 8 | 16 GB | 250 GB SSD |
| **Production (min viable)** | 3 | 16 | 64 GB | ~1.23 TB |
| **Production (HA)** | 5 | 32 | ~112 GB | ~2.3 TB |

