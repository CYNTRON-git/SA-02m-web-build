# -*- coding: utf-8 -*-
"""API tokens. The disk file holds sha256 only. A token cannot mint a token.

Writes go through the root helper, except unit tests
(SA02M_AGENT_TOKEN_DIRECT=1) and a root storecli.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

SCOPES = ("read", "control", "config", "admin")
RANK = {name: i for i, name in enumerate(SCOPES)}
TOKEN_RE = re.compile(r"^sa02m_([0-9a-f]{8})\.([0-9a-f]{32,64})$")
ID_RE = re.compile(r"^[0-9a-f]{8}$")
NAME_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")

DEFAULT_PATH = "/etc/sa02m-agent-api/tokens.json"
DEFAULT_CAP_DIR = "/etc/sa02m-agent-api/root-cap"
RATE_PER_MIN = 60
ADMIN_PER_MIN = 10


def euid():
    fn = getattr(os, "geteuid", None)
    if fn is None:
        return 0
    return fn()


def writes_allowed():
    return euid() == 0 or os.environ.get("SA02M_AGENT_TOKEN_DIRECT") == "1"


def allows(have, need):
    need_rank = RANK.get(need)
    if need_rank is None:
        return False
    return any(RANK.get(s, -1) >= need_rank for s in (have or ()))


def expand(scopes):
    """A higher scope includes every lower one. Store that set explicitly."""
    picked = [s for s in (scopes or []) if s in RANK]
    if not picked:
        raise ValueError("scopes")
    top = max(RANK[s] for s in picked)
    return [s for s in SCOPES if RANK[s] <= top]


def hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RateLimiter:
    def __init__(self, per_min=RATE_PER_MIN):
        self.per_min = per_min
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key, limit=None):
        cap = self.per_min if limit is None else limit
        now = time.monotonic()
        with self._lock:
            prev = self._hits.get(key) or []
            kept = [t for t in prev if now - t < 60.0]
            if len(kept) >= cap:
                self._hits[key] = kept
                return False
            kept.append(now)
            self._hits[key] = kept
            return True


class TokenStore:
    def __init__(self, path=None, cap_dir=None):
        self.path = path or os.environ.get("SA02M_AGENT_TOKEN_FILE") or DEFAULT_PATH
        self.cap_dir = cap_dir or os.environ.get("SA02M_AGENT_CAP_DIR") or DEFAULT_CAP_DIR
        self._lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        rows = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict) and ID_RE.match(str(r.get("id") or ""))]

    def save(self, rows):
        if not writes_allowed():
            raise PermissionError("token store is written only by the root helper")
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        payload = json.dumps({"tokens": rows}, ensure_ascii=False, indent=2)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
        os.replace(tmp, self.path)

    def build(self, name, scopes, days, root_capable=False):
        if not NAME_RE.match(name or ""):
            raise ValueError("name")
        try:
            days_n = int(days)
        except (TypeError, ValueError):
            raise ValueError("days")
        if days_n < 0 or days_n > 3650:
            raise ValueError("days")
        expanded = expand(scopes)
        ident = secrets.token_hex(4)
        secret = secrets.token_hex(32)
        raw = "sa02m_%s.%s" % (ident, secret)
        now = int(time.time())
        row = {
            "id": ident,
            "sha256": hash_token(raw),
            "name": name,
            "scopes": expanded,
            "expires": 0 if days_n == 0 else now + days_n * 86400,
            "created": now,
            "last_used": 0,
            "root_capable": bool(root_capable),
        }
        return row, raw

    def issue(self, name, scopes, days, root_capable=False):
        row, raw = self.build(name, scopes, days, root_capable)
        with self._lock:
            rows = self.load()
            rows.append(row)
            self.save(rows)
        return row, raw

    def revoke(self, ident):
        if not ID_RE.match(ident or ""):
            return False
        with self._lock:
            rows = self.load()
            kept = [r for r in rows if r.get("id") != ident]
            if len(kept) == len(rows):
                return False
            self.save(kept)
        return True

    def authenticate(self, raw):
        m = TOKEN_RE.match((raw or "").strip())
        if not m:
            return None
        ident, _secret = m.group(1), m.group(2)
        digest = hash_token(raw.strip())
        for row in self.load():
            if row.get("id") != ident:
                continue
            if not hmac.compare_digest(str(row.get("sha256") or ""), digest):
                return None
            exp = int(row.get("expires") or 0)
            if exp and exp < int(time.time()):
                return None
            self._touch(ident)
            return row
        return None

    def _touch(self, ident):
        runtime = os.environ.get("SA02M_AGENT_RUNTIME") or "/run/sa02m-agent-api"
        try:
            os.makedirs(runtime, exist_ok=True)
            path = os.path.join(runtime, "last_used.json")
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data[ident] = int(time.time())
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except OSError:
            return

    def public_rows(self):
        out = []
        for row in self.load():
            out.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "scopes": row.get("scopes") or [],
                "expires": row.get("expires") or 0,
                "created": row.get("created") or 0,
                "last_used": row.get("last_used") or 0,
                "root_capable": bool(row.get("root_capable")),
            })
        return out
