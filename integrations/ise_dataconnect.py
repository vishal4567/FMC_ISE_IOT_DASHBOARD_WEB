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

SOURCE = "ISE-DataConnect"


class DataConnectError(Exception):
    pass


def _clean(v):
    """Make an Oracle value JSON-serialisable."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
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
    def _tz_string_handler(cursor, metadata):
        """Fetch TIMESTAMP WITH [LOCAL] TIME ZONE columns as strings. ISE stores
        some with NAMED zones, which python-oracledb thin mode can't parse
        (DPY-3022); returning them as VARCHAR sidesteps it."""
        import oracledb
        if metadata.type_code in (oracledb.DB_TYPE_TIMESTAMP_TZ,
                                  oracledb.DB_TYPE_TIMESTAMP_LTZ):
            return cursor.var(oracledb.DB_TYPE_VARCHAR, arraysize=cursor.arraysize)

    def _connect(self):
        import oracledb
        try:
            conn = oracledb.connect(params=self._params())
            conn.outputtypehandler = self._tz_string_handler
            return conn
        except Exception as exc:  # oracledb.Error or ssl/socket errors
            raise DataConnectError(f"Data Connect connect failed: {exc}") from exc

    # -- queries ------------------------------------------------------------
    def query(self, sql, params=None):
        """Run a SELECT; return ``(columns, rows)`` where rows are dicts.
        Column names are lower-cased."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or {})
            cols = [d[0].lower() for d in (cur.description or [])]
            rows = [{c: _clean(v) for c, v in zip(cols, rec)} for rec in cur]
            return cols, rows
        except DataConnectError:
            raise
        except Exception as exc:
            raise DataConnectError(f"Data Connect query failed: {exc}") from exc
        finally:
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
                "site": r.get("site", "") or "",
            })
        return out

    def location_by_mac(self, macs, *, view="radius_authentications",
                        mac_col="calling_station_id", loc_col="location"):
        """``{MAC: site}`` from the RADIUS view - one row per MAC (any non-null
        location). Batches the MAC list (Oracle IN caps at 1000)."""
        want = [m.upper() for m in macs if m]
        out = {}
        for i in range(0, len(want), 900):
            chunk = want[i:i + 900]
            binds = {f"m{j}": m for j, m in enumerate(chunk)}
            inlist = ", ".join(f":{k}" for k in binds)
            sql = (f"SELECT UPPER({mac_col}) AS mac, MAX({loc_col}) AS site "
                   f"FROM {view} WHERE UPPER({mac_col}) IN ({inlist}) "
                   f"AND {loc_col} IS NOT NULL GROUP BY UPPER({mac_col})")
            try:
                _, rows = self.query(sql, binds)
            except DataConnectError:
                continue
            for r in rows:
                if r.get("mac"):
                    out[str(r["mac"]).upper()] = r.get("site", "") or ""
        return out
