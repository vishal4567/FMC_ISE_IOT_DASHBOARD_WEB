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
Edit `estreamer.conf` (JSON). The keys that matter:

```jsonc
{
  "connections": [
    { "server": "<FMC-IP>", "port": 8302 }        // eStreamer endpoint
  ],
  "pkcs12Filepath": "client.pkcs12",              // the cert from step 1
  "subscription": {                               // which records to request
    "records": {
      "intrusion": true, "connection": true,
      "fileEvent": true, "malware": true, "impact": true
    }
  },
  "handler": {
    "outputters": [
      { "adapter": "json",                        // <-- JSON output
        "stream": { "uri": "stdout://" } }        // <-- to stdout so we can pipe
    ]
  }
}
```
- **`adapter: "json"`** + **`stream.uri: "stdout://"`** is what lets us pipe into
  the ingester. (Alternatively write to a file: `"relfile://encore.log"` and run
  `estreamer_ingest --source file --path encore.log`.)
- Exact key names vary slightly by eNcore version — open your `estreamer.conf` and
  set the server/port, the pkcs12 path, the subscriptions, and a **json/stdout**
  outputter.

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
