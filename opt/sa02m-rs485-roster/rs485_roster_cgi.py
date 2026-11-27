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
import time

try:
    from roster_files import CONFIG_PATH, ROSTER_LIMIT, fresh, read_regular, regular_revision
except ModuleNotFoundError as exc:
    if __name__ == "__main__" and exc.name == "roster_files":
        sys.exit(0)
    raise

def _modules_for(block, default_ts, now=None):
    configured = block.get("source") == "configured"
    if now is None:
        now = time.time()
    ours = []
    for e in block.get("ours") or []:
        try:
            ours.append({
                "addr": int(e.get("addr")),
                "model": str(e.get("model") or ""),
                "online": e.get("online") if type(e.get("online")) is bool
                          and (not configured or fresh(e.get("observed_at"), now)) else None,
                "source": e.get("source"),
            })
        except (TypeError, ValueError):
            continue
    third = block.get("third_party") or {}
    return {
        "source": block.get("source"),
        "live": bool(block.get("live")) and (not configured or any(e["online"] is not None for e in ours)
                                              or fresh(block.get("third_party_latest_at"), now)),
        "ts": block.get("ts", default_ts),
        "ours": ours,
        "third_party": {
            "total": int(third.get("total", 0) or 0),
            "online": third.get("online") if not configured or fresh(block.get("third_party_observed_at"), now) else None,
        },
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/run/sa02m-rs485-roster.json"
    try:
        data = json.loads(read_regular(path, ROSTER_LIMIT)[0])
        if (not isinstance(data, dict) or not fresh(data.get("ts"), time.time())
                or not isinstance(data.get("polling_ports"), dict)
                or data.get("config_revision") != regular_revision(CONFIG_PATH)):
            return 0
    except (OSError, ValueError, RecursionError):
        return 0
    default_ts = data.get("ts")
    lines = []
    for com_key, block in data["polling_ports"].items():
        if com_key not in ("COM1", "COM2", "COM3", "COM4", "COM5"):
            continue
        if not isinstance(block, dict) or block.get("source") != "configured":
            continue
        n = int(com_key[-1]) - 1
        try:
            now = time.time()
            modules = _modules_for(block, default_ts, now)
            if not modules["live"] or not fresh(block.get("ts"), now):
                modules["live"] = False
                modules["third_party"]["online"] = None
                for chip in modules["ours"]:
                    chip["online"] = None
        except (AttributeError, ValueError, TypeError, OverflowError):
            continue
        lines.append("%d\t%s" % (
            n, json.dumps(modules, ensure_ascii=True, separators=(",", ":"))))
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
