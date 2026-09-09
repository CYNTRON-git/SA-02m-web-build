"""Evaluate and run scenarios. No MQTT here — caller supplies state and pub.

Engine v2 (docs/contracts/cloud-scenarios.md): non-blocking scheduler (heap
of timers, cancel-by-key), edge trigger operators, button gestures (bridge
press counters + edge classifier fallback), presence on Vars.home_mode,
day/night condition presets, for_s conditions, ramp/transition/mode/scene
actions, and end events (off | restore) as scheduler entries — a run's 30 s
budget never counts scheduler waits.
"""
from __future__ import annotations

import heapq
import math
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from sa02m_rules import http_guard
from sa02m_rules.store import (
    CAP_RE, EDGE_OPS, EVENT_OPS, GESTURES, HOME_MODES, ID_RE, MAX_WRITES,
    Journal, load, migrate_journal, save)

Pub = Callable[[str, str, Any], None]
MAX_DEPTH = 3
# MAX_WRITES (direct writes per run) is the store's constant — validation
# refuses what this engine would abort.
MAX_ACTIONS = 20          # actions per run (continuations included)
RUN_S = 30                # execution budget per run, scheduler waits excluded;
                          # for type=code a hard deadline on the body itself
BUTTON_COUNTER_MAX = 65535    # bridge press counters are uint16 input regs
BUTTON_COUNTER_WRAP_SLACK = 8  # …→small after ≥ MAX-slack reads as one press
WRITE_WINDOW_S = 10.0     # global MQTT write rate window …
WRITE_WINDOW_MAX = 8      # … with at most this many writes inside it
BUTTON_LONG_S = 0.5       # MR-02m firmware parity: long press threshold
BUTTON_DOUBLE_S = 0.4     # … and double-press window
BUTTON_COUNTER_HOLD_S = 10.0  # counters seen this recently mute the classifier
HEAP_MAX = 4096
SNAPSHOT_MAX = 16

_LEVEL_OPS = ("==", "!=", ">", "<", ">=", "<=", "changed")
_COUNTER_SUFFIX = {"short": "single", "long": "long", "double": "double"}


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


def _truthy(v: Any) -> bool:
    n = _as_num(v)
    return n is not None and n >= 0.5


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


def state_get(state: Dict[str, Dict[str, Any]], device: Any, cap: Any) -> Any:
    if not isinstance(device, str) or not isinstance(cap, str):
        return None
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


def _edge_pred(op: str, value: Any, threshold: Any) -> bool:
    """Steady-state predicate behind an edge/event operator."""
    if op in ("motion_detected", "opened"):
        return _truthy(value)
    if op in ("motion_cleared", "closed"):
        return not _truthy(value) and value is not None
    n = _as_num(value)
    if n is None:
        return False
    if op == "rises_above":
        tn = _as_num(threshold)
        return tn is not None and n > tn
    if op == "drops_below":
        tn = _as_num(threshold)
        return tn is not None and n < tn
    if isinstance(threshold, dict):
        lo, hi = _as_num(threshold.get("min")), _as_num(threshold.get("max"))
        if lo is None or hi is None:
            return False
        if op == "enters_range":
            return lo <= n <= hi
        if op == "leaves_range":
            return n < lo or n > hi
    return False


def _http(act: Dict[str, Any]) -> str:
    """`http` action under the shared target policy (http_guard)."""
    try:
        status, _n = http_guard.fetch(act.get("method") or "GET", act["url"],
                                      act.get("body"))
        return "http %s" % status
    except http_guard.HttpRefused as exc:
        return "http err %s" % exc


class _Run:
    """One scenario execution; survives across scheduler continuations."""
    __slots__ = ("sid", "name", "chain", "writes", "actions",
                 "spent", "snapshot", "turned_on", "source", "error", "root")

    def __init__(self, sid: str, name: str, chain: Tuple[str, ...],
                 source: str) -> None:
        self.sid = sid
        self.name = name
        self.chain = chain
        self.writes = 0
        self.actions = 0
        self.spent = 0.0
        self.snapshot: Optional[Dict[Tuple[str, str], Any]] = None
        self.turned_on: List[Tuple[str, str]] = []
        self.source = source
        self.error = ""
        self.root: Optional[Dict[str, Any]] = None


