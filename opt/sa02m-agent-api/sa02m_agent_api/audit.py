# -*- coding: utf-8 -*-
"""One JSON line per call. Argument values that are secrets are not hashed in."""

import hashlib
import json
import os
import threading
import time

_LOCK = threading.Lock()
_SECRET_KEYS = {"password", "secret", "token", "root_password", "current_password",
                "new_password", "new_password_confirm", "authorization"}


def _redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS:
                out[k] = "*"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 64:
        return "str:%d" % len(obj)
    return obj


def digest(args):
    raw = json.dumps(_redact(args if isinstance(args, dict) else {}),
                     sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record(path, token_id, op, ok, args=None, error=""):
    if not path:
        return
    line = {
        "ts": int(time.time()),
        "token": token_id or "",
        "op": op or "",
        "ok": bool(ok),
        "args": digest(args),
        "error": error or "",
    }
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        return
