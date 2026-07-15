# -*- coding: utf-8 -*-
"""RS-485 roster providers — bus-free sources for the aggregator.

A provider reads ONLY files already written by a bus-owning service (the MQTT
bridge) or by a past leased scan (the flasher). No provider ever opens a serial
port. Each provider yields normalized roster entries (plain dicts, python3.6-safe):

    { "port": "COM4", "addr": 6, "model": "AO6AI6", "ours": True,
      "online": True|False|None,   # None ⇒ liveness unknown (last-known scan)
      "source": "bridge"|"scan", "ts": <float epoch> }

Adding a Provider C later (MPLC4 SNMP / CODESYS OPC-UA) is a new class here plus a
new source tag; the aggregator, cache format, CGI and UI are untouched.
"""

import json
import os
import time

# Default source paths (overridable via env for tests / non-standard installs).
BRIDGE_ROSTER_PATH = os.environ.get(
    "SA02M_MQTT_ROSTER", "/run/sa02m-modbus-mqtt/_roster.json")
SCAN_CACHE_DIR = os.environ.get(
    "SA02M_FLASHER_SCAN_CACHE", "/var/lib/sa02m-flasher/last_scan")

# A bridge roster older than this is treated as stale (bridge stopped, e.g. a
# flasher scan took the port) — the aggregator then falls back to the scan cache.
BRIDGE_STALE_S = 60.0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _entry(port, addr, model, ours, online, source, ts):
    return {
        "port": str(port),
        "addr": int(addr),
        "model": str(model or ""),
        "ours": bool(ours),
        "online": online,
        "source": source,
        "ts": float(ts),
    }


class RosterProvider(object):
    """Provider contract: name, availability, and a flat list of roster entries."""

    name = "base"

    def available(self):
        raise NotImplementedError

    def entries(self):
        raise NotImplementedError


class MqttBridgeProvider(RosterProvider):
    """Provider A — live roster from the Modbus→MQTT bridge (best, real online)."""

    name = "bridge"

    def __init__(self, path=BRIDGE_ROSTER_PATH, stale_s=BRIDGE_STALE_S):
        self._path = path
        self._stale_s = stale_s

    def _load(self):
        data = _read_json(self._path)
        if not isinstance(data, dict):
            return None
        ts = data.get("ts")
        if not isinstance(ts, (int, float)):
            return None
        if (time.time() - float(ts)) > self._stale_s:
            return None  # stale ⇒ bridge not owning the port right now
        return data

    def available(self):
        return self._load() is not None

    def entries(self):
        data = self._load()
        if not data:
            return []
        ts = float(data.get("ts") or time.time())
        out = []
        for dev in data.get("devices") or []:
            try:
                out.append(_entry(
                    dev.get("port"), dev.get("addr"), dev.get("model"),
                    dev.get("ours"), bool(dev.get("online")), "bridge", ts))
            except (TypeError, ValueError):
                continue
        return out


class FlasherScanProvider(RosterProvider):
    """Provider B — last commissioning scan per port (universal fallback).

    Liveness is last-known, never real-time ⇒ online is always None here."""

    name = "scan"

    def __init__(self, cache_dir=SCAN_CACHE_DIR):
        self._dir = cache_dir

    def _files(self):
        try:
            names = os.listdir(self._dir)
        except OSError:
            return []
        return [os.path.join(self._dir, n) for n in names
                if n.endswith(".json") and not n.endswith(".json.tmp")]

    def available(self):
        return len(self._files()) > 0

    def entries(self):
        out = []
        for path in self._files():
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            port = data.get("port") or ""
            ts = float(data.get("ts") or 0)
            for dev in data.get("devices") or []:
                try:
                    out.append(_entry(
                        port, dev.get("addr"), dev.get("model"),
                        dev.get("ours"), None, "scan", ts))
                except (TypeError, ValueError):
                    continue
        return out
