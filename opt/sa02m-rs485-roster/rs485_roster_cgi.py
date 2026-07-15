#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""status.cgi helper — emit per-port `modules` JSON for the RS-485 dashboard.

Reads the normalized roster (/run/sa02m-rs485-roster.json), maps each COM(N+1)
port to the UI's RS-485-N index, and prints one line per port:

    <n>\t<compact-modules-json>

Called once per status.cgi rs485 build (8 s part cache already fronts it). Pure
read — no bus. Output is pure-ASCII JSON (ensure_ascii) so it embeds safely in
the CGI's hand-assembled response (equivalent to json_escape for these values)."""

import json
import sys


def _port_index(com_key):
    """COM4 → 3 (RS-485-3). Returns None for an unparseable / non-positive key."""
    digits = "".join(ch for ch in str(com_key) if ch.isdigit())
    if not digits:
        return None
    n = int(digits) - 1
    return n if n >= 0 else None


def _modules_for(block, default_ts):
    ours = []
    for e in block.get("ours") or []:
        try:
            ours.append({
                "addr": int(e.get("addr")),
                "model": str(e.get("model") or ""),
                # tri-state: true/false = live (bridge), null = last-scan only.
                "online": e.get("online"),
                # per-chip source so the UI colours each chip from its OWN state,
                # never from the port-level flag (a scan chip on a bridge port must
                # stay grey, not render as a false live-offline red).
                "source": e.get("source"),
            })
        except (TypeError, ValueError):
            continue
    third = block.get("third_party") or {}
    return {
        "source": block.get("source"),
        "live": bool(block.get("live")),
        "ts": block.get("ts", default_ts),
        "ours": ours,
        "third_party": {
            "total": int(third.get("total", 0) or 0),
            "online": third.get("online"),
        },
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/run/sa02m-rs485-roster.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    default_ts = data.get("ts")
    lines = []
    for com_key, block in (data.get("ports") or {}).items():
        if not isinstance(block, dict):
            continue
        n = _port_index(com_key)
        if n is None:
            continue
        modules = _modules_for(block, default_ts)
        lines.append("%d\t%s" % (
            n, json.dumps(modules, ensure_ascii=True, separators=(",", ":"))))
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
