#!/usr/bin/env python3
"""Find Russian UI strings in JS files missing from i18n.js."""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JS_ROOT = REPO / "www" / "network_config" / "static" / "js"

i18n = (JS_ROOT / "i18n.js").read_text(encoding="utf-8")
keys = set(re.findall(r"'((?:\\'|[^'])+)':", i18n))
missing_all = {}
for name in ("app.js", "flasher.js", "gateway.js", "mqtt.js"):
    text = (JS_ROOT / name).read_text(encoding="utf-8")
    found = set(re.findall(r"['\"]([А-Яа-яЁё][^'\"]{0,200})['\"]", text))
    missing = sorted(s for s in found if s not in keys and len(s) > 1)
    missing_all[name] = missing

out = REPO / "tmp" / "i18n_missing.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(missing_all, ensure_ascii=False, indent=2), encoding="utf-8")
print("written", out, "total", sum(len(v) for v in missing_all.values()))
