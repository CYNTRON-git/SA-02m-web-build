#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish legacy discovery inventory and config-backed polling membership together.

The legacy ports map merges active bridge rows and last-scan files. The dashboard
gets polling_ports from the current saved setup and confirmed bridge bindings.
The worker never opens a bus. See docs/contracts/rs485-roster.md.
"""

import json
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from providers import MqttBridgeProvider, FlasherScanProvider  # noqa: E402
    from polling_roster import projection  # noqa: E402
except ModuleNotFoundError as exc:
    if __name__ == "__main__" and exc.name in ("roster_files", "polling_roster"):
        sys.exit(0)
    raise

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


def aggregate(providers=None, **projection_options):
    providers = providers if providers is not None else _default_providers()
    entries = []
    runtime = None
    for provider in providers:
        try:
            if isinstance(provider, MqttBridgeProvider):
                runtime = provider.snapshot()
                entries.extend(provider.entries_from(runtime))
            elif provider.available():
                entries.extend(provider.entries())
        except Exception:  # noqa: BLE001 — a bad provider must not sink the rest
            # Never silently swallow: report to stderr (journald) so a broken
            # provider is diagnosable; the other providers still run.
            sys.stderr.write("rs485-roster: provider %r failed:\n%s"
                             % (getattr(provider, "name", "?"),
                                traceback.format_exc()))
            continue
    ports = build_ports(merge_entries(entries))
    now = time.time()
    return {"ts": now, "ports": ports, **projection(runtime, now, **projection_options)}


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
    if not hasattr(MqttBridgeProvider, "snapshot"):
        return 0  # The preceding release's provider is still installed.
    payload = aggregate()
    write_atomic(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
