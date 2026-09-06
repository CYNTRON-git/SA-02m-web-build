"""Atomic JSON store for scenarios. Stdlib only."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

NAME_RE = re.compile(r"^[\w \-./+]{1,64}$", re.UNICODE)
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
DEFAULT_PATH = os.environ.get("SA02M_RULES_PATH", "/etc/sa02m-rules/scenarios.json")
RUNS_MAX = 50
NOTIFY_MAX = 20
TYPES = ("block", "code", "logic", "scene")
TRIGGER_KINDS = ("state", "time", "sun", "boot")
ACTION_KINDS = ("set", "toggle", "delay", "scenario", "notify", "http")
LOGIC_TEMPLATES = ("thermostat", "motion_off", "adaptive_light")


def empty_doc() -> Dict[str, Any]:
    return {"scenarios": [], "library": "", "runs": [], "notify_queue": []}


def _atomic_write(path: str, data: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rules-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path: str = DEFAULT_PATH) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty_doc()
    if not isinstance(data, dict):
        return empty_doc()
    data.setdefault("scenarios", [])
    data.setdefault("library", "")
    data.setdefault("runs", [])
    data.setdefault("notify_queue", [])
    return data


def save(doc: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    _atomic_write(path, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))


def listed(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for s in doc.get("scenarios") or []:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str):
            continue
        out.append({
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "enabled": s.get("enabled") is not False,
            "type": s.get("type") or "block",
            "order": int(s.get("order") or 0),
            "last_run": s.get("last_run"),
            "last_error": s.get("last_error") or "",
            "summary": s.get("summary") or "",
        })
    out.sort(key=lambda r: (r["order"], r["name"]))
    return out


def _new_id(existing: List[str]) -> str:
    n = 1
    while ("s%d" % n) in existing:
        n += 1
    return "s%d" % n


def _clean_trigger(raw: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in TRIGGER_KINDS:
            continue
        row = {"kind": kind}
        if kind == "state":
            if not isinstance(item.get("device"), str) or not ID_RE.match(item["device"]):
                continue
            row["device"] = item["device"]
            row["cap"] = str(item.get("cap") or "on_off")[:32]
            row["op"] = item.get("op") if item.get("op") in ("==", "!=", ">", "<", ">=", "<=", "changed") else "=="
            row["value"] = item.get("value")
        elif kind == "time":
            at = str(item.get("at") or "")
            if not re.match(r"^\d{2}:\d{2}$", at):
                continue
            row["at"] = at
            days = item.get("days")
            if isinstance(days, list):
                row["days"] = [int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6][:7]
        elif kind == "sun":
            row["event"] = "sunset" if item.get("event") == "sunset" else "sunrise"
            try:
                row["offset"] = max(-180, min(180, int(item.get("offset") or 0)))
            except (TypeError, ValueError):
                row["offset"] = 0
        out.append(row)
    return out


def _clean_condition(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    key = "any" if isinstance(raw.get("any"), list) else "all"
    items = raw.get(key) if isinstance(raw.get(key), list) else []
    cleaned = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "time_window":
            fr, to = str(item.get("from") or ""), str(item.get("to") or "")
            if re.match(r"^\d{2}:\d{2}$", fr) and re.match(r"^\d{2}:\d{2}$", to):
                cleaned.append({"kind": "time_window", "from": fr, "to": to})
        elif kind == "state":
            if isinstance(item.get("device"), str) and ID_RE.match(item["device"]):
                cleaned.append({
                    "kind": "state",
                    "device": item["device"],
                    "cap": str(item.get("cap") or "on_off")[:32],
                    "op": item.get("op") if item.get("op") in ("==", "!=", ">", "<", ">=", "<=") else "==",
                    "value": item.get("value"),
                })
    return {key: cleaned} if cleaned else {}


def _clean_action(raw: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in ACTION_KINDS:
            continue
        row = {"kind": kind}
        if kind in ("set", "toggle"):
            if not isinstance(item.get("device"), str) or not ID_RE.match(item["device"]):
                continue
            row["device"] = item["device"]
            row["cap"] = str(item.get("cap") or "on_off")[:32]
            if kind == "set":
                row["value"] = item.get("value")
        elif kind == "delay":
            try:
                row["seconds"] = max(1, min(300, int(item.get("seconds") or 1)))
            except (TypeError, ValueError):
                continue
        elif kind == "scenario":
            if not isinstance(item.get("id"), str) or not ID_RE.match(item["id"]):
                continue
            row["id"] = item["id"]
        elif kind == "notify":
            text = str(item.get("text") or "").strip()[:240]
            if not text:
                continue
            row["text"] = text
        elif kind == "http":
            url = str(item.get("url") or "").strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            row["url"] = url[:500]
            row["method"] = "POST" if str(item.get("method") or "").upper() == "POST" else "GET"
            if item.get("body") is not None:
                row["body"] = str(item.get("body"))[:2000]
        out.append(row)
    return out


def _summary(s: Dict[str, Any]) -> str:
    if s.get("type") == "code":
        return "code"
    if s.get("type") == "scene":
        return "scene"
    if s.get("type") == "logic":
        return str(s.get("template") or "logic")
    trig = s.get("trigger") or []
    act = s.get("action") or []
    t0 = trig[0]["kind"] if trig else "?"
    a0 = act[0]["kind"] if act else "?"
    return "if %s then %s" % (t0, a0)


def validate_row(body: Dict[str, Any], existing_id: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    name = body.get("name")
    if not isinstance(name, str):
        return None, "invalid name"
    name = " ".join(name.split())
    if not name or not NAME_RE.match(name):
        return None, "invalid name"
    sid = body.get("id") if isinstance(body.get("id"), str) else existing_id
    if sid and not ID_RE.match(sid):
        return None, "invalid id"
    typ = body.get("type") if body.get("type") in TYPES else "block"
    row: Dict[str, Any] = {
        "id": sid or "",
        "name": name,
        "enabled": body.get("enabled") is not False,
        "type": typ,
        "order": 0,
        "trigger": _clean_trigger(body.get("trigger")),
        "condition": _clean_condition(body.get("condition")),
        "action": _clean_action(body.get("action")),
        "code": str(body.get("code") or "")[:8000] if typ == "code" else "",
        "template": body.get("template") if body.get("template") in LOGIC_TEMPLATES else "",
        "summary": "",
        "last_run": body.get("last_run"),
        "last_error": "",
    }
    try:
        row["order"] = int(body.get("order") or 0)
    except (TypeError, ValueError):
        row["order"] = 0
    row["summary"] = _summary(row)
    return row, ""


def _ok(doc: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    """Success payload: always carry list + journal so the hub cache can patch."""
    out: Dict[str, Any] = {
        "ok": True,
        "scenarios": listed(doc),
        "runs": list(doc.get("runs") or [])[-20:],
        "notify_queue": list(doc.get("notify_queue") or []),
        "library": doc.get("library") or "",
    }
    out.update(extra)
    return out


def _wait_run_flag(flag: str, timeout_s: float = 2.5) -> None:
    """Block until sa02m-rules consumes `path.run` (one tick is 1 s)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not os.path.exists(flag):
            time.sleep(0.2)
            return
        time.sleep(0.05)


