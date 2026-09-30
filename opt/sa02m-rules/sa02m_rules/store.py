"""Atomic JSON store for scenarios. Stdlib only.

Two files (contract §Store): the DOCUMENT (`scenarios.json` — scenarios,
library, vars; written only when that content changes) and the JOURNAL
(`runs.json` beside it — run records, the notify queue, per-scenario
last_run/last_error; written by the engine's `Journal` buffer on a bounded
cadence). Readers (`load`) merge the journal into the document view, so the
cloud channel's `listed()` / `_ok()` shapes never changed when the split
landed (1.0.6.41, audit A16: a 1 Hz motion rule was one whole-store
fsync per second).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sa02m_rules import device_index, http_guard

LOG = logging.getLogger("sa02m-rules.store")

NAME_RE = re.compile(r"^[\w \-./+]{1,64}$", re.UNICODE)
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
#: `cap` becomes an MQTT topic segment (`/devices/<id>/controls/<cap>/on`):
#: no `/`, `+`, `#`, no whitespace (verdict A2 — a wildcard here crashed the
#: publish out of on_boot into a restart loop).
CAP_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
TEMPLATE_RE = re.compile(r"^[a-z0-9_]{2,32}$")
BUTTON_INPUT_RE = re.compile(r"^di_\d{1,2}$")
DEFAULT_PATH = os.environ.get("SA02M_RULES_PATH", "/etc/sa02m-rules/scenarios.json")
RUNS_MAX = 50
NOTIFY_MAX = 20
#: Journal flush cadence (engine buffer → runs.json): whichever comes
#: first — this many seconds after the first unflushed record, or this many
#: pending records. A crash loses at most RUNS_FLUSH_S seconds of records.
RUNS_FLUSH_S = 5
RUNS_FLUSH_MAX = 32
JOURNAL_NAME = "runs.json"
_JOURNAL_KEYS = ("runs", "notify_queue")
_LAST_KEYS = ("last_run", "last_error")
PARAMS_MAX_BYTES = 4096
#: Stored scenarios — every write path (replace, batch upsert, single upsert)
#: refuses growth past this with `too_many` (contract §Store).
SCENARIOS_MAX = 64
#: Direct writes (`set`/`toggle`/`ramp`, and Hub.set in code) per scenario
#: row AND per engine run — one constant, so the store refuses at validation
#: (`too_many_writes`) exactly what the engine would abort at run time.
MAX_WRITES = 8
_WRITE_KINDS = ("set", "toggle", "ramp")
#: Elements kept per row; the rest are dropped as `over_limit`.
TRIGGERS_MAX = 8
CONDITIONS_MAX = 8
ACTIONS_MAX = 20
#: Outbound HTTP requests per engine run — the `http` action and the sandbox
#: `Http` share ONE counter per run, so a scenario cannot loop over targets
#: and turn the board into a scanner or an amplifier. Run-time only, unlike
#: MAX_WRITES: the budget spans the whole chain (nested `scenario`/`scene`
#: children spend the parent's), so no single stored row can be validated
#: against it. The 9th request raises HttpRefused("http cap") — journaled,
#: never sent.
HTTP_PER_RUN_MAX = 8
#: Engine version reported to the cloud (control view `rules_engine`):
#: 2 = end events, edge operators, day/night presets, button/every/presence.
RULES_ENGINE = 2
TYPES = ("block", "code", "logic", "scene")
TRIGGER_KINDS = ("state", "time", "sun", "boot", "every", "button", "presence")
ACTION_KINDS = ("set", "toggle", "ramp", "delay", "scenario", "scene",
                "mode", "notify", "http")
LEVEL_OPS = ("==", "!=", ">", "<", ">=", "<=")
#: `changed` fires on a transition, like the edge ops (contract «## Engine
#: v2»), but compares no threshold — so it is not in EDGE_OPS, whose members
#: the engine evaluates through a threshold predicate (engine._edge_pred).
CHANGE_OPS = ("changed",)
EDGE_OPS = ("rises_above", "drops_below", "enters_range", "leaves_range")
EVENT_OPS = ("motion_detected", "motion_cleared", "opened", "closed")
STATE_OPS = LEVEL_OPS + CHANGE_OPS + EDGE_OPS + EVENT_OPS
GESTURES = ("single", "double", "long", "long_release")
HOME_MODES = ("home", "away", "night", "holiday")
#: Logic templates this engine executes (synced with the cloud catalog's
#: BOARD_LOGIC_TEMPLATES — docs/contracts/scenario-templates.md). Unknown
#: names are STORED (the catalog may be newer) and rejected at run time
#: with last_error="unknown template".
LOGIC_TEMPLATES = (
    "switch_light", "motion_light", "circadian", "thermostat",
    "humidity_fan", "co2_ventilation", "humidifier", "away_home",
)


def empty_doc() -> Dict[str, Any]:
    return {"scenarios": [], "library": "", "runs": [], "notify_queue": [],
            "vars": {}}


def charge_http(counter: List[int]) -> None:
    """Charge one outbound request against a run's HTTP budget.

    The one home of the per-run cap for both issuers (`Engine._do_action`'s
    `http` action and the sandbox `Http`): they pass the SAME single-element
    counter of the run, so the budget cannot be doubled by mixing paths.
    Raises before the request is built, so nothing is sent past the cap."""
    if counter[0] >= HTTP_PER_RUN_MAX:
        raise http_guard.HttpRefused("http cap")
    counter[0] += 1


def _atomic_write(path: str, data: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rules-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp is 0600 and os.replace carries the mode over — keep the
        # installer's 0644 (scripts/06b-rules.sh) instead of discarding it
        # on the first save.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_raw(path: str) -> Optional[Dict[str, Any]]:
    """The document as stored, or None when unreadable / not an object."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── journal ─────────────────────────────────────────────────────────────
