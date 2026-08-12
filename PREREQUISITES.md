# Prerequisites — IoT Security Dashboard (ISE + FMC)

Everything you need to run this application and connect it to Cisco ISE and FMC.

---

## 1. Run the POC (the current app)

### Host / OS
- Windows 10/11, macOS, or Linux (the app is cross-platform).
- Outbound network access to the ISE and FMC endpoints (see §4).

### Software
| Requirement | Version | Notes |
|---|---|---|
| Python | **3.11+** (3.14 tested) | with `pip` and `venv` |
| Python packages | see `requirements.txt` | Django ≥5, `requests`, `python-dotenv` |
| Web browser | any modern browser | dashboard uses Bootstrap 5, Chart.js, DataTables via CDN |
| Internet access | — | required for the CDN assets **and** to reach ISE/FMC |

### Install & run
```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate          # macOS / Linux
pip install -r requirements.txt
copy .env.example .env               # then fill in credentials (see §3)
python manage.py migrate
python manage.py runserver
```
Open <http://127.0.0.1:8000/>.

> **Offline note:** the UI pulls Bootstrap/Chart.js/DataTables from a CDN. If the
> browser host has no internet, those assets must be vendored locally.

---

## 2. Cisco ISE prerequisites

### Reachability
- ISE PAN (admin node) reachable from the app host on **TCP 9060** (ERS) and
  **TCP 443** (ERS-on-443 / MnT).

### Enable the APIs (ISE admin UI)
- **Administration → System → Settings → API Settings → API Service Settings**
  - Enable **ERS (Read/Write)** — required.
  - Enable **Open API** — optional (not required by this app today).

### Service account
- A dedicated account that is a member of an admin group with **ERS access**
  (**ERS Admin** for read/write, or **ERS Operator** for read-only — read-only is
  sufficient for this dashboard).
- For **Widget-level session data (MnT / active sessions)** the account also needs
  **MnT API access** (Super Admin / MnT Admin). *Without it, the "ISE Active
  Sessions" report returns 401 — expected, and handled gracefully.*

### What the app reads from ISE (read-only)
Endpoints, endpoint identity groups, network devices, profiler profiles, and
(optionally) MnT active sessions.

---

## 3. Cisco FMC prerequisites

### Reachability
- FMC reachable from the app host on **TCP 443** (REST API).

### Enable the REST API (FMC admin UI)
- **System → Configuration → REST API Preferences → Enable REST API** (on by
  default in FMC 6.1+).

### Service account
- A dedicated FMC user with a **read-only** role is sufficient for config polling.
- ⚠️ **Token/rate limits:** FMC allows a small number of concurrent API tokens
  (~10) and rate-limits `generatetoken`. The app reuses one token per source; do
  not point many clients at the same FMC account.

### What the app reads from FMC (read-only)
Managed devices, access-control policies & rules, intrusion / file / prefilter
policies, security zones, network objects, audit records.

### ⚠️ Events are NOT in the FMC REST API
Intrusion / malware / file / connection **events** (needed for the threat/traffic
widgets) are **not** available over the config REST API. They require one of:
- **eStreamer** (TCP **8302**) with a **pkcs12 client certificate** generated in
  **System → Integration → eStreamer** (select the event types + Create Client +
  Download Certificate), consumed by an **eNcore** collector;

---

## 4. Network / firewall

| From (app host) | To | Port | Purpose |
|---|---|---|---|
| App | ISE PAN | **TCP 9060** | ERS API |
| App | ISE PAN | **TCP 443** | ERS-on-443 / MnT sessions |
| App | FMC | **TCP 443** | FMC REST API |
| Event collector | FMC | **TCP 8302** | eStreamer event stream *(when events are wired up)* |
| Browser | App | **TCP 8000** (dev) / **443** (prod) | Dashboard UI |
