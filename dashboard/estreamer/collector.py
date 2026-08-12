"""
eStreamer collector helpers.

The production collector is Cisco's **eNcore** (github.com/CiscoDevNet/eStreamer-eNcore),
which authenticates to FMC:8302 with a pkcs12 client certificate and emits JSON.
Run it and pipe its output into the ingester:

    encore.sh foreground | python manage.py estreamer_ingest --source stdin

This module only provides a lightweight connectivity check for diagnostics /
deployment validation - it does NOT implement the binary eStreamer protocol.
"""
from __future__ import annotations

import os
import socket
import ssl


def check_connectivity(host: str, port: int = 8302, pkcs12_path: str = "",
                       timeout: float = 5.0) -> dict:
    """Validate that the eStreamer port is reachable and the client cert exists.

    Returns a diagnostics dict (does not perform the eStreamer handshake)."""
    result = {"host": host, "port": port, "tcp_reachable": False,
              "tls_handshake": False, "cert_present": False, "detail": ""}
    result["cert_present"] = bool(pkcs12_path) and os.path.exists(pkcs12_path)

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            result["tcp_reachable"] = True
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    result["tls_handshake"] = True
                    result["detail"] = f"TLS peer: {ss.version()}"
            except ssl.SSLError as exc:
                result["detail"] = f"TLS: {exc}"
    except OSError as exc:
        result["detail"] = f"TCP: {exc}"
    return result
