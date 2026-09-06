"""Evaluate and run scenarios. No MQTT here — caller supplies state and pub."""
from __future__ import annotations

import math
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from sa02m_rules.store import enqueue_notify, load, save

Pub = Callable[[str, str, Any], None]
MAX_DEPTH = 3
MAX_WRITES = 8
RUN_S = 30


def _as_num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "on", "true", "yes"):
            return 1.0
        if s in ("0", "off", "false", "no"):
            return 0.0
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None
    return None


def cmp_op(left: Any, op: str, right: Any) -> bool:
    if op == "changed":
        return True
    ln, rn = _as_num(left), _as_num(right)
    if ln is not None and rn is not None:
        if op == "==":
            return ln == rn
        if op == "!=":
            return ln != rn
        if op == ">":
            return ln > rn
        if op == "<":
            return ln < rn
        if op == ">=":
            return ln >= rn
        if op == "<=":
            return ln <= rn
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return False


def state_get(state: Dict[str, Dict[str, Any]], device: str, cap: str) -> Any:
    return (state.get(device) or {}).get(cap)


def _hm(now: float) -> Tuple[int, int, int]:
    lt = time.localtime(now)
    return lt.tm_hour, lt.tm_min, (lt.tm_wday)  # Mon=0


def _in_window(now: float, fr: str, to: str) -> bool:
    h, m, _ = _hm(now)
    cur = h * 60 + m
    a = int(fr[:2]) * 60 + int(fr[3:5])
    b = int(to[:2]) * 60 + int(to[3:5])
    if a <= b:
        return a <= cur <= b
    return cur >= a or cur <= b


def sun_times(now: float, lat: float, lon: float) -> Tuple[float, float]:
    """Approximate local sunrise/sunset unix ts. NOAA-ish, no deps."""
    lt = time.localtime(now)
    n = lt.tm_yday
    decl = 23.44 * math.sin(math.radians(360.0 / 365.0 * (n - 81)))
    lat_r = math.radians(lat)
    decl_r = math.radians(decl)
    cos_w = -math.tan(lat_r) * math.tan(decl_r)
    cos_w = max(-1.0, min(1.0, cos_w))
    w = math.degrees(math.acos(cos_w))
    noon = 12.0 - lon / 15.0
    rise_h = noon - w / 15.0
    set_h = noon + w / 15.0
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight + rise_h * 3600.0, midnight + set_h * 3600.0


def cond_ok(cond: Dict[str, Any], state: Dict[str, Dict[str, Any]], now: float) -> bool:
    if not cond:
        return True
    items = cond.get("any") if isinstance(cond.get("any"), list) else cond.get("all")
    any_mode = isinstance(cond.get("any"), list)
    if not isinstance(items, list) or not items:
        return True
    hits = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "time_window":
            hits.append(_in_window(now, str(item.get("from")), str(item.get("to"))))
        elif item.get("kind") == "state":
            hits.append(cmp_op(state_get(state, item.get("device"), item.get("cap")),
                               item.get("op") or "==", item.get("value")))
    if not hits:
        return True
    return any(hits) if any_mode else all(hits)