class LogicRuntime:
    """The narrow facade a logic template instance sees (logic_templates.py)."""

    def __init__(self, engine: "Engine", sid: str) -> None:
        self._e = engine
        self.sid = sid

    def now(self) -> float:
        return self._e._now()

    def get(self, device: str, cap: str = "on_off") -> Any:
        return state_get(self._e.state, device, cap)

    def caps_of(self, device: str) -> Tuple[str, ...]:
        return self._e.caps_of(device)

    def set(self, device: str, cap: str, value: Any) -> bool:
        return self._e._write(device, cap, value, None)

    def schedule(self, delay_s: float, key: str) -> None:
        self._e._schedule(delay_s, "logic", "%s:logic:%s" % (self.sid, key),
                          (self.sid, key))

    def cancel(self, key: str) -> None:
        self._e._cancel("%s:logic:%s" % (self.sid, key))

    def sun(self) -> Tuple[float, float]:
        return sun_times(self.now(), self._e.lat, self._e.lon)

    def is_night(self) -> bool:
        rise, sett = self.sun()
        now = self.now()
        return not (rise <= now <= sett)

    def in_window(self, fr: str, to: str) -> bool:
        return _in_window(self.now(), fr, to)

    def home_mode(self) -> str:
        return self._e.home_mode()

    def set_home_mode(self, mode: str) -> None:
        self._e.set_home_mode(mode)

    def notify(self, text: str) -> None:
        self._e._notify(text)

    def publish_status(self, control: str, value: Any) -> None:
        self._e._publish_tpl_state(self.sid, control, value)


