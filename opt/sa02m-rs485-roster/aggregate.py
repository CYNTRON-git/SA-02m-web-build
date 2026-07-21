#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RS-485 roster aggregator — the single writer of /run/sa02m-rs485-roster.json.

Reads Provider A (MQTT bridge, live) + Provider B (flasher scan cache, last-known),
merges by (port, addr) preferring the live bridge entry, tags every entry with
source/live/ours, and writes ONE normalized cache the status.cgi rs485 part reads.

NEVER opens a serial port — every input is a file already written by a bus-owning
service or a past leased scan. Run by a systemd timer (~8 s). ARM-cheap: a few
small file reads + a dict merge + one atomic write.

Output shape (consumed by status.cgi and the cloud heartbeat filler):
    { "ts": <epoch>,
      "ports": { "COM4": { "source": "bridge", "live": true, "ts": <epoch>,
                           "ours": [ {"addr": 6, "model": "6AI6AO",
                                      "online": true, "source": "bridge"} ],
                           "third_party": { "total": 3, "online": 2 } } } }
"""

import json
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from providers import MqttBridgeProvider, FlasherScanProvider  # noqa: E402

OUTPUT_PATH = os.environ.get(
    "SA02M_RS485_ROSTER_OUT", "/run/sa02m-rs485-roster.json")

# Bridge (live) outranks scan (last-known) for the same (port, addr).
_SOURCE_RANK = {"bridge": 2, "scan": 1}


def _default_providers():
    return [MqttBridgeProvider(), FlasherScanProvider()]


def merge_entries(entries):
    """Collapse entries by (port, addr): higher source rank wins, then fresher ts."""
    best = {}
    for e in entries:
        key = (e["port"], e["addr"])
        cur = best.get(key)
        if cur is None:
            best[key] = e
            continue
        rank_new = _SOURCE_RANK.get(e["source"], 0)
        rank_cur = _SOURCE_RANK.get(cur["source"], 0)
        if rank_new > rank_cur or (rank_new == rank_cur and e["ts"] > cur["ts"]):
            best[key] = e
    return list(best.values())


def build_ports(entries):
    """Group merged entries into the per-port normalized blocks."""
    by_port = {}
    for e in entries:
        by_port.setdefault(e["port"], []).append(e)

    ports = {}
    for port, items in by_port.items():
        live = any(it["source"] == "bridge" for it in items)
        source = "bridge" if live else "scan"
        ts = max((it["ts"] for it in items), default=0.0)

        ours = [
            {"addr": it["addr"], "model": it["model"],
             "online": it["online"], "source": it["source"]}
            for it in items if it["ours"]
        ]
        ours.sort(key=lambda r: r["addr"])

        third = [it for it in items if not it["ours"]]
        # Third-party online is only known from a live source; a scan-only port
        # cannot claim liveness ⇒ report null rather than a false zero.
        if any(it["online"] is not None for it in third):
            online_count = sum(1 for it in third if it["online"] is True)
        else:
            online_count = None
        third_party = {"total": len(third), "online": online_count}

        ports[port] = {
            "source": source,
            "live": live,
            "ts": ts,
            "ours": ours,
            "third_party": third_party,
        }
    return ports


def aggregate(providers=None):
    providers = providers if providers is not None else _default_providers()
    entries = []
    for provider in providers:
        try:
            if provider.available():
                entries.extend(provider.entries())
        except Exception:  # noqa: BLE001 — a bad provider must not sink the rest
            # Never silently swallow: report to stderr (journald) so a broken
            # provider is diagnosable; the other providers still run.
            sys.stderr.write("rs485-roster: provider %r failed:\n%s"
                             % (getattr(provider, "name", "?"),
                                traceback.format_exc()))
            continue
    ports = build_ports(merge_entries(entries))
    return {"ts": time.time(), "ports": ports}


def write_atomic(payload, path=OUTPUT_PATH):
    """Write world-readable (0644) so www-data reads it; atomic tmp+replace."""
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".rs485-roster.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    payload = aggregate()
    write_atomic(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