def apply_command(body: Dict[str, Any], path: str = DEFAULT_PATH) -> Dict[str, Any]:
    """Upsert / delete / run_now / library / replace. Returns board response."""
    if not isinstance(body, dict):
        return {"ok": False, "error": "bad json"}
    doc = load(path)
    if body.get("library") is not None and "name" not in body and not body.get("id"):
        doc["library"] = str(body.get("library") or "")[:16000]
        save(doc, path)
        return _ok(doc)
    if body.get("replace") is True and isinstance(body.get("scenarios"), list):
        cleaned, ids = [], []
        for raw in body["scenarios"][:64]:
            if not isinstance(raw, dict):
                continue
            row, err = validate_row(raw)
            if err:
                return {"ok": False, "error": err}
            if not row["id"]:
                row["id"] = _new_id(ids)
            ids.append(row["id"])
            cleaned.append(row)
        doc["scenarios"] = cleaned
        save(doc, path)
        return _ok(doc)
    sid = body.get("id") if isinstance(body.get("id"), str) else ""
    if body.get("delete") is True:
        if not sid or not ID_RE.match(sid):
            return {"ok": False, "error": "invalid id"}
        before = len(doc["scenarios"])
        doc["scenarios"] = [s for s in doc["scenarios"] if not (isinstance(s, dict) and s.get("id") == sid)]
        if len(doc["scenarios"]) == before:
            return {"ok": False, "error": "not_found"}
        save(doc, path)
        return _ok(doc)
    if body.get("get") is True:
        # Fetch one FULL document (the editor opens a scenario from this —
        # list rows carry only the summary).
        if not sid or not ID_RE.match(sid):
            return {"ok": False, "error": "invalid id"}
        found = next((s for s in doc["scenarios"]
                      if isinstance(s, dict) and s.get("id") == sid), None)
        if not found:
            return {"ok": False, "error": "not_found"}
        return _ok(doc, scenario=found)
    if isinstance(body.get("upsert"), list):
        # Batch upsert: validate ALL entries first so a template compile never
        # lands half-written (docs/contracts/cloud-scenarios.md §Channel).
        cleaned = []
        ids = [s.get("id") for s in doc["scenarios"] if isinstance(s, dict)]
        for raw in body["upsert"][:16]:
            if not isinstance(raw, dict):
                return {"ok": False, "error": "bad json"}
            row, err = validate_row(raw, raw.get("id") if isinstance(raw.get("id"), str) else None)
            if err:
                return {"ok": False, "error": err}
            if not row["id"]:
                row["id"] = _new_id([i for i in ids if isinstance(i, str)])
                ids.append(row["id"])
            cleaned.append(row)
        for row in cleaned:
            idx = next((i for i, s in enumerate(doc["scenarios"])
                        if isinstance(s, dict) and s.get("id") == row["id"]), None)
            if idx is None:
                doc["scenarios"].append(row)
            else:
                prev = doc["scenarios"][idx]
                row["last_run"] = prev.get("last_run")
                row["last_error"] = prev.get("last_error") or ""
                doc["scenarios"][idx] = row
        save(doc, path)
        return _ok(doc)
    if body.get("ack_notify") is True:
        take_notify(doc, path)
        return _ok(doc, notify_queue=[])
    if body.get("run_now") is True:
        if not sid:
            return {"ok": False, "error": "invalid id"}
        found = next((s for s in doc["scenarios"] if isinstance(s, dict) and s.get("id") == sid), None)
        if not found:
            return {"ok": False, "error": "not_found"}
        flag = path + ".run"
        _atomic_write(flag, sid)
        _wait_run_flag(flag)
        return _ok(load(path), run_now=sid)
    row, err = validate_row(body, sid or None)
    if err:
        return {"ok": False, "error": err}
    ids = [s.get("id") for s in doc["scenarios"] if isinstance(s, dict)]
    if not row["id"]:
        row["id"] = _new_id([i for i in ids if isinstance(i, str)])
    idx = next((i for i, s in enumerate(doc["scenarios"]) if isinstance(s, dict) and s.get("id") == row["id"]), None)
    if idx is None:
        doc["scenarios"].append(row)
    else:
        prev = doc["scenarios"][idx]
        row["last_run"] = prev.get("last_run")
        row["last_error"] = prev.get("last_error") or ""
        doc["scenarios"][idx] = row
    save(doc, path)
    listed_now = listed(doc)
    return _ok(doc, scenario=listed_now[next(i for i, s in enumerate(listed_now) if s["id"] == row["id"])])


def append_run(doc: Dict[str, Any], rec: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    runs = doc.setdefault("runs", [])
    runs.append(rec)
    del runs[:-RUNS_MAX]
    save(doc, path)


def enqueue_notify(doc: Dict[str, Any], text: str, path: str = DEFAULT_PATH) -> None:
    q = doc.setdefault("notify_queue", [])
    q.append({"ts": time.time(), "text": text[:240]})
    del q[:-NOTIFY_MAX]
    save(doc, path)


def take_notify(doc: Dict[str, Any], path: str = DEFAULT_PATH) -> List[Dict[str, Any]]:
    q = list(doc.get("notify_queue") or [])
    doc["notify_queue"] = []
    save(doc, path)
    return q