class Engine:
    """Scenario runtime. One instance per rules service."""

    def __init__(self, pub: Pub, path: str, now: Callable[[], float] = time.time,
                 lat: float = 55.75, lon: float = 37.62,
                 pub_state: Optional[Pub] = None) -> None:
        self._pub = pub
        # State-topic publisher for the sa02m-rules-<id> virtual device
        # (template state for cloud debugging); defaults to the command pub.
        self._pub_state_fn = pub_state if pub_state is not None else pub
        self.path = path
        self._now = now
        self.lat, self.lon = lat, lon
        self.state: Dict[str, Dict[str, Any]] = {}
        # Run state is buffered here and flushed to runs.json (store.Journal);
        # a pre-1.0.6.41 document still carrying it is migrated once, before
        # the first load.
        migrate_journal(path)
        self.journal = Journal(path, now)
        self.doc = load(path)
        if not isinstance(self.doc.get("vars"), dict):
            self.doc["vars"] = {}
        self._heap: List[Tuple[float, int, str, str, int, Any]] = []
        self._gen: Dict[str, int] = {}
        self._seq = 0
        self._writes_win: Deque[float] = deque()
        self._trig_prev: Dict[Tuple[str, int], Any] = {}
        self._btn: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._btn_counters: Dict[Tuple[str, str], float] = {}
        self._for_s: Dict[Tuple[str, int], float] = {}
        self._for_s_index: Dict[Tuple[str, str], List[Tuple[str, int, Dict[str, Any]]]] = {}
        self._last_tick: Dict[str, Any] = {}
        self._logic: Dict[str, Any] = {}
        self._tpl_state: Dict[str, Dict[str, Any]] = {}
        self._caps_provider: Optional[Callable[[str], Tuple[str, ...]]] = None
        self._fingerprint = ""
        self._adopt(self.doc, force=True)

    # ── external wiring ────────────────────────────────────────────────
    def set_caps_provider(self, fn: Callable[[str], Tuple[str, ...]]) -> None:
        self._caps_provider = fn

    def caps_of(self, device: str) -> Tuple[str, ...]:
        if self._caps_provider is None:
            return ()
        try:
            return self._caps_provider(device)
        except Exception:
            return ()

    # ── scheduler ──────────────────────────────────────────────────────
    def _schedule(self, delay_s: float, kind: str, key: str, payload: Any = None) -> None:
        self._schedule_at(self._now() + max(0.0, delay_s), kind, key, payload)

    def _schedule_at(self, due: float, kind: str, key: str, payload: Any = None) -> None:
        if len(self._heap) > HEAP_MAX:  # drop stale-generation entries
            self._heap = [e for e in self._heap
                          if e[4] == self._gen.get(e[3], 0)]
            heapq.heapify(self._heap)
        self._seq += 1
        heapq.heappush(self._heap, (due, self._seq, kind, key,
                                    self._gen.get(key, 0), payload))

    def _cancel(self, key: str) -> None:
        self._gen[key] = self._gen.get(key, 0) + 1

    def _cancel_prefix(self, prefix: str) -> None:
        for key in list(self._gen):
            if key.startswith(prefix):
                self._gen[key] += 1

    # ── doc adoption (hot reload) ──────────────────────────────────────
    @staticmethod
    def _fingerprint_of(doc: Dict[str, Any]) -> str:
        import json
        # Content only: last_run/last_error are journal state, and a merged
        # view may carry an older copy than this engine's own buffer.
        rows = [{k: v for k, v in s.items() if k not in ("last_run", "last_error")}
                if isinstance(s, dict) else s for s in doc.get("scenarios") or []]
        return json.dumps([rows, doc.get("library") or "", doc.get("vars") or {}],
                          ensure_ascii=False, sort_keys=True, default=str)

    def adopt(self, doc: Dict[str, Any]) -> None:
        """Hot-reload entry: adopt `doc` when the scenario content changed."""
        if self._fingerprint_of(doc) == self._fingerprint:
            return  # only runs/notify moved (our own save) — keep state
        self._adopt(doc)

    def _adopt(self, doc: Dict[str, Any], force: bool = False) -> None:
        old = {s.get("id"): s for s in (self.doc.get("scenarios") or [])
               if isinstance(s, dict)} if not force else {}
        self.doc = doc
        if not isinstance(self.doc.get("vars"), dict):
            self.doc["vars"] = {}
        self.journal.overlay(self.doc)  # the loaded view predates our buffer
        self._fingerprint = self._fingerprint_of(doc)
        new_ids = set()
        for s in doc.get("scenarios") or []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            sid = s["id"]
            new_ids.add(sid)
            prev = old.get(sid)
            if prev is not None and self._same_runtime(prev, s):
                continue  # keep timers/trackers/logic state
            self._cancel_prefix(sid + ":")
            self._trig_prev = {k: v for k, v in self._trig_prev.items()
                               if k[0] != sid}
            self._arm_scenario(s)
            self._publish_tpl_state(sid, "rule_enabled",
                                    1 if s.get("enabled") is not False else 0)
        for sid in set(old) - new_ids:
            self._cancel_prefix(sid + ":")
            self._logic.pop(sid, None)
            self._tpl_state.pop(sid, None)
            self._trig_prev = {k: v for k, v in self._trig_prev.items()
                               if k[0] != sid}
        self._rebuild_for_s_index()

    @staticmethod
    def _same_runtime(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        import json
        keys = ("enabled", "type", "trigger", "condition", "action", "end",
                "template", "params", "code")
        return json.dumps([a.get(k) for k in keys], sort_keys=True, default=str) == \
            json.dumps([b.get(k) for k in keys], sort_keys=True, default=str)

    def _arm_scenario(self, s: Dict[str, Any]) -> None:
        sid = s["id"]
        if s.get("enabled") is False:
            self._logic.pop(sid, None)
            return
        for i, tr in enumerate(s.get("trigger") or []):
            if isinstance(tr, dict) and tr.get("kind") == "every":
                self._schedule(float(tr.get("minutes") or 1) * 60.0, "every",
                               "%s:every:%d" % (sid, i), (sid, i))
        if s.get("type") == "logic":
            self._logic[sid] = self._make_logic(s)

    def _make_logic(self, s: Dict[str, Any]) -> Any:
        from sa02m_rules.logic_templates import make_logic
        inst = make_logic(str(s.get("template") or ""),
                          LogicRuntime(self, s["id"]),
                          s.get("params") if isinstance(s.get("params"), dict) else {})
        if inst is None:
            self.journal.set_last(s, s.get("last_run"), "unknown template")
        else:
            inst.on_boot()
        return inst

    # ── template state mirror (cloud debugging) ────────────────────────
    def _publish_tpl_state(self, sid: str, control: str, value: Any) -> None:
        last = self._tpl_state.setdefault(sid, {})
        if last.get(control) == value:
            return
        last[control] = value
        self._pub_state_fn("sa02m-rules-%s" % sid, control, value)

    # ── home mode / presence ───────────────────────────────────────────
    def home_mode(self) -> str:
        mode = str(self.doc.get("vars", {}).get("home_mode") or "home")
        return mode if mode in HOME_MODES else "home"

    def set_home_mode(self, mode: str) -> None:
        if mode not in HOME_MODES:
            return
        old = self.home_mode()
        if mode == old:
            return
        self.doc["vars"]["home_mode"] = mode
        save(self.doc, self.path)
        # leave: home/night → away/holiday; arrive: away/holiday → home.
        # night is «home, asleep» — entering it is not an arrival event.
        event = ""
        if mode in ("away", "holiday") and old not in ("away", "holiday"):
            event = "leave"
        elif mode == "home" and old in ("away", "holiday"):
            event = "arrive"
        if event:
            self._dispatch({"kind": "presence", "event": event})

    # ── writes ─────────────────────────────────────────────────────────
    def _write(self, device: Any, cap: Any, value: Any,
               run: Optional[_Run], from_ramp: bool = False,
               force: bool = False) -> bool:
        """Publish a control write. `force` skips the rate window (still
        counted in it) — the `end` safety auto-off must land whatever
        unrelated traffic filled the window (verdict A7)."""
        if not isinstance(device, str) or not isinstance(cap, str):
            return False
        if not ID_RE.match(device) or not CAP_RE.match(cap):
            return False  # every caller's names reach an MQTT topic (A2)
        now = self._now()
        win = self._writes_win
        while win and now - win[0] > WRITE_WINDOW_S:
            win.popleft()
        if len(win) >= WRITE_WINDOW_MAX and not force:
            if run is not None:
                run.error = "write cap"
            return False
        if run is not None:
            key = (device, cap)
            if run.snapshot is not None and key not in run.snapshot \
                    and len(run.snapshot) < SNAPSHOT_MAX:
                run.snapshot[key] = state_get(self.state, device, cap)
            if cap == "on_off" and _truthy(value) and key not in run.turned_on:
                run.turned_on.append(key)
        if not from_ramp:
            self._cancel("ramp:%s:%s" % (device, cap))
        self._pub(device, cap, value)
        win.append(now)
        self.state.setdefault(device, {})[cap] = value
        return True

    # ── events in ──────────────────────────────────────────────────────
    def alias_state(self, device: str, cap: str, value: Any) -> None:
        """Raw-topic mirror (no triggers) for unmapped device/control names."""
        self.state.setdefault(device, {})[cap] = value

    def on_state(self, device: str, cap: str, value: Any) -> None:
        prev = state_get(self.state, device, cap)
        self.state.setdefault(device, {})[cap] = value
        if prev == value:
            return
        self._for_s_touch(device, cap, value)
        self._button_state(device, cap, value, prev)
        for inst in list(self._logic.values()):
            inst.on_state(device, cap, value, prev)
        self._dispatch({"kind": "state", "device": device, "cap": cap,
                        "value": value, "prev": prev})

    def on_boot(self) -> None:
        self._dispatch({"kind": "boot"})

    def run_now(self, sid: str) -> Optional[Dict[str, Any]]:
        s = self._scenario(sid)
        if s is None or s.get("enabled") is False:
            return None
        rec = self._run(s, "run_now", source="external")
        # The cloud channel reads the store right after its .run flag is
        # consumed (store._wait_run_flag): the record must be on disk now.
        self.flush_runs()
        return rec

    # ── journal flush ──────────────────────────────────────────────────
    def flush_runs(self) -> bool:
        return self.journal.flush(self.doc)

    def _flush_if_due(self) -> None:
        if self.journal.due():
            self.flush_runs()

    def _notify(self, text: str) -> None:
        self.journal.notify(self.doc, str(text)[:240])
        self._flush_if_due()

    # ── trigger dispatch ───────────────────────────────────────────────
    def _scenario(self, sid: Any) -> Optional[Dict[str, Any]]:
        for s in self.doc.get("scenarios") or []:
            if isinstance(s, dict) and s.get("id") == sid:
                return s
        return None

    def _dispatch(self, event: Dict[str, Any]) -> None:
        for s in list(self.doc.get("scenarios") or []):
            if not isinstance(s, dict) or s.get("enabled") is False:
                continue
            if s.get("type") == "logic":
                continue  # logic instances react via on_state/on_button/timers
            if self._triggered(s, event) and self._conditions_ok(s):
                self._run(s, str(event.get("kind") or "?"), source="scenario")

    def _triggered(self, s: Dict[str, Any], event: Dict[str, Any]) -> bool:
        trigs = s.get("trigger") or []
        for i, tr in enumerate(trigs):
            if not isinstance(tr, dict):
                continue
            kind = tr.get("kind")
            if kind == "boot" and event.get("kind") == "boot":
                return True
            if kind == "state" and event.get("kind") == "state":
                if event.get("device") != tr.get("device") or event.get("cap") != tr.get("cap"):
                    continue
                op = tr.get("op") or "=="
                if op in _LEVEL_OPS:
                    if cmp_op(event.get("value"), op, tr.get("value")):
                        return True
                else:  # edge / sensor-event: fire once on the crossing
                    key = (str(s.get("id")), i)
                    prev = self._trig_prev.get(key)
                    self._trig_prev[key] = event.get("value")
                    if prev is None:
                        continue  # first sight establishes the baseline
                    if _edge_pred(op, event.get("value"), tr.get("value")) \
                            and not _edge_pred(op, prev, tr.get("value")):
                        return True
            elif kind == "button" and event.get("kind") == "button":
                if event.get("device") != tr.get("device"):
                    continue
                if tr.get("input") and event.get("input") != tr.get("input"):
                    continue
                if event.get("gesture") == tr.get("gesture"):
                    return True
            elif kind == "presence" and event.get("kind") == "presence":
                if event.get("event") == tr.get("event"):
                    return True
        return False

    # ── button gestures ────────────────────────────────────────────────
    def _button_state(self, device: str, cap: str, value: Any, prev: Any) -> None:
        import re
        m = re.match(r"^(di_\d{1,2})_(short|long|double)$", cap)
        if m:
            # Counter path: fire only on an observed INCREMENT (never on the
            # first sight after boot — «never fire on service start»). A
            # decrease is a counter reset (module reboot / firmware upgrade,
            # verdict A8) and only re-baselines — except the uint16 wrap
            # 65535→small, which is one real press.
            if prev is None:
                return
            pn, vn = _as_num(prev), _as_num(value)
            if pn is None or vn is None:
                return
            wrapped = (pn >= BUTTON_COUNTER_MAX - BUTTON_COUNTER_WRAP_SLACK
                       and vn <= BUTTON_COUNTER_WRAP_SLACK)
            if vn > pn or wrapped:
                self._btn_counters[(device, m.group(1))] = self._now()
                self._dispatch({"kind": "button", "device": device,
                                "input": m.group(1),
                                "gesture": _COUNTER_SUFFIX[m.group(2)]})
            return
        if not re.match(r"^di_\d{1,2}$", cap):
            return
        # Fallback edge classifier on di_N fronts (firmware thresholds).
        key = (device, cap)
        now = self._now()
        st = self._btn.get(key)
        if st is None:
            if len(self._btn) >= 64:  # bounded: evict the idle-est
                oldest = min(self._btn, key=lambda k: self._btn[k].get("ts", 0))
                self._btn.pop(oldest, None)
            st = {"press_ts": None, "long": False, "pending_single": False,
                  "ts": now}
            self._btn[key] = st
        st["ts"] = now
        was, is_now = _truthy(prev) if prev is not None else None, _truthy(value)
        if prev is None:
            return  # baseline only — no edges at service start
        if is_now and not was:
            st["press_ts"] = now
            st["long"] = False
            self._schedule(BUTTON_LONG_S, "btn_long",
                           "btnl:%s:%s" % key, (device, cap))
        elif was and not is_now:
            press_ts = st.get("press_ts")
            st["press_ts"] = None
            self._cancel("btnl:%s:%s" % key)
            if press_ts is None:
                return
            if st.get("long"):
                st["long"] = False
                self._btn_emit(device, cap, "long_release")
            elif now - press_ts < BUTTON_LONG_S:
                if st.get("pending_single"):
                    st["pending_single"] = False
                    self._cancel("btns:%s:%s" % key)
                    self._btn_emit(device, cap, "double")
                else:
                    st["pending_single"] = True
                    self._schedule(BUTTON_DOUBLE_S, "btn_single",
                                   "btns:%s:%s" % key, (device, cap))

    def _btn_emit(self, device: str, input_name: str, gesture: str) -> None:
        if gesture in GESTURES and gesture != "long_release":
            seen = self._btn_counters.get((device, input_name))
            if seen is not None and self._now() - seen < BUTTON_COUNTER_HOLD_S:
                return  # bridge counters are authoritative for these gestures
        self._dispatch({"kind": "button", "device": device,
                        "input": input_name, "gesture": gesture})
        for inst in list(self._logic.values()):
            inst.on_button(device, input_name, gesture)

    # ── for_s condition tracking ───────────────────────────────────────
    def _rebuild_for_s_index(self) -> None:
        self._for_s_index = {}
        self._for_s = {}
        for s in self.doc.get("scenarios") or []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            cond = s.get("condition") or {}
            items = cond.get("any") if isinstance(cond.get("any"), list) \
                else cond.get("all")
            if not isinstance(items, list):
                continue
            for ci, item in enumerate(items):
                if not isinstance(item, dict) or item.get("kind") != "state":
                    continue
                if not item.get("for_s"):
                    continue
                key = (str(item.get("device")), str(item.get("cap") or "on_off"))
                self._for_s_index.setdefault(key, []).append((s["id"], ci, item))
                # Seed from the current mirror so a value that already holds
                # starts its hold clock now, not on the next MQTT update.
                if self._cond_state_level(item, state_get(self.state, *key)):
                    self._for_s[(s["id"], ci)] = self._now()

    def _for_s_touch(self, device: str, cap: str, value: Any) -> None:
        for sid, ci, item in self._for_s_index.get((device, cap)) or []:
            key = (sid, ci)
            if self._cond_state_level(item, value):
                if key not in self._for_s:
                    self._for_s[key] = self._now()
            else:
                self._for_s.pop(key, None)

    @staticmethod
    def _cond_state_level(item: Dict[str, Any], value: Any) -> bool:
        op = item.get("op") or "=="
        if op in EDGE_OPS or op in EVENT_OPS:
            return _edge_pred(op, value, item.get("value"))
        return cmp_op(value, op, item.get("value"))

    # ── conditions ─────────────────────────────────────────────────────
    def _conditions_ok(self, s: Dict[str, Any]) -> bool:
        cond = s.get("condition") or {}
        if not isinstance(cond, dict):
            return True
        items = cond.get("any") if isinstance(cond.get("any"), list) else cond.get("all")
        if not isinstance(items, list) or not items:
            return True
        any_mode = isinstance(cond.get("any"), list)
        now = self._now()
        hits = []
        for ci, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            hits.append(self._cond_item_ok(s, ci, item, now))
        if not hits:
            return True
        return any(hits) if any_mode else all(hits)

    def _cond_item_ok(self, s: Dict[str, Any], ci: int, item: Dict[str, Any],
                      now: float) -> bool:
        kind = item.get("kind")
        if kind == "time_window":
            preset = item.get("preset")
            if preset == "any":
                return True
            if preset in ("day", "night"):
                rise, sett = sun_times(now, self.lat, self.lon)
                day = rise <= now <= sett
                return day if preset == "day" else not day
            return _in_window(now, str(item.get("from")), str(item.get("to")))
        if kind == "weekday":
            wday = _hm(now)[2]
            days = item.get("days")
            if isinstance(days, list) and days:
                return wday in days
            preset = item.get("preset")
            if preset == "workday":
                return wday < 5
            if preset == "weekend":
                return wday >= 5
            return True
        if kind == "mode":
            return self.home_mode() == str(item.get("value") or "home")
        if kind == "state":
            holds = self._cond_state_level(
                item, state_get(self.state, item.get("device"), item.get("cap")))
            for_s = item.get("for_s")
            if not for_s:
                return holds
            if not holds:
                self._for_s.pop((str(s.get("id")), ci), None)
                return False
            since = self._for_s.get((str(s.get("id")), ci))
            return since is not None and now - since >= float(for_s)
        return True  # unknown kinds are ignored (v1 behavior)

    # ── tick: due timers + clock triggers ──────────────────────────────
    def tick(self) -> None:
        now = self._now()
        while self._heap and self._heap[0][0] <= now:
            due, _seq, kind, key, gen, payload = heapq.heappop(self._heap)
            if gen != self._gen.get(key, 0):
                continue  # cancelled
            self._fire_timer(due, kind, key, payload)
        self._scan_clock_triggers(now)
        self._flush_if_due()

    def _fire_timer(self, due: float, kind: str, key: str, payload: Any) -> None:
        if kind == "every":
            sid, idx = payload
            s = self._scenario(sid)
            tr = None
            if s is not None:
                trigs = s.get("trigger") or []
                if idx < len(trigs):
                    tr = trigs[idx]
            if s is None or s.get("enabled") is False or not isinstance(tr, dict) \
                    or tr.get("kind") != "every":
                return  # not re-armed: scenario gone or trigger changed
            self._schedule(float(tr.get("minutes") or 1) * 60.0, "every", key,
                           payload)  # re-arm first: a run error must not kill it
            if self._conditions_ok(s):
                self._run(s, "every", source="scenario")
        elif kind == "cont":
            run, frames = payload
            self._exec(run, frames)
        elif kind == "ramp":
            self._ramp_step(key, payload, due)
        elif kind == "end":
            self._end_fire(payload)
        elif kind == "btn_long":
            device, cap = payload
            st = self._btn.get((device, cap))
            if st and st.get("press_ts") is not None:
                st["long"] = True
                self._btn_emit(device, cap, "long")
        elif kind == "btn_single":
            device, cap = payload
            st = self._btn.get((device, cap))
            if st and st.get("pending_single"):
                st["pending_single"] = False
                self._btn_emit(device, cap, "single")
        elif kind == "logic":
            sid, lkey = payload
            inst = self._logic.get(sid)
            if inst is not None:
                inst.on_timer(lkey)

    def _scan_clock_triggers(self, now: float) -> None:
        for s in list(self.doc.get("scenarios") or []):
            if not isinstance(s, dict) or s.get("enabled") is False:
                continue
            if s.get("type") == "logic":
                continue
            for tr in s.get("trigger") or []:
                if not isinstance(tr, dict):
                    continue
                kind = tr.get("kind")
                if kind == "time":
                    h, m, wday = _hm(now)
                    if ("%02d:%02d" % (h, m)) != str(tr.get("at") or ""):
                        continue
                    days = tr.get("days")
                    if isinstance(days, list) and days and wday not in days:
                        continue
                    tkey = "time:%s:%s:%d" % (s.get("id"), tr.get("at"), now // 60)
                    if self._last_tick.get(tkey):
                        continue
                    self._last_tick[tkey] = True
                    if self._conditions_ok(s):
                        self._run(s, "time", source="scenario")
                    break
                if kind == "sun":
                    rise, sett = sun_times(now, self.lat, self.lon)
                    target = sett if tr.get("event") == "sunset" else rise
                    target += float(tr.get("offset") or 0) * 60.0
                    if abs(now - target) > 30:
                        continue
                    tkey = "sun:%s:%s:%d" % (s.get("id"), tr.get("event"),
                                             int(target // 60))
                    if self._last_tick.get(tkey):
                        continue
                    self._last_tick[tkey] = True
                    if self._conditions_ok(s):
                        self._run(s, "sun", source="scenario")
                    break
        if len(self._last_tick) > 256:  # bounded: keys are per-minute anyway
            self._last_tick = dict(list(self._last_tick.items())[-128:])

    # ── runs ───────────────────────────────────────────────────────────
    def _run(self, s: Dict[str, Any], reason: str, source: str) -> Dict[str, Any]:
        sid = str(s.get("id"))
        run = _Run(sid, str(s.get("name") or sid), (sid,), source)
        run.root = s
        # A re-trigger supersedes: pending continuation and end timer reset.
        self._cancel("%s:cont" % sid)
        self._cancel("%s:end" % sid)
        end = s.get("end")
        if isinstance(end, dict):
            self._publish_tpl_state(sid, "end_after_s", 0)
            if end.get("mode") == "restore":
                run.snapshot = {}
        typ = s.get("type") or "block"
        if typ == "code":
            from sa02m_rules.code_runner import run_code
            started = self._now()
            rec = run_code(s, self.doc.get("library") or "", self.state,
                           self._pub, self._now(), self.lat, self.lon,
                           self.doc, self.path, self.doc["vars"],
                           on_home_mode=self.set_home_mode, budget_s=RUN_S,
                           notify_sink=self._notify)
            run.spent += self._now() - started
            rec["source"] = source
            self._finish(run, s, rec.get("error") or "")
            return rec
        if typ == "logic":
            rec = {"ts": self._now(), "id": sid, "name": run.name,
                   "error": "logic runs by itself", "ok": False,
                   "source": source}
            self._journal(s, rec)
            return rec
        self._exec(run, [[s, list(s.get("action") or []), 0]])
        return {"ts": self._now(), "id": sid, "name": run.name,
                "error": run.error, "ok": not run.error, "source": source}

    def _exec(self, run: _Run, frames: List[list]) -> None:
        """Run the frame stack; a `delay` suspends the WHOLE stack (child
        scenarios included) and the scheduler resumes it — no wall time
        spent waiting, so RUN_S never counts scheduler waits."""
        seg = self._now()
        while frames:
            frame = frames[-1]
            s, actions, idx = frame[0], frame[1], frame[2]
            dive = False
            while idx < len(actions):
                if run.spent + (self._now() - seg) > RUN_S:
                    run.error = "timeout"
                    frames.clear()
                    break
                if run.actions >= MAX_ACTIONS:
                    run.error = "action cap"
                    frames.clear()
                    break
                act = actions[idx]
                idx += 1
                frame[2] = idx
                if not isinstance(act, dict):
                    continue
                run.actions += 1
                if act.get("kind") == "delay":
                    run.spent += self._now() - seg
                    self._schedule(float(act.get("seconds") or 1), "cont",
                                   "%s:cont" % run.sid, (run, frames))
                    return  # resume from the scheduler
                step = self._do_action(run, s, act, frames)
                if step is None:  # error — abort the whole run
                    frames.clear()
                    break
                if step == "pushed":
                    dive = True
                    break
            if not dive and frames and frames[-1] is frame:
                frames.pop()
        run.spent += self._now() - seg
        self._finish(run, run.root or s, run.error)

    def _do_action(self, run: _Run, s: Dict[str, Any], act: Dict[str, Any],
                   frames: List[list]) -> Optional[str]:
        """True = keep going, "pushed" = child frame on the stack, None = abort."""
        kind = act.get("kind")
        if kind == "set":
            if run.writes >= MAX_WRITES:
                run.error = "write cap"
                return None
            transition = float(act.get("transition_s") or 0)
            value = act.get("value")
            num = _as_num(value)
            if transition > 0 and num is not None:
                self._start_ramp(run, str(act.get("device")),
                                 str(act.get("cap") or "on_off"),
                                 num, transition)
            else:
                self._write(act.get("device"), str(act.get("cap") or "on_off"),
                            value, run)
            run.writes += 1
        elif kind == "toggle":
            if run.writes >= MAX_WRITES:
                run.error = "write cap"
                return None
            cur = state_get(self.state, act.get("device"),
                            str(act.get("cap") or "on_off"))
            self._write(act.get("device"), str(act.get("cap") or "on_off"),
                        0 if _truthy(cur) else 1, run)
            run.writes += 1
        elif kind == "ramp":
            if run.writes >= MAX_WRITES:
                run.error = "write cap"
                return None
            self._start_ramp(run, str(act.get("device")),
                             str(act.get("cap") or "on_off"),
                             float(act.get("to")), float(act.get("seconds") or 1))
            run.writes += 1
        elif kind == "mode":
            self.set_home_mode(str(act.get("value") or ""))
        elif kind == "notify":
            self._notify(str(act.get("text") or ""))
        elif kind == "http":
            err = _http(act) if str(act.get("url") or "").startswith("http") else "bad url"
            if err.startswith("http err"):
                run.error = err
                return None
        elif kind in ("scenario", "scene"):
            if len(frames) - 1 >= MAX_DEPTH:
                run.error = "depth"
                return None
            child = self._scenario(act.get("id"))
            if child is None:
                run.error = "child missing"
                return None
            if child.get("id") in run.chain:
                run.error = "loop"
                return None
            if kind == "scene" and (child.get("type") or "block") != "scene":
                run.error = "not a scene"
                return None
            if (child.get("type") or "block") in ("logic", "code"):
                run.error = "bad child"
                return None
            if child.get("enabled") is False:
                return "ok"  # a disabled child is skipped, not an error
            run.chain = run.chain + (str(child.get("id")),)
            frames.append([child, list(child.get("action") or []), 0])
            return "pushed"
        return "ok"

    def _finish(self, run: _Run, s: Dict[str, Any], err: str) -> None:
        run.error = err
        rec = {"ts": self._now(), "id": run.sid, "name": run.name,
               "error": err, "ok": not err, "source": run.source}
        self._journal(s, rec)
        end = s.get("end")
        if isinstance(end, dict):
            payload = {"sid": run.sid, "mode": end.get("mode"),
                       "turned_on": list(run.turned_on),
                       "snapshot": dict(run.snapshot) if run.snapshot is not None else None}
            self._schedule(float(end.get("after_s") or 60), "end",
                           "%s:end" % run.sid, payload)
            # Published once when armed, 0 when fired/reset — it does not
            # count down, hence the name (verdict A19).
            self._publish_tpl_state(run.sid, "end_after_s",
                                    int(end.get("after_s") or 60))

    def _journal(self, s: Dict[str, Any], rec: Dict[str, Any]) -> None:
        self.journal.set_last(s, rec["ts"], rec.get("error") or "")
        self.journal.append_run(self.doc, rec)
        self._flush_if_due()

    # ── ramp ───────────────────────────────────────────────────────────
    def _start_ramp(self, run: Optional[_Run], device: str, cap: str,
                    to: float, seconds: float) -> None:
        seconds = max(1.0, min(300.0, seconds))
        cur = _as_num(state_get(self.state, device, cap))
        start = cur if cur is not None else 0.0
        steps = max(1, min(30, int(seconds / 2)))
        if run is not None and run.snapshot is not None:
            key = (device, cap)
            if key not in run.snapshot and len(run.snapshot) < SNAPSHOT_MAX:
                run.snapshot[key] = state_get(self.state, device, cap)
        key = "ramp:%s:%s" % (device, cap)
        self._cancel(key)
        self._schedule(seconds / steps, "ramp", key,
                       {"device": device, "cap": cap, "from": start,
                        "to": to, "i": 0, "n": steps, "dt": seconds / steps})

    def _ramp_step(self, key: str, payload: Dict[str, Any], due: float) -> None:
        payload["i"] += 1
        i, n = payload["i"], payload["n"]
        frac = i / float(n)
        value = payload["from"] + (payload["to"] - payload["from"]) * frac
        if i >= n:
            value = payload["to"]
        if abs(value - round(value)) < 1e-9:
            value = int(round(value))
        else:
            value = round(value, 2)
        self._write(payload["device"], payload["cap"], value, None,
                    from_ramp=True)
        if i < n:
            # Due-relative, not fire-relative: a late tick must not stretch
            # the ramp (a 1 Hz service tick can lag under load).
            self._schedule_at(due + payload["dt"], "ramp", key, payload)

    # ── end event ──────────────────────────────────────────────────────
    def _end_fire(self, payload: Dict[str, Any]) -> None:
        sid = str(payload.get("sid"))
        self._publish_tpl_state(sid, "end_after_s", 0)
        refused = False
        if payload.get("mode") == "off":
            for device, cap in payload.get("turned_on") or []:
                refused |= not self._write(device, cap, 0, None, force=True)
        elif payload.get("mode") == "restore":
            snap = payload.get("snapshot") or {}
            for (device, cap), value in snap.items():
                if value is not None:
                    refused |= not self._write(device, cap, value, None, force=True)
        if refused:
            # Never silent: the load stays on and the cloud must see why.
            s = self._scenario(sid)
            rec = {"ts": self._now(), "id": sid,
                   "name": str((s or {}).get("name") or sid),
                   "error": "end write refused", "ok": False, "source": "end"}
            if s is not None:
                self._journal(s, rec)
            else:
                self.journal.append_run(self.doc, rec)
                self._flush_if_due()

    # ── logic template status relay ────────────────────────────────────
    def logic_status_poll(self) -> None:
        """Republish changed logic status controls (blocked_by_switch …)."""
        for sid, inst in list(self._logic.items()):
            try:
                status = inst.status()
            except Exception:
                status = {}
            for control, value in (status or {}).items():
                self._publish_tpl_state(sid, str(control)[:32], value)