def journal_path(path: str = DEFAULT_PATH) -> str:
    """`runs.json` beside the document: the unit's ReadWritePaths covers
    exactly that directory (ProtectSystem=strict), so the journal needs no
    second writable tree."""
    return os.path.join(os.path.dirname(path) or ".", JOURNAL_NAME)


def empty_journal() -> Dict[str, Any]:
    return {"runs": [], "notify_queue": [], "last": {}}


def _read_journal(jpath: str) -> Optional[Dict[str, Any]]:
    """None when the file is absent; a corrupt/unreadable/odd-shaped journal
    reads as EMPTY (it never blocks scenarios — the next flush rewrites it)."""
    try:
        with open(jpath, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return empty_journal()
    if not isinstance(data, dict):
        return empty_journal()
    out = empty_journal()
    for key in _JOURNAL_KEYS:
        out[key] = [r for r in (data.get(key) if isinstance(data.get(key), list) else [])
                    if isinstance(r, dict)]
    if isinstance(data.get("last"), dict):
        out["last"] = {sid: v for sid, v in data["last"].items()
                       if isinstance(sid, str) and isinstance(v, dict)}
    return out


def save_journal(journal: Dict[str, Any], jpath: str) -> None:
    _atomic_write(jpath, json.dumps(journal, ensure_ascii=False, separators=(",", ":")))


def _has_legacy_state(raw: Dict[str, Any]) -> bool:
    """A pre-1.0.6.41 document carried runs/notify_queue/last_* inside."""
    if any(key in raw for key in _JOURNAL_KEYS):
        return True
    return any(isinstance(s, dict) and any(k in s for k in _LAST_KEYS)
               for s in raw.get("scenarios") or [])


def migrate_journal(path: str = DEFAULT_PATH) -> bool:
    """Move a pre-1.0.6.41 document's run state into the journal, once.

    Journal first, then the stripped document: a crash between the two
    leaves legacy fields in the document that `load` ignores as soon as the
    journal exists (the journal is truth), and the next boot strips them.
    The engine calls this at boot; a reader never migrates.
    """
    raw = _read_raw(path)
    if raw is None or not _has_legacy_state(raw):
        return False
    jpath = journal_path(path)
    if _read_journal(jpath) is None:
        journal = empty_journal()
        for key in _JOURNAL_KEYS:
            journal[key] = [r for r in (raw.get(key) if isinstance(raw.get(key), list) else [])
                            if isinstance(r, dict)]
        del journal["runs"][:-RUNS_MAX]
        del journal["notify_queue"][:-NOTIFY_MAX]
        for s in raw.get("scenarios") or []:
            if isinstance(s, dict) and isinstance(s.get("id"), str) \
                    and any(k in s for k in _LAST_KEYS):
                journal["last"][s["id"]] = {"last_run": s.get("last_run"),
                                            "last_error": s.get("last_error") or ""}
        save_journal(journal, jpath)
    save(raw, path)
    return True


class Journal:
    """The engine's write side: an in-memory delta of run records, notify
    entries and per-scenario last_run/last_error, flushed to `runs.json`
    read-modify-write — so a `take_notify` drain by the cloud channel's
    process between two flushes is never resurrected — when `due()` (the
    RUNS_FLUSH_S / RUNS_FLUSH_MAX cadence), on `run_now`, and at shutdown.
    A failed flush keeps the (bounded) delta for the next attempt: the
    scenario engine never dies on a journal write.
    """

    def __init__(self, path: str, now: Callable[[], float] = time.time) -> None:
        self.path = journal_path(path)
        self._now = now
        self.pending_runs: List[Dict[str, Any]] = []
        self.pending_notify: List[Dict[str, Any]] = []
        self.pending_last: Dict[str, Dict[str, Any]] = {}
        self._dirty_since: Optional[float] = None

    @property
    def dirty(self) -> bool:
        return self._dirty_since is not None

    def _touch(self) -> None:
        if self._dirty_since is None:
            self._dirty_since = self._now()

    def append_run(self, doc: Dict[str, Any], rec: Dict[str, Any]) -> None:
        runs = doc.setdefault("runs", [])
        runs.append(rec)
        del runs[:-RUNS_MAX]
        self.pending_runs.append(rec)
        del self.pending_runs[:-RUNS_MAX]
        self._touch()

    def set_last(self, s: Dict[str, Any], ts: Any, err: str) -> None:
        s["last_run"] = ts
        s["last_error"] = err
        if isinstance(s.get("id"), str):
            self.pending_last[s["id"]] = {"last_run": ts, "last_error": err}
        self._touch()

    def notify(self, doc: Dict[str, Any], text: str) -> None:
        entry = {"ts": self._now(), "text": str(text)[:240]}
        q = doc.setdefault("notify_queue", [])
        q.append(entry)
        del q[:-NOTIFY_MAX]
        self.pending_notify.append(entry)
        del self.pending_notify[:-NOTIFY_MAX]
        self._touch()

    def due(self) -> bool:
        if self._dirty_since is None:
            return False
        if len(self.pending_runs) + len(self.pending_notify) >= RUNS_FLUSH_MAX:
            return True
        return self._now() - self._dirty_since >= RUNS_FLUSH_S

    def overlay(self, doc: Dict[str, Any]) -> None:
        """Re-apply the unflushed delta onto a freshly loaded document view
        (hot reload adopted a doc whose journal merge predates the delta)."""
        if self.pending_runs:
            runs = doc.setdefault("runs", [])
            runs.extend(self.pending_runs)
            del runs[:-RUNS_MAX]
        for s in doc.get("scenarios") or []:
            if isinstance(s, dict) and s.get("id") in self.pending_last:
                s.update(self.pending_last[s["id"]])

    def flush(self, doc: Optional[Dict[str, Any]] = None) -> bool:
        """Write the delta; True when nothing is left pending."""
        if self._dirty_since is None:
            return True
        journal = _read_journal(self.path) or empty_journal()
        journal["runs"] = (journal["runs"] + self.pending_runs)[-RUNS_MAX:]
        journal["notify_queue"] = (journal["notify_queue"] + self.pending_notify)[-NOTIFY_MAX:]
        journal["last"].update(self.pending_last)
        if doc is not None:  # bounded: no last_* for a deleted scenario
            live = {s.get("id") for s in doc.get("scenarios") or [] if isinstance(s, dict)}
            journal["last"] = {sid: v for sid, v in journal["last"].items() if sid in live}
        try:
            save_journal(journal, self.path)
        except OSError:
            return False
        self.pending_runs = []
        self.pending_notify = []
        self.pending_last = {}
        self._dirty_since = None
        return True


# ── document ────────────────────────────────────────────────────────────
def load(path: str = DEFAULT_PATH) -> Dict[str, Any]:
    """The merged view: document + journal (journal present ⇒ it is the
    truth for runs/notify/last_*; absent ⇒ the document's own fields, which
    a pre-1.0.6.41 file still carries until the engine migrates it)."""
    data = _read_raw(path)
    if data is None:
        return empty_doc()
    data.setdefault("scenarios", [])
    data.setdefault("library", "")
    if not isinstance(data.get("vars"), dict):
        data["vars"] = {}
    journal = _read_journal(journal_path(path))
    if journal is not None:
        data["runs"] = journal["runs"]
        data["notify_queue"] = journal["notify_queue"]
        for s in data["scenarios"]:
            if isinstance(s, dict) and s.get("id") in journal["last"]:
                last = journal["last"][s["id"]]
                s["last_run"] = last.get("last_run")
                s["last_error"] = last.get("last_error") or ""
    else:
        for key in _JOURNAL_KEYS:
            if not isinstance(data.get(key), list):
                data[key] = []
    return data


def _document_of(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The document half: everything but the journal-homed state."""
    out = {k: v for k, v in doc.items() if k not in _JOURNAL_KEYS}
    out["scenarios"] = [
        {k: v for k, v in s.items() if k not in _LAST_KEYS} if isinstance(s, dict) else s
        for s in doc.get("scenarios") or []]
    return out


def save(doc: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    """Write the DOCUMENT only — run state never rides a content write."""
    _atomic_write(path, json.dumps(_document_of(doc), ensure_ascii=False,
                                   separators=(",", ":")))


def listed(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for s in doc.get("scenarios") or []:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str):
            continue
        row: Dict[str, Any] = {
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "enabled": s.get("enabled") is not False,
            "type": s.get("type") or "block",
            "order": int(s.get("order") or 0),
            "last_run": s.get("last_run"),
            "last_error": s.get("last_error") or "",
            "summary": s.get("summary") or "",
        }
        # Provenance rides the list row so the editor can re-open the wizard
        # instance and offer catalog updates without a `get` per scenario.
        if s.get("template_id"):
            row["template_id"] = s["template_id"]
            row["template_version"] = int(s.get("template_version") or 1)
        if s.get("source"):
            row["source"] = s["source"]
        if isinstance(s.get("params"), dict) and s["params"]:
            row["params"] = s["params"]
        out.append(row)
    out.sort(key=lambda r: (r["order"], r["name"]))
    return out


def _explicit_ids(rows: List[Any]) -> List[str]:
    """The client-supplied ids of a batch, in order — the mint's seed."""
    return [r["id"] for r in rows
            if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]]


def _new_id(existing: List[str]) -> str:
    n = 1
    while ("s%d" % n) in existing:
        n += 1
    return "s%d" % n


def _clean_state_op(item: Dict[str, Any], row: Dict[str, Any],
                    allow_changed: bool = True) -> bool:
    """Shared op/value validation for state triggers and conditions."""
    op = item.get("op") or "=="
    allowed = STATE_OPS if allow_changed else tuple(
        o for o in STATE_OPS if o != "changed")
    if op not in allowed:
        op = "=="
    if op in EDGE_OPS and op in ("enters_range", "leaves_range"):
        rng = item.get("value")
        if not isinstance(rng, dict):
            return False
        try:
            lo, hi = float(rng.get("min")), float(rng.get("max"))
        except (TypeError, ValueError, OverflowError):
            return False
        if not (math.isfinite(lo) and math.isfinite(hi)):
            return False
        row["value"] = {"min": lo, "max": hi}
    elif op in EVENT_OPS:
        row.pop("value", None)
    else:
        if not _clean_value(item.get("value")):
            return False  # NaN / ±Infinity would make the store non-strict JSON
        row["value"] = item.get("value")
    row["op"] = op
    return True


def _no_drop(_index: int, _reason: str) -> None:
    pass


#: A `set` value: a string of at most this many characters, or a list /
#: object whose JSON is at most this many bytes (a colour, a mode word).
SET_VALUE_MAX = 256


def _clean_value(v: Any) -> bool:
    """True for a `set` value the store keeps: null, a bool, a finite
    number, a bounded string or a bounded list/object."""
    if v is None or isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return _finite(v)
    if isinstance(v, str):
        return len(v) <= SET_VALUE_MAX
    if isinstance(v, (list, dict)):
        try:
            blob = json.dumps(v, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return False
        return len(blob.encode("utf-8")) <= SET_VALUE_MAX
    return False


def _condition_container_reason(cond: Any) -> str:
    """"" for a condition object the store can read, else the drop reason:
    `not_list` — a non-null key other than `all` / `any`, or an `all` /
    `any` that is not a list; `all_and_any` — both lists non-empty (the
    engine evaluates ONE list, so which one the author meant is a guess).
    `null` values count as absent — `{"all": null}` is «no conditions»."""
    if not isinstance(cond, dict):
        return ""
    carried = {k: v for k, v in cond.items() if v is not None}
    if any(k not in ("all", "any") or not isinstance(v, list) for k, v in carried.items()):
        return "not_list"
    if carried.get("all") and carried.get("any"):
        return "all_and_any"
    return ""


def _condition_key(cond: Dict[str, Any]) -> str:
    """The one list a condition object carries: `any` when it is a
    non-empty list and `all` is not, else `all` (an empty sibling is «no
    conditions» on its side, never a reason to drop the other list)."""
    if isinstance(cond.get("any"), list) and cond["any"] \
            and not (isinstance(cond.get("all"), list) and cond["all"]):
        return "any"
    return "all"


def _clean_target(item: Dict[str, Any], row: Dict[str, Any]) -> str:
    """device + cap (+ optional instance) of a state trigger / condition /
    write action → "" or the drop reason. `instance` names one capability
    of a type the device carries more than once (the Alice document's
    `parameters.instance`), charset-checked like `cap`."""
    if not isinstance(item.get("device"), str) or not ID_RE.match(item["device"]):
        return "bad_device"
    cap = _clean_cap(item.get("cap"))
    if cap is None:
        return "bad_cap"
    inst = item.get("instance")
    if inst is not None and inst != "":
        if not isinstance(inst, str) or not CAP_RE.match(inst):
            return "bad_instance"
    row["device"] = item["device"]
    row["cap"] = cap
    if inst:
        row["instance"] = inst
    return ""


def _finite(v: Any) -> bool:
    """A real, finite number — never a bool, NaN, ±inf or an int past the
    float range (Python's JSON parser accepts `NaN` / `Infinity` and turns
    a 400-digit literal into an int that math.isfinite() cannot take)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def _to_int(v: Any, default: int) -> int:
    """int(v) for a store field, `default` for anything int() refuses or
    that is not finite — a save never dies on a number (error:"internal")."""
    if isinstance(v, bool) or (isinstance(v, float) and not math.isfinite(v)):
        return default  # a bool is not a number (contract §numbers)
    try:
        n = int(v)
        float(n)  # past float range ⇒ OverflowError ⇒ «not a number»
    except (TypeError, ValueError, OverflowError):
        return default
    return n


def _to_float(v: Any, default: float) -> float:
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return f if math.isfinite(f) else default


def _clean_days(days: Any) -> List[int]:
    return [int(d) for d in days if _finite(d) and 0 <= int(d) <= 6][:7]


def _trigger_item(item: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    if not isinstance(item, dict):
        return None, "not_object"
    kind = item.get("kind")
    if kind not in TRIGGER_KINDS:
        return None, "unknown_kind"
    row: Dict[str, Any] = {"kind": kind}
    if kind == "state":
        reason = _clean_target(item, row)
        if reason:
            return None, reason
        if not _clean_state_op(item, row):
            return None, "bad_value"
    elif kind == "time":
        at = str(item.get("at") or "")
        if not re.match(r"^\d{2}:\d{2}$", at):
            return None, "bad_value"
        row["at"] = at
        if isinstance(item.get("days"), list):
            row["days"] = _clean_days(item["days"])
    elif kind == "sun":
        row["event"] = "sunset" if item.get("event") == "sunset" else "sunrise"
        row["offset"] = max(-180, min(180, _to_int(item.get("offset") or 0, 0)))
        # Own coordinates (the cloud's location of the scenario); absent ⇒
        # the board's (service._geo). Present but not a number in range is
        # a drop, never a silent fallback to the board's coordinates.
        for key, lim in (("lat", 90.0), ("lon", 180.0)):
            v = item.get(key)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not -lim <= v <= lim:
                return None, "bad_value"
            row[key] = float(v)
        if isinstance(item.get("days"), list):
            row["days"] = _clean_days(item["days"])
    elif kind == "every":
        minutes = _to_int(item.get("minutes") or 0, 0)
        if not 1 <= minutes <= 60:
            return None, "bad_value"
        row["minutes"] = minutes
    elif kind == "button":
        if not isinstance(item.get("device"), str) or not ID_RE.match(item["device"]):
            return None, "bad_device"
        gesture = item.get("gesture")
        if gesture not in GESTURES:
            return None, "bad_value"
        row["device"] = item["device"]
        row["gesture"] = gesture
        inp = item.get("input")
        if isinstance(inp, str) and BUTTON_INPUT_RE.match(inp):
            row["input"] = inp
    elif kind == "presence":
        if item.get("event") not in ("arrive", "leave"):
            return None, "bad_value"
        row["event"] = item["event"]
    return row, ""


def _clean_trigger(raw: Any, drop: Callable[[int, str], None] = _no_drop) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out = []
    for i, item in enumerate(raw):
        if i >= TRIGGERS_MAX:
            drop(i, "over_limit")  # once: the rest is not walked (bounded answer)
            break
        row, reason = _trigger_item(item)
        if row is None:
            drop(i, reason)
            continue
        out.append(row)
    return out


def _condition_item(item: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    if not isinstance(item, dict):
        return None, "not_object"
    kind = item.get("kind")
    if kind == "time_window":
        preset = item.get("preset")
        if preset in ("any", "day", "night"):
            return {"kind": "time_window", "preset": preset}, ""
        fr, to = str(item.get("from") or ""), str(item.get("to") or "")
        if re.match(r"^\d{2}:\d{2}$", fr) and re.match(r"^\d{2}:\d{2}$", to):
            return {"kind": "time_window", "from": fr, "to": to}, ""
        return None, "bad_value"
    if kind == "weekday":
        row: Dict[str, Any] = {"kind": "weekday"}
        days = item.get("days")
        if isinstance(days, list) and days:
            row["days"] = _clean_days(days)
            if not row["days"]:
                return None, "bad_value"
        elif item.get("preset") in ("workday", "weekend"):
            row["preset"] = item["preset"]
        else:
            return None, "bad_value"
        return row, ""
    if kind == "mode":
        if item.get("value") in HOME_MODES:
            return {"kind": "mode", "value": item["value"]}, ""
        return None, "bad_value"
    if kind == "state":
        row = {"kind": "state"}
        reason = _clean_target(item, row)
        if reason:
            return None, reason
        # Conditions are level-only (contract: no `changed`); for_s asks
        # the level to hold steadily for N seconds. Edge/event ops keep
        # their steady-state level meaning («is above», «is inside the
        # range», «is open») — the engine evaluates them as levels.
        if not _clean_state_op(item, row, allow_changed=False):
            return None, "bad_value"
        op = row.get("op")
        if op == "rises_above":
            row["op"] = ">"
        elif op == "drops_below":
            row["op"] = "<"
        elif op in ("motion_detected", "opened"):
            row["op"] = "=="
            row["value"] = 1
        elif op in ("motion_cleared", "closed"):
            row["op"] = "=="
            row["value"] = 0
        # enters_range / leaves_range stay as-is: level «inside/outside».
        for_s = _to_int(item.get("for_s") or 0, 0)
        if for_s > 0:
            row["for_s"] = min(for_s, 86400)
        return row, ""
    return None, "unknown_kind"


def _clean_condition(raw: Any, drop: Callable[[int, str], None] = _no_drop) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    key = _condition_key(raw)
    items = raw.get(key) if isinstance(raw.get(key), list) else []
    cleaned = []
    for i, item in enumerate(items):
        if i >= CONDITIONS_MAX:
            drop(i, "over_limit")  # once: the rest is not walked (bounded answer)
            break
        row, reason = _condition_item(item)
        if row is None:
            drop(i, reason)
            continue
        cleaned.append(row)
    return {key: cleaned} if cleaned else {}


def _action_item(item: Any, scene_only: bool,
                 targets: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    if not isinstance(item, dict):
        return None, "not_object"
    kind = item.get("kind")
    if kind not in ACTION_KINDS:
        return None, "unknown_kind"
    if scene_only and kind != "set":
        return None, "not_in_scene"  # a scene IS a list of set actions (contract §Scenes)
    row: Dict[str, Any] = {"kind": kind}
    if kind in ("set", "toggle"):
        reason = _clean_target(item, row)
        if reason:
            return None, reason
        if kind == "set":
            if not _clean_value(item.get("value")):
                return None, "bad_value"
            row["value"] = item.get("value")
            transition = _to_float(item.get("transition_s") or 0, 0.0)
            if transition > 0:
                row["transition_s"] = min(transition, 300.0)
    elif kind == "ramp":
        if not isinstance(item.get("device"), str) or not ID_RE.match(item["device"]):
            return None, "bad_device"
        if not _finite(item.get("to")):
            return None, "bad_value"
        seconds = _to_float(item.get("seconds") or 0, 0.0)
        if not 1.0 <= seconds <= 300.0:
            return None, "bad_value"
        reason = _clean_target(item, row)
        if reason:
            return None, reason
        row["to"] = item.get("to")
        row["seconds"] = seconds
    elif kind == "delay":
        raw_s = item.get("seconds") or 1
        if isinstance(raw_s, bool) or (isinstance(raw_s, float) and not math.isfinite(raw_s)):
            return None, "bad_value"
        try:
            row["seconds"] = max(1, min(300, int(raw_s)))
        except (TypeError, ValueError, OverflowError):
            return None, "bad_value"
    elif kind in ("scenario", "scene"):
        if not isinstance(item.get("id"), str) or not ID_RE.match(item["id"]):
            return None, "bad_id"
        row["id"] = item["id"]
    elif kind == "mode":
        if item.get("value") not in HOME_MODES:
            return None, "bad_value"
        row["value"] = item["value"]
    elif kind == "notify":
        text = str(item.get("text") or "").strip()[:240]
        if not text:
            return None, "bad_value"
        row["text"] = text
    elif kind == "http":
        url = str(item.get("url") or "").strip()[:http_guard.URL_MAX]
        # Static half of the target policy (no DNS here); the engine
        # resolves and re-checks at request time (http_guard).
        if http_guard.check_url(url, resolve=False):
            return None, "bad_url"
        row["url"] = url
        row["method"] = "POST" if str(item.get("method") or "").upper() == "POST" else "GET"
        if item.get("body") is not None:
            row["body"] = str(item.get("body"))[:2000]
    if targets is not None and kind in _WRITE_KINDS:
        _topic, reason = targets.resolve(row["device"], row["cap"], row.get("instance", ""))
        if reason:
            return None, reason
    return row, ""


def _clean_action(raw: Any, scene_only: bool = False,
                  drop: Callable[[int, str], None] = _no_drop,
                  targets: Any = None) -> List[Dict[str, Any]]:
    """`targets` (a device_index.DeviceIndex, or None to skip) drops a
    set/toggle/ramp the Alice device document cannot resolve."""
    if not isinstance(raw, list):
        return []
    out = []
    for i, item in enumerate(raw):
        if i >= ACTIONS_MAX:
            drop(i, "over_limit")  # once: the rest is not walked (bounded answer)
            break
        row, reason = _action_item(item, scene_only, targets)
        if row is None:
            drop(i, reason)
            continue
        out.append(row)
    return out


def _clean_cap(raw: Any) -> Optional[str]:
    """Capability short name; absent ⇒ `on_off`, bad charset ⇒ None (drop)."""
    cap = str(raw or "on_off")
    return cap if CAP_RE.match(cap) else None


def _clean_end(raw: Any) -> Optional[Dict[str, Any]]:
    """`end` event: {after_s: 60..14400, mode: off|restore} (contract §End)."""
    if not isinstance(raw, dict):
        return None
    if raw.get("mode") not in ("off", "restore"):
        return None
    after_s = _to_int(raw.get("after_s") or 0, 0)
    if not 60 <= after_s <= 14400:
        return None
    return {"after_s": after_s, "mode": raw["mode"]}


def _strict_json(v: Any) -> Any:
    """`v` with every NaN / ±Infinity float and every integer past float
    range replaced by None — the store and the cloud answer stay strict
    JSON, and a template never meets a number float() refuses (it reads
    None as «use the default»)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and not _finite(v):
        return None
    if isinstance(v, dict):
        return {k: _strict_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_strict_json(x) for x in v]
    return v


def _clean_params(raw: Any) -> Optional[Dict[str, Any]]:
    """Template params: object, ≤ 4 KB JSON (contract §Document), non-finite
    numbers normalised to null."""
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        params = _strict_json(raw)
        # The bound is judged on what is stored (NaN ⇒ null grows a value,
        # a 400-digit int ⇒ null shrinks it).
        blob = json.dumps(params, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if len(blob) > PARAMS_MAX_BYTES:
        return None
    return params


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


def validate_row(body: Dict[str, Any], existing_id: Optional[str] = None,
                 dropped: Optional[List[Dict[str, Any]]] = None,
                 targets: Any = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Clean one scenario body. Every trigger / condition / action element
    it drops is appended to `dropped` as {part, index, reason}; whether a
    drop refuses the save is `apply_command`'s call (`_checked_row`)."""
    sink: List[Dict[str, Any]] = dropped if dropped is not None else []

    def drop_in(part: str) -> Callable[[int, str], None]:
        return lambda i, reason: sink.append({"part": part, "index": i, "reason": reason})

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
    for part in ("trigger", "action"):
        raw = body.get(part)
        if raw and not isinstance(raw, list):
            sink.append({"part": part, "index": None, "reason": "not_list"})
    if body.get("condition") and not isinstance(body.get("condition"), dict):
        sink.append({"part": "condition", "index": None, "reason": "not_object"})
    elif _condition_container_reason(body.get("condition")):
        sink.append({"part": "condition", "index": None,
                     "reason": _condition_container_reason(body.get("condition"))})
    row: Dict[str, Any] = {
        "id": sid or "",
        "name": name,
        "enabled": body.get("enabled") is not False,
        "type": typ,
        "order": 0,
        "trigger": _clean_trigger(body.get("trigger"), drop_in("trigger")),
        "condition": _clean_condition(body.get("condition"), drop_in("condition")),
        "action": _clean_action(body.get("action"), scene_only=(typ == "scene"),
                                drop=drop_in("action"), targets=targets),
        "code": str(body.get("code") or "")[:8000] if typ == "code" else "",
        "summary": "",
        # The board's own record (journal): a client value is never stored
        # or echoed — a NaN there made the answer non-strict JSON.
        "last_run": None,
        "last_error": "",
    }
    if sum(1 for a in row["action"] if a.get("kind") in _WRITE_KINDS) > MAX_WRITES:
        return None, "too_many_writes"  # the engine would abort at MAX_WRITES anyway
    # `template` is stored even when this engine predates the name — the
    # catalog may be newer than the firmware; the ENGINE rejects unknown
    # names at run time with last_error="unknown template" (§Versioning).
    template = body.get("template")
    if isinstance(template, str) and TEMPLATE_RE.match(template):
        row["template"] = template
    else:
        row["template"] = ""
    template_id = body.get("template_id")
    if isinstance(template_id, str) and TEMPLATE_RE.match(template_id):
        row["template_id"] = template_id
        row["template_version"] = max(1, _to_int(body.get("template_version") or 1, 1))
    if body.get("source") in ("wizard", "manual"):
        row["source"] = body["source"]
    params = _clean_params(body.get("params"))
    if params is not None:
        row["params"] = params
    end = _clean_end(body.get("end"))
    if end is not None:
        row["end"] = end
    if body.get("alice_expose") is True and typ == "scene":
        row["alice_expose"] = True
    captured = body.get("captured_from")
    if isinstance(captured, dict) and typ == "scene":
        keep = {k: str(v)[:64] for k, v in captured.items()
                if k in ("room_id", "group_id") and isinstance(v, str)}
        if keep:
            row["captured_from"] = keep
    row["order"] = _to_int(body.get("order") or 0, 0)
    row["summary"] = _summary(row)
    return row, ""


def _gutted(body: Dict[str, Any], row: Dict[str, Any]) -> bool:
    """The body carried trigger / action / condition content and cleaning
    kept none of it — storing that row would silently gut the scenario (an
    emptied condition list makes it fire MORE often than asked; Operator
    2026-09-30)."""
    for part in ("trigger", "action"):
        raw = body.get(part)
        if raw and not row[part]:
            return True
    cond = body.get("condition")
    if _condition_container_reason(cond):
        return True
    if cond and not row["condition"]:
        if not isinstance(cond, dict):
            return True
        return bool(cond.get(_condition_key(cond)))
    return False


#: Body parts of a scenario: a single-path save carrying none of them for an
#: EXISTING id is a partial update (name / enabled / order / …) that keeps
#: the stored body (the cloud's enable/disable button sends {id,name,enabled,
#: type} — it used to store an empty scenario).
_BODY_PARTS = ("trigger", "condition", "action", "code", "template", "params")

_UNREADABLE_WARNED: Set[str] = set()


def _target_index() -> Any:
    """The Alice device document's index, or None — the target check is
    skipped (logged once per document path) while the document cannot be
    read, so an absent or broken document never refuses every save."""
    path = device_index.devices_path()
    ix = device_index.load_index(path)
    if ix.loaded:
        _UNREADABLE_WARNED.discard(path)
        return ix
    if path not in _UNREADABLE_WARNED:
        _UNREADABLE_WARNED.add(path)
        LOG.warning("device document %s unreadable: write targets are not "
                    "checked until it can be read", path)
    return None


def _checked_row(body: Dict[str, Any], existing_id: Optional[str],
                 targets: Any) -> Tuple[Optional[Dict[str, Any]], str, List[Dict[str, Any]]]:
    """validate_row + the refusal rule: (row, "", dropped) or
    (None, error, dropped) — `invalid_elements` for a gutted body."""
    dropped: List[Dict[str, Any]] = []
    row, err = validate_row(body, existing_id, dropped=dropped, targets=targets)
    if err:
        return None, err, []
    if _gutted(body, row):
        return None, "invalid_elements", dropped
    return row, "", dropped


def _refused(err: str, dropped: List[Dict[str, Any]],
             row_index: Optional[int] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": err}
    if err == "invalid_elements":
        if row_index is not None:
            out["row"] = row_index
        out["dropped"] = dropped
    return out


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
        cleaned = []
        all_dropped: List[Dict[str, Any]] = []
        targets = _target_index()
        # Seed the mint with every explicit id of the batch FIRST: an id-less
        # row minted before an explicit row carrying the same id used to land
        # two rows under one id (review 1.0.6.39 N2).
        ids = _explicit_ids(body["scenarios"][:SCENARIOS_MAX])
        for i, raw in enumerate(body["scenarios"][:SCENARIOS_MAX]):
            if not isinstance(raw, dict):
                continue
            row, err, dropped = _checked_row(raw, None, targets)
            if err:
                return _refused(err, dropped, i)  # all-or-nothing
            all_dropped += [dict(d, row=i) for d in dropped]
            if not row["id"]:
                row["id"] = _new_id(ids)
                ids.append(row["id"])
            cleaned.append(row)
        doc["scenarios"] = cleaned
        save(doc, path)
        return _ok(doc, **({"dropped": all_dropped} if all_dropped else {}))
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
        all_dropped = []
        targets = _target_index()
        ids = [s.get("id") for s in doc["scenarios"]
               if isinstance(s, dict) and isinstance(s.get("id"), str)]
        # Explicit batch ids seed the mint too (review 1.0.6.39 N2): a minted
        # id must never collide with an explicit id later in the same batch.
        ids += _explicit_ids(body["upsert"][:16])
        for i, raw in enumerate(body["upsert"][:16]):
            if not isinstance(raw, dict):
                return {"ok": False, "error": "bad json"}
            row, err, dropped = _checked_row(
                raw, raw.get("id") if isinstance(raw.get("id"), str) else None, targets)
            if err:
                return _refused(err, dropped, i)
            all_dropped += [dict(d, row=i) for d in dropped]
            if not row["id"]:
                row["id"] = _new_id(ids)
            # Every resulting id, client-supplied or minted: the cap is judged
            # on the document the merge would produce (review 1.0.6.39 F1 —
            # counting only minted ids let a 16-row explicit-id batch store 80).
            ids.append(row["id"])
            cleaned.append(row)
        if len(set(ids)) > SCENARIOS_MAX:
            return {"ok": False, "error": "too_many"}  # post-merge total
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
        return _ok(doc, **({"dropped": all_dropped} if all_dropped else {}))
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
    prev_row = next((s for s in doc["scenarios"] if isinstance(s, dict)
                     and sid and s.get("id") == sid), None)
    if prev_row is not None and all(body.get(k) is None for k in _BODY_PARTS):
        # Partial update: the carried keys over the stored row. The body
        # parts were validated when stored, so the target check is not
        # re-run (a device renamed since must not block a rename/disable).
        merged = {k: v for k, v in prev_row.items() if k not in _LAST_KEYS}
        merged.update({k: v for k, v in body.items()
                       if not (k in _BODY_PARTS and v is None)})  # null = not sent
        row, err, dropped = _checked_row(merged, sid, None)
    else:
        row, err, dropped = _checked_row(body, sid or None, _target_index())
    if err:
        return _refused(err, dropped)
    ids = [s.get("id") for s in doc["scenarios"] if isinstance(s, dict)]
    if not row["id"]:
        row["id"] = _new_id([i for i in ids if isinstance(i, str)])
    idx = next((i for i, s in enumerate(doc["scenarios"]) if isinstance(s, dict) and s.get("id") == row["id"]), None)
    if idx is None:
        if len(doc["scenarios"]) >= SCENARIOS_MAX:
            return {"ok": False, "error": "too_many"}
        doc["scenarios"].append(row)
    else:
        prev = doc["scenarios"][idx]
        row["last_run"] = prev.get("last_run")
        row["last_error"] = prev.get("last_error") or ""
        doc["scenarios"][idx] = row
    save(doc, path)
    listed_now = listed(doc)
    extra: Dict[str, Any] = {"dropped": dropped} if dropped else {}
    return _ok(doc, scenario=listed_now[next(i for i, s in enumerate(listed_now) if s["id"] == row["id"])],
               **extra)


def append_run(doc: Dict[str, Any], rec: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    """Unbuffered variant for a caller without an engine: journal write now."""
    journal = Journal(path)
    journal.append_run(doc, rec)
    journal.flush(doc)


def enqueue_notify(doc: Dict[str, Any], text: str, path: str = DEFAULT_PATH) -> None:
    """Unbuffered variant (code_runner without a sink): journal write now."""
    journal = Journal(path)
    journal.notify(doc, text)
    journal.flush(doc)


def take_notify(doc: Dict[str, Any], path: str = DEFAULT_PATH) -> List[Dict[str, Any]]:
    """Drain the queue — on the journal, read-modify-write, so a run record
    the engine flushed meanwhile is kept and the engine's next flush (which
    re-reads the file) does not bring the drained entries back."""
    q = list(doc.get("notify_queue") or [])
    doc["notify_queue"] = []
    jpath = journal_path(path)
    journal = _read_journal(jpath)
    if journal is not None and journal["notify_queue"]:
        journal["notify_queue"] = []
        save_journal(journal, jpath)
    return q
