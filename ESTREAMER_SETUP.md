# Setting up eNcore (FMC eStreamer → JSON → this app)

eNcore is Cisco's reference eStreamer client. It connects to **FMC:8302** with a
**pkcs12 client certificate**, parses the binary eStreamer protocol, and outputs
**JSON**, which we pipe into `manage.py estreamer_ingest --source stdin`.

```
FMC :8302  ──eStreamer──►  eNcore (encore.sh)  ──JSON(stdout)──►  estreamer_ingest  ──►  SecurityEvent
```

> ⚠️ The eNcore **CLI is End-of-Life (1 Jul 2024)** but remains the reference
> client. Maintained forks exist (Cisco's Splunk/Sentinel eNcore repos). For a
> supported product, use **Security Analytics & Logging (SAL)** instead and point
> the ingester at its query output.

---

## 1. Enable eStreamer + create the client cert (in FMC)
FMC UI → **System → Integration → eStreamer**:
1. Tick the event types to stream: **Intrusion**, **Connection**, **File**,
   **Malware**, **Security Intelligence** (+ Impact/Discovery if wanted). **Save**.
2. **Create Client** → enter the **hostname/IP of the RHEL host running eNcore**
   (must match the source IP FMC sees) → set a **password** → **Save**.
3. Click the **download** icon → save **`client.pkcs12`**.

Copy it to the RHEL host (e.g. `scp client.pkcs12 iot@host:/opt/eStreamer-eNcore/`).

## 2. Install eNcore (RHEL 9)
```bash
sudo dnf -y install git python3.11
sudo git clone https://github.com/CiscoSecurity/fp-05-microsoft-sentinel-connector.git \
     /opt/eStreamer-eNcore
sudo chown -R iotdash:iotdash /opt/eStreamer-eNcore
cd /opt/eStreamer-eNcore
cp /path/to/client.pkcs12 ./client.pkcs12     # default filename eNcore expects
```
(Any eNcore distribution works — `magorbalassy/estreamer-encore` is a common
community mirror. Match its README to your version.)

## 3. Configure `estreamer.conf`
A ready-made, FMC-tailored config is in this repo:
**[deploy/estreamer.conf.example](deploy/estreamer.conf.example)**. Copy it over
eNcore's and set the FMC IP:
```bash
cp /opt/iotdash/deploy/estreamer.conf.example /opt/eStreamer-eNcore/estreamer.conf
# edit /opt/eStreamer-eNcore/estreamer.conf:
#   subscription.servers[0].host  ->  your FMC management IP
```
What it sets (based on the real eNcore schema):
- **`subscription.servers[0]`** → `{host: <FMC-IP>, port: 8302, pkcs12Filepath: "client.pkcs12", tlsVersion: 1.2}`.
- **`handler.outputters`** → a single **`json`** adapter with **`stream.uri: "stdout://"`** so it pipes into `estreamer_ingest --source stdin`. (The stock config wrote JSON to a rotating file and also had a CEF/Sentinel outputter — both removed.)
- **`logging.stdOut: false`** — *critical*: stdout must carry only the JSON events, or log lines corrupt the pipe.
- **`handler.records`** → `connections, core, intrusion, metadata` on; `packets, rna, rua` off (cut volume/disk). If file/malware events don't arrive, add their record type numbers to `handler.records.include`.
- **`subscription.records.packetData: false`** (don't stream raw packets).
- **`start: 2`** → resume from bookmark after restarts.

> **Durable alternative (no stdout pipe):** set the json outputter's
> `stream.uri` to `"relfile:///data/json/encore.{0}.log"` (rotating files) and
> run `estreamer_ingest --source file` — survives ingester restarts, at the cost
> of disk under `/data/json` (watch capacity — see SETUP_GUIDE troubleshooting).

## 4. Test the handshake
```bash
cd /opt/eStreamer-eNcore
./encore.sh test          # prompts for the pkcs12 password; verifies TLS to :8302
```
Also, from this app, sanity-check reachability:
```bash
python manage.py probe_apis --out api_responses.json   # includes an 8302 TCP/TLS check
```

## 5. Run it and pipe into the ingester
Manual (first run — watch it in the foreground):
```bash
/opt/eStreamer-eNcore/encore.sh foreground \
  | /opt/iotdash/.venv/bin/python /opt/iotdash/manage.py estreamer_ingest --source stdin
```
As a service (already provided):
```bash
sudo systemctl enable --now iotdash-estreamer     # deploy/systemd/iotdash-estreamer.service
journalctl -u iotdash-estreamer -f                # watch events land
```

## 6. Capture a sample for parser tuning
The JSON field names eNcore emits depend on its version/config. Grab a handful:
```bash
/opt/eStreamer-eNcore/encore.sh foreground | head -50 > encore-sample.jsonl
```
Send `encore-sample.jsonl` (+ `api_responses.json`) back and the mapping in
`dashboard/estreamer/mapping.py` gets tuned to your exact fields.

## Gotchas
- **Firewall:** the eNcore host must reach **FMC:8302** outbound (firewalld/ACLs).
- **Cert host match:** the "Create Client" hostname/IP must match the eNcore
  host, or FMC refuses the connection.
- **Password:** `./encore.sh test` stores the pkcs12 password so the service can
  start unattended; re-run `test` if you rotate the cert.
- **Python:** eNcore v4+ needs Python 3.6+. RHEL 9's `python3.11` is fine.
- **Backpressure at scale:** for very high event rates, have eNcore write to a
  file / Kafka and run multiple `estreamer_ingest` consumers (see VM spec).