def trigger_hit(tr: Dict[str, Any], event: Dict[str, Any], state: Dict[str, Dict[str, Any]],
                now: float, lat: float, lon: float, last_tick: Dict[str, Any]) -> bool:
    kind = tr.get("kind")
    if kind == "boot":
        return event.get("kind") == "boot"
    if kind == "state":
        if event.get("kind") != "state":
            return False
        if event.get("device") != tr.get("device") or event.get("cap") != tr.get("cap"):
            return False
        if tr.get("op") == "changed":
            return True
        return cmp_op(event.get("value"), tr.get("op") or "==", tr.get("value"))
    if kind == "time":
        h, m, wday = _hm(now)
        at = str(tr.get("at") or "")
        if ("%02d:%02d" % (h, m)) != at:
            return False
        days = tr.get("days")
        if isinstance(days, list) and days and wday not in days:
            return False
        key = "time:%s:%s" % (tr.get("at"), now // 60)
        if last_tick.get(key):
            return False
        last_tick[key] = True
        return True
    if kind == "sun":
        rise, sett = sun_times(now, lat, lon)
        target = sett if tr.get("event") == "sunset" else rise
        target += float(tr.get("offset") or 0) * 60.0
        if abs(now - target) > 30:
            return False
        key = "sun:%s:%s" % (tr.get("event"), int(target // 60))
        if last_tick.get(key):
            return False
        last_tick[key] = True
        return True
    return False


def compile_logic(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    t = s.get("template")
    if t == "motion_off":
        return [
            {"kind": "delay", "seconds": int(s.get("off_s") or 180)},
            {"kind": "set", "device": s.get("light") or s.get("device") or "",
             "cap": "on_off", "value": 0},
        ]
    if t == "thermostat":
        return [{"kind": "set", "device": s.get("heater") or "", "cap": "on_off",
                 "value": 1}]
    if t == "adaptive_light":
        return [{"kind": "set", "device": s.get("light") or "", "cap": "brightness",
                 "value": 40}]
    return list(s.get("action") or [])


def _http(act: Dict[str, Any]) -> str:
    req = urllib.request.Request(act["url"], method=act.get("method") or "GET")
    if act.get("method") == "POST" and act.get("body") is not None:
        data = str(act["body"]).encode("utf-8")
        req.data = data
        req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return "http %s" % resp.status
    except urllib.error.HTTPError as exc:
        return "http %s" % exc.code
    except Exception as exc:
        return "http err %s" % exc


def run_actions(s: Dict[str, Any], doc: Dict[str, Any], state: Dict[str, Dict[str, Any]],
                pub: Pub, now: float, path: str, depth: int = 0,
                writes: Optional[List[int]] = None) -> Dict[str, Any]:
    writes = writes if writes is not None else [0]
    started = time.time()
    err = ""
    actions = compile_logic(s) if s.get("type") == "logic" else list(s.get("action") or [])
    for act in actions:
        if time.time() - started > RUN_S:
            err = "timeout"
            break
        kind = act.get("kind")
        if kind == "delay":
            time.sleep(min(300, int(act.get("seconds") or 1)))
        elif kind == "set":
            if writes[0] >= MAX_WRITES:
                err = "write cap"
                break
            pub(act.get("device"), act.get("cap"), act.get("value"))
            writes[0] += 1
        elif kind == "toggle":
            if writes[0] >= MAX_WRITES:
                err = "write cap"
                break
            cur = state_get(state, act.get("device"), act.get("cap"))
            n = _as_num(cur)
            pub(act.get("device"), act.get("cap"), 0 if n and n >= 0.5 else 1)
            writes[0] += 1
        elif kind == "notify":
            enqueue_notify(doc, str(act.get("text") or ""), path)
        elif kind == "http":
            err = _http(act) if str(act.get("url") or "").startswith("http") else "bad url"
            if err.startswith("http err"):
                break
            err = ""
        elif kind == "scenario":
            if depth >= MAX_DEPTH:
                err = "depth"
                break
            child = next((c for c in doc.get("scenarios") or []
                          if isinstance(c, dict) and c.get("id") == act.get("id")), None)
            if not child:
                err = "child missing"
                break
            if child.get("id") == s.get("id"):
                err = "loop"
                break
            rec = run_actions(child, doc, state, pub, now, path, depth + 1, writes)
            if rec.get("error"):
                err = rec["error"]
                break
    rec = {"ts": now, "id": s.get("id"), "name": s.get("name"), "error": err, "ok": not err}
    s["last_run"] = now
    s["last_error"] = err
    runs = doc.setdefault("runs", [])
    runs.append(rec)
    del runs[:-50]
    save(doc, path)
    return rec


def maybe_run(doc: Dict[str, Any], event: Dict[str, Any], state: Dict[str, Dict[str, Any]],
              pub: Pub, now: float, lat: float, lon: float, last_tick: Dict[str, Any],
              path: str) -> List[Dict[str, Any]]:
    out = []
    for s in doc.get("scenarios") or []:
        if not isinstance(s, dict) or s.get("enabled") is False:
            continue
        trigs = s.get("trigger") or []
        hit = False
        for tr in trigs:
            if isinstance(tr, dict) and trigger_hit(tr, event, state, now, lat, lon, last_tick):
                hit = True
                break
        if not trigs and event.get("kind") == "run_now" and event.get("id") == s.get("id"):
            hit = True
        if event.get("kind") == "run_now" and event.get("id") == s.get("id"):
            hit = True
        if not hit:
            continue
        if not cond_ok(s.get("condition") or {}, state, now) and event.get("kind") != "run_now":
            continue
        if s.get("type") == "code":
            from sa02m_rules.code_runner import run_code
            from sa02m_rules.store import save
            rec = run_code(s, doc.get("library") or "", state, pub, now, lat, lon,
                           doc, path, {})
            runs = doc.setdefault("runs", [])
            runs.append(rec)
            del runs[:-50]
            save(doc, path)
            out.append(rec)
            continue
        out.append(run_actions(s, doc, state, pub, now, path))
    return out
