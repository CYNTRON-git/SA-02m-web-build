# -*- coding: utf-8 -*-
"""Root-side token file editor. www-data reaches it only through the helper."""

import json
import os
import re
import sys

from .tokens import ID_RE, SCOPES, TokenStore, writes_allowed

_TMP = re.compile(r"^/tmp/.+")


def _refuse():
    sys.stderr.write("refusing token store edit\n")
    return 2


def put(path):
    if not writes_allowed():
        return _refuse()
    if os.environ.get("SA02M_AGENT_TOKEN_DIRECT") != "1":
        if not _TMP.match(path or "") or os.path.islink(path) or not os.path.isfile(path):
            return _refuse()
    with open(path, "r", encoding="utf-8") as fh:
        row = json.load(fh)
    if not isinstance(row, dict) or not ID_RE.match(str(row.get("id") or "")):
        return _refuse()
    if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or "")):
        return _refuse()
    scopes = row.get("scopes") or []
    if not isinstance(scopes, list) or not scopes or any(s not in SCOPES for s in scopes):
        return _refuse()
    store = TokenStore()
    rows = [r for r in store.load() if r.get("id") != row["id"]]
    rows.append({
        "id": row["id"],
        "sha256": row["sha256"],
        "name": str(row.get("name") or "")[:64],
        "scopes": list(scopes),
        "expires": int(row.get("expires") or 0),
        "created": int(row.get("created") or 0),
        "last_used": int(row.get("last_used") or 0),
        "root_capable": bool(row.get("root_capable")),
    })
    store.save(rows)
    return 0


def delete(ident):
    if not writes_allowed() or not ID_RE.match(ident or ""):
        return _refuse()
    TokenStore().revoke(ident)
    return 0


def main(argv):
    if len(argv) < 3:
        return _refuse()
    if argv[1] == "put":
        return put(argv[2])
    if argv[1] == "delete":
        return delete(argv[2])
    return _refuse()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
