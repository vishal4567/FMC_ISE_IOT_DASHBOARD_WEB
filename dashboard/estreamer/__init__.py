"""Cisco FMC eStreamer event ingestion.

The FMC config REST API does not carry events; they are streamed over eStreamer
(TCP 8302, pkcs12 client cert). The supported collector is Cisco's **eNcore**
reference client, which parses the binary eStreamer protocol and emits JSON.

Pipeline:  FMC:8302  --eStreamer-->  eNcore  --JSON-->  estreamer_ingest  -->  SecurityEvent

Modules:
* ``mapping``   - map an eNcore/eStreamer JSON record onto the internal event dict
* ``collector`` - connectivity check + how to run eNcore and pipe into the ingester
"""
