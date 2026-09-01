"""
Cisco ISE Data Connect client - read-only SQL over ISE's reporting database.

Cisco recommends Data Connect (not ERS/Open API polling) for bulk retrieval on
large deployments (millions of endpoints). One indexed SQL query returns just
the IoT subset in seconds - no REST pagination, no rate limits, no full-table
filter scans.

Transport: python-oracledb in THIN mode (pure Python, no Oracle Instant Client),
over TLS (TCPS) on port 2484, service ``cpm10``, user ``dataconnect``. The connect
recipe follows Cisco DevNet / the community iseql reference:
    ConnectParams(protocol="tcps", ..., ssl_context=ctx, ssl_server_dn_match=False)

``oracledb`` is imported lazily so the app/manage.py run without it installed.
"""
from __future__ import annotations

import datetime as _dt
import decimal as _dec
import time as _time
from contextlib import contextmanager

SOURCE = "ISE-DataConnect"


class DataConnectError(Exception):
    pass


def _loc_leaf(v):
    """RADIUS/NDG location is a hierarchy path like
    'All Locations#India#Mumbai#BLDG5' - return the leaf ('BLDG5')."""
    v = str(v or "").strip()
    return v.rsplit("#", 1)[-1].strip() if "#" in v else v


def _clean(v):
    """Make an Oracle value JSON-serialisable."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, bytes):     # bypass_decode gives raw bytes -> lenient decode
        return v.decode("utf-8", "replace")
    if isinstance(v, _dec.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    try:                    # CLOB/LOB and anything else -> text
        return v.read() if hasattr(v, "read") else str(v)
    except Exception:
        return str(v)


class DataConnectClient:
    def __init__(self, host, password, *, port=2484, service_name="cpm10",
                 user="dataconnect", verify_tls=False, ca_cert="", timeout=60):
        if not host or not password:
            raise DataConnectError("Data Connect host and password are required "
                                   "(set ISE_DATACONNECT_HOST / _PASSWORD).")
        self.host = host
        self.password = password
        self.port = int(port)
        self.service_name = service_name
        self.user = user
        self.verify_tls = verify_tls
        self.ca_cert = ca_cert
        self.timeout = int(timeout)
        self.log = None        # optional callable(msg) for per-query progress
        self._conn = None      # reused connection inside a session()

    def _say(self, msg):
        if callable(self.log):
            self.log(msg)

    @contextmanager
    def session(self):
        """Reuse ONE connection for all queries inside the block (each connect is
        a ~2s TLS handshake, so this matters for multi-query work)."""
        t = _time.monotonic()
        conn = self._connect()
        self._say(f"[dc] connected {self.host}:{self.port}/{self.service_name} "
                  f"({round(_time.monotonic()-t,2)}s)")
        self._conn = conn
        try:
            yield self
        finally:
            self._conn = None
            try:
                conn.close()
            except Exception:
                pass
            self._say("[dc] connection closed")

    # -- connection ---------------------------------------------------------
    def _params(self):
        import ssl
        import oracledb

        ctx = ssl.create_default_context()
        if self.verify_tls:
            if self.ca_cert:
                ctx.load_verify_locations(cafile=self.ca_cert)
        else:
            # ISE ships a self-signed cert by default; accept it.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return oracledb.ConnectParams(
            protocol="tcps", host=self.host, port=self.port,
            service_name=self.service_name, user=self.user, password=self.password,
            retry_count=2, retry_delay=3, tcp_connect_timeout=self.timeout,
            ssl_context=ctx, ssl_server_dn_match=False,
        )

    @staticmethod
    def _out_handler(cursor, metadata):
        """Make fetches robust against ISE data quirks in thin mode:
        - TIMESTAMP WITH [LOCAL] TIME ZONE with NAMED zones -> fetch as VARCHAR
          (else DPY-3022 'named time zones are not supported').
        - text columns -> bypass_decode so bad/non-UTF-8 bytes come back raw and
          _clean() decodes them leniently (else UnicodeDecodeError on garbled data).
        """
        import oracledb
        t = metadata.type_code
        if t in (oracledb.DB_TYPE_TIMESTAMP_TZ, oracledb.DB_TYPE_TIMESTAMP_LTZ):
            return cursor.var(oracledb.DB_TYPE_VARCHAR, arraysize=cursor.arraysize)
        if t in (oracledb.DB_TYPE_VARCHAR, oracledb.DB_TYPE_CHAR,
                 oracledb.DB_TYPE_NVARCHAR, oracledb.DB_TYPE_NCHAR,
                 oracledb.DB_TYPE_LONG):
            return cursor.var(t, arraysize=cursor.arraysize, bypass_decode=True)

    def _connect(self):
        import oracledb
        try:
            conn = oracledb.connect(params=self._params())
            conn.outputtypehandler = self._out_handler
            return conn
        except Exception as exc:  # oracledb.Error or ssl/socket errors
            raise DataConnectError(f"Data Connect connect failed: {exc}") from exc

    # -- queries ------------------------------------------------------------
    def query(self, sql, params=None):
        """Run a SELECT; return ``(columns, rows)``. Logs the SQL + row count +
        seconds via self.log. Reuses the session connection if one is open."""
        own = self._conn is None
        conn = self._conn or self._connect()
        label = " ".join(sql.split())
        self._say(f"[dc] SQL: {label[:110]}{' …' if len(label) > 110 else ''}"
                  + (f"  ({len(params)} binds)" if params else ""))
        t = _time.monotonic()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or {})
            cols = [d[0].lower() for d in (cur.description or [])]
            rows = [{c: _clean(v) for c, v in zip(cols, rec)} for rec in cur]
            self._say(f"[dc]   -> {len(rows)} rows in {round(_time.monotonic()-t,2)}s")
            return cols, rows
        except DataConnectError:
            raise
        except Exception as exc:
            self._say(f"[dc]   -> ERROR in {round(_time.monotonic()-t,2)}s: "
                      f"{str(exc)[:100]}")
            raise DataConnectError(f"Data Connect query failed: {exc}") from exc
        finally:
            if own:
                try:
                    conn.close()
                except Exception:
                    pass

    def test(self):
        cols, rows = self.query("SELECT 1 AS ok FROM dual")
        return {"ok": bool(rows)}

    def list_views(self):
        """View/table names the dataconnect user can read (best effort)."""
        for sql in (
            "SELECT view_name AS name FROM user_views ORDER BY view_name",
            "SELECT table_name AS name FROM all_tables "
            "WHERE owner NOT IN ('SYS','SYSTEM') ORDER BY table_name",
        ):
            try:
                _, rows = self.query(sql)
                if rows:
                    return [r["name"] for r in rows]
            except DataConnectError:
                continue
        return []

    def columns(self, view):
        """Column names of a view. Uses WHERE 1=0 so NO rows are fetched - this
        avoids DPY-3022 on views with a named-time-zone timestamp column."""
        cols, _ = self.query(f"SELECT * FROM {view} WHERE 1=0")
        return cols

    def sample(self, view, n=5):
        """First ``n`` rows, TO_CHAR'ing any TIMESTAMP columns so named-time-zone
        values (unsupported in thin mode) come back as strings."""
        import oracledb

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {view} WHERE 1=0")   # describe only
            tz = (oracledb.DB_TYPE_TIMESTAMP, oracledb.DB_TYPE_TIMESTAMP_TZ,
                  oracledb.DB_TYPE_TIMESTAMP_LTZ)
            parts = []
            for d in cur.description:
                name, tcode = d[0], d[1]
                parts.append(f"TO_CHAR({name}) AS {name}" if tcode in tz else name)
            cur.execute(f"SELECT {', '.join(parts)} FROM {view} "
                        f"FETCH FIRST {int(n)} ROWS ONLY")
            cols = [c[0].lower() for c in cur.description]
            rows = [{c: _clean(v) for c, v in zip(cols, rec)} for rec in cur]
            return cols, rows
        except Exception as exc:
            raise DataConnectError(f"sample({view}) failed: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def count(self, view):
        _, rows = self.query(f"SELECT COUNT(*) AS n FROM {view}")
        return rows[0]["n"] if rows else None

    def sessions_for_macs(self, macs, view, columns, *, mac_col="calling_station_id",
                          extra_where=""):
        """Sessions for ONLY the given MACs, via an indexed
        ``calling_station_id IN (…)`` filter (batched). This removes non-IoT
        devices in the query rather than after. ``extra_where`` lets you scope to
        active/current records. ``location`` fields get leaf-extracted."""
        want = [m.upper() for m in macs if m]
        out = []
        n = (len(want) + 899) // 900
        with self.session():
            for bi, i in enumerate(range(0, len(want), 900), 1):
                chunk = want[i:i + 900]
                self._say(f"[dc] sessions batch {bi}/{n} ({len(chunk)} MACs)")
                binds = {f"m{j}": m for j, m in enumerate(chunk)}
                where = f"{mac_col} IN ({', '.join(f':{k}' for k in binds)})"
                if extra_where:
                    where += f" AND {extra_where}"
                try:
                    _, rows = self.query(
                        f"SELECT {', '.join(columns)} FROM {view} WHERE {where}", binds)
                except DataConnectError:
                    continue
                for r in rows:
                    if "location" in r:
                        r["location"] = _loc_leaf(r.get("location"))
                    out.append(r)
        return out

    def rows(self, view, columns, *, where="", binds=None, order="", limit=0):
        """Simple SELECT of named columns (avoids SELECT * so odd columns can't
        break the fetch). Returns list[dict]."""
        sql = f"SELECT {', '.join(columns)} FROM {view}"
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        if limit:
            sql += f" FETCH FIRST {int(limit)} ROWS ONLY"
        _, rows = self.query(sql, binds or {})
        return rows

    # -- IoT discovery ------------------------------------------------------
    def iot_endpoints(self, profiles, *, view, col_mac, col_profile,
                      col_group="", col_devicetype="", col_ip="", col_site="",
                      limit=0):
        """Return inventory rows for endpoints whose profile is in ``profiles``,
        using one filtered SQL query. Column names are configurable because the
        Data Connect schema is confirmed via probe_dataconnect first."""
        if not profiles:
            return []
        select = [f"{col_mac} AS mac", f"{col_profile} AS profile"]
        if col_group:
            select.append(f"{col_group} AS grp")
        if col_devicetype:
            select.append(f"{col_devicetype} AS device_type")
        if col_ip:
            select.append(f"{col_ip} AS ip")
        if col_site:
            select.append(f"{col_site} AS site")
        binds = {f"p{i}": name for i, name in enumerate(profiles)}
        inlist = ", ".join(f":{k}" for k in binds)
        sql = (f"SELECT {', '.join(select)} FROM {view} "
               f"WHERE {col_profile} IN ({inlist})")
        if limit:
            sql += f" FETCH FIRST {int(limit)} ROWS ONLY"
        _, rows = self.query(sql, binds)
        out = []
        for r in rows:
            mac = str(r.get("mac") or "").upper()
            if not mac:
                continue
            out.append({
                "mac": mac,
                "endpoint_profile": r.get("profile", "") or "",
                "logical_profile": r.get("grp", "") or "",
                "device_type": r.get("device_type", "") or r.get("profile", "") or "",
                "ip": r.get("ip", "") or "",
                "site": _loc_leaf(r.get("site", "")),
            })
        return out

    def iot_by_authz(self, *, view="radius_authentication_summary",
                     mac_col="calling_station_id",
                     authz_col="authorization_profiles", match="IOT",
                     col_profile="endpoint_profile", col_devicetype="device_type",
                     col_ip="framed_ip_address", col_site="location", limit=0):
        """Discover IoT endpoints from the RADIUS summary where the AUTHORIZATION
        profile name contains ``match`` (case-insensitive). One row per MAC, in
        the same shape as iot_endpoints() so it feeds the same sync pipeline."""
        select = [f"{mac_col} AS mac", f"MAX({authz_col}) AS authz"]
        if col_profile:
            select.append(f"MAX({col_profile}) AS profile")
        if col_devicetype:
            select.append(f"MAX({col_devicetype}) AS device_type")
        if col_ip:
            select.append(f"MAX({col_ip}) AS ip")
        if col_site:
            select.append(f"MAX({col_site}) AS site")
        sql = (f"SELECT {', '.join(select)} FROM {view} "
               f"WHERE UPPER({authz_col}) LIKE :m GROUP BY {mac_col}")
        if limit:
            sql += f" FETCH FIRST {int(limit)} ROWS ONLY"
        _, rows = self.query(sql, {"m": f"%{match.upper()}%"})
        out = []
        for r in rows:
            mac = str(r.get("mac") or "").upper()
            if not mac:
                continue
            out.append({
                "mac": mac,
                "endpoint_profile": r.get("profile", "") or "",
                "logical_profile": "",
                "device_type": r.get("device_type", "") or r.get("profile", "") or "",
                "ip": r.get("ip", "") or "",
                "site": _loc_leaf(r.get("site", "")),
                "authz_profile": r.get("authz", "") or "",
            })
        self._say(f"[dc] IoT-by-authz ('{match}'): {len(out)} unique MACs")
        return out

    def iot_by_logical_profiles(self, logical_profiles=None, *, match="",
                                endpoints_view="endpoints_data",
                                mac_col="mac_address",
                                profile_col="endpoint_policy",
                                ip_col="endpoint_ip",
                                lp_view="logical_profiles",
                                lp_name_col="logical_profile",
                                lp_policy_col="assigned_policies", limit=0):
        """Discover IoT endpoints that belong to LOGICAL PROFILES - the explicit
        ``logical_profiles`` list AND/OR ALL whose name contains ``match`` (e.g.
        'IOT', case-insensitive), combined (union). Expands them to member
        profiling policies (``assigned_policies``) and selects those endpoints.
        Returns mac + device_type (= endpoint_policy) + ip in the
        iot_endpoints() shape; site is resolved separately from the NAD name."""
        binds, conds = {}, []
        if logical_profiles:
            lkeys = []
            for i, name in enumerate(logical_profiles):
                binds[f"l{i}"] = name
                lkeys.append(f":l{i}")
            conds.append(f"{lp_name_col} IN ({', '.join(lkeys)})")
        if match:
            binds["lpm"] = f"%{match.upper()}%"
            conds.append(f"UPPER({lp_name_col}) LIKE :lpm")
        if not conds:
            return []
        lp_where = "(" + " OR ".join(conds) + ")"
        select = [f"{mac_col} AS mac", f"MAX({profile_col}) AS profile"]
        if ip_col:
            select.append(f"MAX({ip_col}) AS ip")
        sql = (f"SELECT {', '.join(select)} FROM {endpoints_view} "
               f"WHERE {profile_col} IN (SELECT {lp_policy_col} FROM {lp_view} "
               f"WHERE {lp_where}) GROUP BY {mac_col}")
        if limit:
            sql += f" FETCH FIRST {int(limit)} ROWS ONLY"
        _, rows = self.query(sql, binds)
        out = []
        for r in rows:
            mac = str(r.get("mac") or "").upper()
            if not mac:
                continue
            out.append({
                "mac": mac,
                "endpoint_profile": r.get("profile", "") or "",
                "logical_profile": "",
                "device_type": r.get("profile", "") or "",
                "ip": r.get("ip", "") or "",
                "site": "",
            })
        scope = []
        if logical_profiles:
            scope.append(f"{len(logical_profiles)} named")
        if match:
            scope.append(f"~'{match}'")
        self._say(f"[dc] logical-profile discovery ({' + '.join(scope)}): "
                  f"{len(out)} endpoints")
        return out

    def ip_by_mac(self, macs, *, view="endpoints_data", mac_col="mac_address",
                  ip_col="endpoint_ip"):
        """``{MAC: ip}`` from the endpoints view - used to backfill the device IP
        when the discovery source (e.g. RADIUS summary) has no endpoint-IP
        column. Batched IN (900)."""
        want = [m.upper() for m in macs if m]
        out = {}
        with self.session():
            for i in range(0, len(want), 900):
                chunk = want[i:i + 900]
                binds = {f"m{j}": m for j, m in enumerate(chunk)}
                inlist = ", ".join(f":{k}" for k in binds)
                sql = (f"SELECT {mac_col} AS mac, MAX({ip_col}) AS ip "
                       f"FROM {view} WHERE UPPER({mac_col}) IN ({inlist}) "
                       f"AND {ip_col} IS NOT NULL GROUP BY {mac_col}")
                try:
                    _, rows = self.query(sql, binds)
                except DataConnectError:
                    continue
                for r in rows:
                    if r.get("mac"):
                        out[str(r["mac"]).upper()] = str(r.get("ip") or "")
        return out

    def endpoint_attrs_by_mac(self, macs, *, view="endpoints_data",
                              mac_col="mac_address", ip_col="endpoint_ip",
                              profile_col="endpoint_policy"):
        """``{MAC: {'ip':.., 'profile':..}}`` from the endpoints view - backfills
        endpoint IP and the profiling policy (device type) when the discovery
        source (e.g. RADIUS summary) lacks them. Batched IN (900)."""
        want = [m.upper() for m in macs if m]
        cols = [f"{mac_col} AS mac"]
        if ip_col:
            cols.append(f"MAX({ip_col}) AS ip")
        if profile_col:
            cols.append(f"MAX({profile_col}) AS profile")
        out = {}
        with self.session():
            for i in range(0, len(want), 900):
                chunk = want[i:i + 900]
                binds = {f"m{j}": m for j, m in enumerate(chunk)}
                inlist = ", ".join(f":{k}" for k in binds)
                sql = (f"SELECT {', '.join(cols)} FROM {view} "
                       f"WHERE UPPER({mac_col}) IN ({inlist}) GROUP BY {mac_col}")
                try:
                    _, rows = self.query(sql, binds)
                except DataConnectError:
                    continue
                for r in rows:
                    if r.get("mac"):
                        out[str(r["mac"]).upper()] = {
                            "ip": str(r.get("ip") or ""),
                            "profile": str(r.get("profile") or ""),
                        }
        return out

    def location_by_mac(self, macs, *, view="radius_authentications",
                        mac_col="calling_station_id", loc_col="location",
                        days=0, time_col="timestamp"):
        """``{MAC: site}`` from a RADIUS view - one row per MAC. Optimisations:
        - filter the MAC column DIRECTLY (no UPPER()) so its index is used;
        - optional ``days`` window on ``time_col`` so a time-partitioned auth log
          is partition-pruned (what ISE's own dashboards do) - huge on the full
          radius_authentications; batches the MAC list (Oracle IN caps at 1000)."""
        want = [m.upper() for m in macs if m]
        out = {}
        n_batches = (len(want) + 899) // 900
        window = (f" AND {time_col} >= SYSTIMESTAMP - INTERVAL '{int(days)}' DAY"
                  if days and time_col else "")
        with self.session():   # reuse one connection across all batches
            for bi, i in enumerate(range(0, len(want), 900), 1):
                chunk = want[i:i + 900]
                self._say(f"[dc] location batch {bi}/{n_batches} ({len(chunk)} MACs"
                          + (f", last {days}d)" if days else ")"))
                binds = {f"m{j}": m for j, m in enumerate(chunk)}
                inlist = ", ".join(f":{k}" for k in binds)
                sql = (f"SELECT {mac_col} AS mac, MAX({loc_col}) AS site "
                       f"FROM {view} WHERE {mac_col} IN ({inlist}) "
                       f"AND {loc_col} IS NOT NULL{window} GROUP BY {mac_col}")
                try:
                    _, rows = self.query(sql, binds)
                except DataConnectError:
                    continue
                for r in rows:
                    if r.get("mac"):
                        out[str(r["mac"]).upper()] = _loc_leaf(r.get("site", ""))
        return out

    # ------------------------------------------------------------------ #
    # Location from the NAD HOSTNAME (site-code table) instead of the
    # RADIUS location hierarchy: endpoint --nas_ip--> NETWORK_DEVICES.name
    # (hostname) --site-code--> friendly site name.
    # ------------------------------------------------------------------ #
    def nad_ip_to_hostname(self, *, view="network_devices", name_col="name",
                           ip_col="ip_mask"):
        """``{nad_ip: hostname}`` from NETWORK_DEVICES. ``ip_mask`` may be
        '10.1.2.3/32' or '10.1.2.3 255.255.255.255' - keyed on the bare IP."""
        _, rows = self.query(f"SELECT {name_col} AS name, {ip_col} AS ip "
                             f"FROM {view}")
        out = {}
        for r in rows:
            raw = str(r.get("ip") or "")
            ip = raw.replace("/", " ").split()[0].strip() if raw else ""
            if ip:
                out[ip] = str(r.get("name") or "")
        return out

    def nas_ip_by_mac(self, macs, *, view="radius_authentication_summary",
                      mac_col="calling_station_id", nas_col="nas_ip_address"):
        """``{MAC: nas_ip}`` - which NAD each endpoint authenticated through.
        Indexed IN filter on the MAC column, batched at 900."""
        want = [m.upper() for m in macs if m]
        out = {}
        with self.session():
            for i in range(0, len(want), 900):
                chunk = want[i:i + 900]
                binds = {f"m{j}": m for j, m in enumerate(chunk)}
                inlist = ", ".join(f":{k}" for k in binds)
                sql = (f"SELECT {mac_col} AS mac, MAX({nas_col}) AS nas_ip "
                       f"FROM {view} WHERE {mac_col} IN ({inlist}) "
                       f"AND {nas_col} IS NOT NULL GROUP BY {mac_col}")
                try:
                    _, rows = self.query(sql, binds)
                except DataConnectError:
                    continue
                for r in rows:
                    if r.get("mac"):
                        out[str(r["mac"]).upper()] = str(r.get("nas_ip") or "")
        return out

    def location_by_nad_hostname(self, macs, *, matcher=None,
                                 nd_view="network_devices",
                                 nd_name_col="name", nd_ip_col="ip_mask",
                                 radius_view="radius_authentication_summary",
                                 mac_col="calling_station_id",
                                 nas_col="nas_ip_address"):
        """``{MAC: site}`` derived from the endpoint's NAD hostname via the
        site-code table. ``matcher`` is a build_matcher() result (pass the
        DB-backed one from dashboard.site_mapping; defaults to the built-in
        list). Two small queries: the NAD inventory (IP->hostname) and the
        endpoints' nas_ip - then match in code."""
        from integrations.location_map import build_matcher, site_from_hostname

        if matcher is None:
            matcher = build_matcher()
        ip_host = self.nad_ip_to_hostname(view=nd_view, name_col=nd_name_col,
                                          ip_col=nd_ip_col)
        self._say(f"[dc] {len(ip_host)} NADs from {nd_view}")
        mac_nas = self.nas_ip_by_mac(macs, view=radius_view, mac_col=mac_col,
                                     nas_col=nas_col)
        out = {}
        for mac, nas_ip in mac_nas.items():
            out[mac] = site_from_hostname(ip_host.get(nas_ip, ""), matcher)
        matched = sum(1 for v in out.values() if v)
        self._say(f"[dc] NAD-hostname location: {matched}/{len(out)} macs -> site")
        return out

    def location_by_device_name(self, macs, *, matcher=None,
                                view="radius_authentication_summary",
                                mac_col="calling_station_id",
                                host_col="device_name", time_col="", days=0):
        """``{MAC: site}`` from the NAD hostname carried in the RADIUS view
        (``device_name``), matched via the site-code table. The RADIUS log has
        many rows per MAC, so when ``time_col`` is given we take the hostname
        from the LATEST row per MAC (``MAX(host) KEEP DENSE_RANK LAST ORDER BY
        time``); ``days`` optionally time-bounds the scan for partition pruning.
        One indexed query per batch, no NETWORK_DEVICES join."""
        from integrations.location_map import build_matcher, site_from_hostname

        if matcher is None:
            matcher = build_matcher()
        want = [m.upper() for m in macs if m]
        window = (f" AND {time_col} >= SYSTIMESTAMP - INTERVAL '{int(days)}' DAY"
                  if days and time_col else "")
        host_expr = (f"MAX({host_col}) KEEP (DENSE_RANK LAST ORDER BY {time_col})"
                     if time_col else f"MAX({host_col})")
        out = {}
        with self.session():
            for i in range(0, len(want), 900):
                chunk = want[i:i + 900]
                binds = {f"m{j}": m for j, m in enumerate(chunk)}
                inlist = ", ".join(f":{k}" for k in binds)
                sql = (f"SELECT {mac_col} AS mac, {host_expr} AS host "
                       f"FROM {view} WHERE {mac_col} IN ({inlist}) "
                       f"AND {host_col} IS NOT NULL{window} GROUP BY {mac_col}")
                try:
                    _, rows = self.query(sql, binds)
                except DataConnectError:
                    continue
                for r in rows:
                    if r.get("mac"):
                        out[str(r["mac"]).upper()] = site_from_hostname(
                            r.get("host", ""), matcher)
        matched = sum(1 for v in out.values() if v)
        self._say(f"[dc] device_name location: {matched}/{len(out)} macs -> site")
        return out
