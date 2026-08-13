# ISE data sources — the confirmed design

This build is IoT-scoped and reads each fact from the ISE interface that actually
carries it. The choices below were validated against the production ISE
(artifacts in the repo history) — not assumed.

## Summary

| Fact | Source (confirmed) | Rejected alternative |
|---|---|---|
| Which endpoints are IoT | ERS `GET /endpoint?filter=profileId.EQ.<id>` (allow-listed profiles only) | Walking all endpoints — this ISE has ~51k active sessions |
| Device type | ERS `/endpoint/{id}` → `mfcAttributes.mfcDeviceType[0]` (e.g. `Zebra-Device`) | Open API `deviceType` — **blank** in this deployment; endpoint `customAttributes` — only empty AD fields |
| Profile name | `profileId` → name via `/profilerprofile` (daily) | — |
| Site / location | MnT `Session/MACAddress/{mac}` → NAS IP/name → NAD **Location** group | Endpoint IP → subnet — **IP not reliably present** in any ISE API |
| Manufacturer | `mfcAttributes.mfcHardwareManufacturer[0]` | — |

## Why ERS, not Open API

The Open API was the natural first choice (it supports a `profileId` filter and a
rich schema), **but its `deviceType` / `ipAddress` fields are not populated in
this ISE.** Since the device type must come from ERS `mfcAttributes` regardless,
the Open API discovery call adds nothing — so ERS does both discovery and typing.

ERS endpoint filtering is documented to support these fields:
`logicalProfileName, portalUser, staticProfileAssignment, profileId, profile,
groupId, staticGroupAssignment, mac` — so both `profileId.EQ` (default) and
`logicalProfileName.EQ` (fewer calls: 3 logical profiles vs 21 policies) work.

The Open API path remains available as a fallback (`ISE_USE_OPENAPI=True`,
`ISE_ERS_ENRICH=True`) and degrades gracefully (client-side profileId filter if
the server filter is rejected), but is **off by default**.

## Why location comes from the NAD, not the device

The endpoint record has no location, and no ISE API reliably returns the
endpoint's IP here. Location is therefore derived from **which switch/WLC the
device authenticated through**: the live session gives the NAS IP (or NAD name),
which maps to that NAD's `Location#All Locations#<site>` group. This is exactly
how ISE's own Context Visibility "Location" column is populated. NADs that are
tagged only at the `Location#All Locations` root yield a blank site (honest).

## Correlating FMC events to ISE devices (identity, then activity)

The dashboard's identity for a device — **device type + location** — always comes
from **ISE**, keyed by MAC. FMC then supplies the **activity** (threats,
connections, policy hits) for those same devices. The catch: ISE keys by MAC,
FMC/eStreamer events are **IP-based**. The bridge is the **device IP that ISE
reports in the session** (`framed_ip_address`), stored on `IoTDevice.ip`.

At ingest, each FMC event is attributed to a device by:
1. **MAC** — if the eStreamer record carries one (`enrich_with_ise` → MAC map);
2. otherwise **IP** — the event's device/source/dest IP is looked up in the ISE
   IP map (`IoTDevice.ip`), and the event inherits that device's MAC, device
   type and site (`in_ise=True`).

Events that match neither are kept but flagged **FMC-only** (`in_ise=False`), so
the correlation view can show matched vs. unmatched. Because the device IP is
essential for this bridge, the hourly sync **always** captures it from the
session, regardless of the location method.

## Two cadences (Celery beat)

| Task | Interval | Work |
|---|---|---|
| `refresh_ise_reference` | daily (`ISE_REFERENCE_MINUTES=1440`) | resolve IoT profile names→ids; build `{NAS → site}` NAD Location map (cached in Redis) |
| `sync_iot_endpoints` | hourly (`IOT_SYNC_MINUTES=60`) | ERS filter → allow-listed endpoints → device type (mfc) + site (session→NAD) → upsert `IoTDevice` |

`IoTDevice` is the source of truth the eStreamer ingester stamps events against
(device_type / site / in_ise), so the dashboard's Site filter and per-type views
reflect real ISE identity for IoT devices only.

## Key settings (`.env.prod`)

```ini
ISE_USE_OPENAPI=False                 # ERS discovery (default)
ISE_ERS_ENRICH=True                   # device type from mfcAttributes
ISE_LOCATION_METHOD=session           # NAD Location via session→NAS (IP not needed)
# ISE_IOT_PROFILES=...                # defaults to the built-in 21 IoT policies
# ISE_IOT_LOGICAL_PROFILES=Wipro_CCTV,Wipro-Access-Control,Wipro-BMS  # fewer calls
```

## Confirm against a live ISE

```bash
manage.py probe_apis        # ise.iot.profile_filter → counts for ers_by_profileId,
                            #   ers_by_logicalProfile, openapi_by_profileId
manage.py sync_ise          # → {'iot_endpoints': N, 'with_site': M}
```
