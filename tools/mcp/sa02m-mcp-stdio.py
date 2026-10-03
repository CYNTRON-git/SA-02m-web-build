#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stdio MCP bridge for clients that cannot speak Streamable HTTP.

SA02M_API_URL is the board origin (http://192.168.1.136:9999).
SA02M_API_TOKEN is the panel token. One JSON-RPC object per stdin line.
"""

import json
import os
import sys
import urllib.request


def main():
    base = (os.environ.get("SA02M_API_URL") or "").rstrip("/")
    token = os.environ.get("SA02M_API_TOKEN") or ""
    if not base or not token:
        sys.stderr.write("SA02M_API_URL and SA02M_API_TOKEN are required\n")
        return 2
    url = base + "/mcp"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = urllib.request.Request(
            url, data=line.encode("utf-8"), method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-SA02M-Token": token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                code = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            code = exc.code
        if code == 202 or not body:
            continue
        sys.stdout.write(body.decode("utf-8", "replace").rstrip() + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
