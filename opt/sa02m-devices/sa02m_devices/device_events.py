"""Журнал событий: скачки напряжения / пики тока СЭ и фронты тревог Carel."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sa02m_devices.device_history_db import (
    CAREL_PLANT_STATE_CODE,
    carel_plant_code,
    db_path,
)
from sa02m_devices.stand_devices import CAREL_PLANT_RU, parse_device_id
from sa02m_devices.stand_storage_path import journaling_for_fstype, mount_fstype

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo(os.environ.get("STAND_TZ", "Europe/Moscow"))
except Exception:  # noqa: BLE001
    _TZ = timezone(timedelta(hours=3))

def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _ienv(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


def voltage_jump_v() -> float:
    return _fenv("STAND_DEVICES_VOLTAGE_JUMP_V", 15.0)


def current_spike_ratio() -> float:
    return _fenv("STAND_DEVICES_CURRENT_SPIKE_RATIO", 1.8)


def current_spike_min_a() -> float:
    return _fenv("STAND_DEVICES_CURRENT_SPIKE_MIN_A", 0.5)


def current_spike_abs_a() -> float:
    return _fenv("STAND_DEVICES_CURRENT_SPIKE_ABS_A", 0.3)


def event_cooldown_s() -> float:
    return _fenv("STAND_DEVICES_EVENT_COOLDOWN_S", 120.0)


def events_limit_default() -> int:
    return _ienv("STAND_DEVICES_EVENTS_LIMIT", 100)

_PHASES = ("a", "b", "c")
_PHASE_RU = {"a": "A", "b": "B", "c": "C"}

# Carel AHU edge events (E2): the fault moment lives here, at the logger's 1 Hz
# tick, not in the 10 s-averaged carel_samples series.
CAREL_EVENT_KINDS = ("carel_alarm_on", "carel_alarm_off", "carel_plant_state")
# Last seen (alarm, plant_state code) per (db path, device id). Keyed by the DB
# path so two archives (tests, a promote mid-run) never share a baseline; seeded
# from carel_samples on first sight so a restart compares against the last
# ARCHIVED state instead of silently re-baselining.
_CAREL_PREV: dict[tuple[str, str], dict[str, float | None]] = {}
_CAREL_PLANT_WORD = {code: word for word, code in CAREL_PLANT_STATE_CODE.items()}

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS device_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    port_num INTEGER,
    addr INTEGER,
    value REAL,
    ref_value REAL,
    message TEXT NOT NULL
);
"""


def ensure_events_schema(path: Path | None = None) -> Path:
    """Create device_events (+ indexes) in the DB at `path`; return the path.

    The migrate roster merges this table like the sample tables, and the
    destination must carry the schema BEFORE the merge reads PRAGMA table_info."""
    p = db_path(path)
    _connect(p).close()
    return p


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    fst = mount_fstype(p.parent)
    journal, sync = journaling_for_fstype(fst)
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute(f"PRAGMA synchronous={sync}")
    conn.executescript(_CREATE_EVENTS)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_events_ts ON device_events(ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_events_dedup "
        "ON device_events(device_id, kind, phase, ts)"
    )
    return conn


def _ensure_seed_table(
    conn: sqlite3.Connection,
    path: Path | None,
    table: str,
    column: str = "",
) -> None:
    """Make sure the history table a detector seeds from exists IN THE SHAPE the
    seed reads — WITHOUT a second open on the steady state.

    Both detectors run on every 1 Hz logger tick and used to call
    `history_db.ensure_schema(path)` unconditionally — a full extra connect +
    DDL + PK-migration probe per tick on top of their own `_connect()` (ship
    review 1.0.6.39, advisory). One `sqlite_master` query on the connection we
    already hold answers the question; `ensure_schema()` runs only when the
    table is really absent (a brand-new file, a rotation, a direct call before
    any sample landed), which keeps the fresh-file behaviour intact.

    `column` names a column the seed query needs, for a table whose SHAPE can be
    older than this release: on a board upgrading from 1.0.6.40 `carel_samples`
    exists but is still the long `(metric, value)` table, and the wide seed read
    would raise into a silent `None` baseline — a cleared alarm losing its `off`
    row on exactly the restart the seed exists for. `ensure_schema` migrates it.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if row is not None and column:
        cols = {
            str(r[1])
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in cols:
            row = None
    if row is None:
        from sa02m_devices.device_history_db import ensure_schema

        ensure_schema(path)


def _day_start_ts(now: float | None = None) -> float:
    t = float(now if now is not None else time.time())
    local = datetime.fromtimestamp(t, tz=_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def _recent_exists(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    kind: str,
    phase: str,
    ts: float,
    cooldown_s: float,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM device_events"
        " WHERE device_id = ? AND kind = ? AND phase = ?"
        " AND ts >= ? LIMIT 1",
        (device_id, kind, phase, float(ts) - float(cooldown_s)),
    ).fetchone()
    return bool(row)


def _insert_event(
    conn: sqlite3.Connection,
    *,
    ts: float,
    device_id: str,
    kind: str,
    phase: str,
    port_num: Any,
    addr: Any,
    value: float | None,
    ref_value: float | None,
    message: str,
    cooldown_s: float | None = None,
) -> bool:
    """`cooldown_s` None → the CE spike cooldown; 0 → every edge lands (Carel)."""
    if _recent_exists(
        conn,
        device_id=device_id,
        kind=kind,
        phase=phase,
        ts=ts,
        cooldown_s=event_cooldown_s() if cooldown_s is None else cooldown_s,
    ):
        return False
    conn.execute(
        """
        INSERT INTO device_events(
            ts, device_id, kind, phase, port_num, addr, value, ref_value, message
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            float(ts),
            device_id,
            kind,
            phase,
            port_num,
            addr,
            value,
            ref_value,
            message,
        ),
    )
    return True


def _prev_ce_row(
    conn: sqlite3.Connection, device_id: str, ts: float
) -> dict[str, float | None] | None:
    row = conn.execute(
        """
        SELECT voltage_a, voltage_b, voltage_c,
               current_a, current_b, current_c
        FROM ce_samples
        WHERE device_id = ? AND ts < ?
        ORDER BY ts DESC LIMIT 1
        """,
        (device_id, float(ts)),
    ).fetchone()
    if not row:
        return None
    keys = (
        "voltage_a", "voltage_b", "voltage_c",
        "current_a", "current_b", "current_c",
    )
    out: dict[str, float | None] = {}
    for i, k in enumerate(keys):
        try:
            out[k] = None if row[i] is None else float(row[i])
        except (TypeError, ValueError):
            out[k] = None
    return out


def _day_avg_current(
    conn: sqlite3.Connection, device_id: str, ts: float
) -> dict[str, float | None]:
    t0 = _day_start_ts(ts)
    row = conn.execute(
        """
        SELECT avg(current_a), avg(current_b), avg(current_c)
        FROM ce_samples
        WHERE device_id = ? AND ts >= ? AND ts <= ?
          AND (current_a IS NOT NULL OR current_b IS NOT NULL OR current_c IS NOT NULL)
        """,
        (device_id, t0, float(ts)),
    ).fetchone()
    out: dict[str, float | None] = {"a": None, "b": None, "c": None}
    if not row:
        return out
    for i, ph in enumerate(_PHASES):
        try:
            out[ph] = None if row[i] is None else float(row[i])
        except (TypeError, ValueError):
            out[ph] = None
    return out


def detect_ce_events(
    snapshot: dict[str, Any],
    *,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Сравнить текущий снимок СЭ с предыдущим/средним за день и записать события."""
    ts = float(snapshot.get("ts") or time.time())
    ce_list = [
        d
        for d in (snapshot.get("ce") or [])
        if isinstance(d, dict) and str(d.get("id") or "").strip()
    ]
    if not ce_list:
        return []

    created: list[dict[str, Any]] = []
    conn = _connect(path)
    try:
        _ensure_seed_table(conn, path, "ce_samples")
        with conn:
            for ce in ce_list:
                did = str(ce.get("id") or "").strip()
                meta = parse_device_id(did)
                port_num = meta.get("port_num")
                addr = meta.get("addr")
                volt = ce.get("voltage") if isinstance(ce.get("voltage"), dict) else {}
                curr = ce.get("current") if isinstance(ce.get("current"), dict) else {}
                prev = _prev_ce_row(conn, did, ts)
                day_avg = _day_avg_current(conn, did, ts)

                for ph in _PHASES:
                    ph_ru = _PHASE_RU[ph]
                    v_now = volt.get(ph)
                    try:
                        v_now_f = float(v_now) if v_now is not None else None
                    except (TypeError, ValueError):
                        v_now_f = None
                    v_prev = prev.get(f"voltage_{ph}") if prev else None
                    if (
                        v_now_f is not None
                        and v_prev is not None
                        and abs(v_now_f - v_prev) >= voltage_jump_v()
                    ):
                        up = v_now_f > v_prev
                        kind = "voltage_up" if up else "voltage_down"
                        verb = "выросло" if up else "уменьшилось"
                        msg = (
                            f"Напряжение фазы {ph_ru} резко {verb}: "
                            f"{v_prev:.1f} → {v_now_f:.1f} В"
                        )
                        if _insert_event(
                            conn,
                            ts=ts,
                            device_id=did,
                            kind=kind,
                            phase=ph_ru,
                            port_num=port_num,
                            addr=addr,
                            value=v_now_f,
                            ref_value=v_prev,
                            message=msg,
                        ):
                            created.append(
                                {
                                    "ts": ts,
                                    "device_id": did,
                                    "kind": kind,
                                    "phase": ph_ru,
                                    "message": msg,
                                }
                            )

                    i_now = curr.get(ph)
                    try:
                        i_now_f = float(i_now) if i_now is not None else None
                    except (TypeError, ValueError):
                        i_now_f = None
                    i_avg = day_avg.get(ph)
                    if (
                        i_now_f is not None
                        and i_avg is not None
                        and i_avg >= current_spike_abs_a()
                        and i_now_f >= current_spike_min_a()
                        and i_now_f >= i_avg * current_spike_ratio()
                    ):
                        # доп. условие: рост относительно предыдущего семпла
                        i_prev = prev.get(f"current_{ph}") if prev else None
                        if i_prev is not None and i_now_f <= i_prev * 1.05:
                            continue
                        kind = "current_spike"
                        msg = (
                            f"Ток фазы {ph_ru} резко вырос выше среднего за день: "
                            f"{i_now_f:.3f} А (ср. {i_avg:.3f} А)"
                        )
                        if _insert_event(
                            conn,
                            ts=ts,
                            device_id=did,
                            kind=kind,
                            phase=ph_ru,
                            port_num=port_num,
                            addr=addr,
                            value=i_now_f,
                            ref_value=i_avg,
                            message=msg,
                        ):
                            created.append(
                                {
                                    "ts": ts,
                                    "device_id": did,
                                    "kind": kind,
                                    "phase": ph_ru,
                                    "message": msg,
                                }
                            )
    finally:
        conn.close()
    return created


def _f(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def reset_carel_event_state() -> None:
    """Forget every in-memory Carel baseline (tests; a process restart does
    this implicitly — the next tick re-seeds from carel_samples)."""
    _CAREL_PREV.clear()


def _carel_prev_from_archive(
    conn: sqlite3.Connection, device_id: str
) -> dict[str, float | None]:
    out: dict[str, float | None] = {"alarm": None, "plant_state": None}
    for metric in out:
        # `metric` is a key of the literal above (an archive COLUMN since the
        # wide table, 1.0.6.41), never request input. Last NON-NULL per column,
        # not the last row: a tick whose alarm probe was unread archives NULL
        # there and must not erase the baseline the restart compares against.
        try:
            row = conn.execute(
                f"SELECT {metric} FROM carel_samples"
                f" WHERE device_id = ? AND {metric} IS NOT NULL"
                " ORDER BY ts DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        out[metric] = float(row[0]) if row and row[0] is not None else None
    return out


def _plant_ru(code: float | int) -> str:
    word = _CAREL_PLANT_WORD.get(int(code), "")
    return CAREL_PLANT_RU.get(word, str(int(code)))


def detect_carel_events(
    snapshot: dict[str, Any],
    *,
    path: Path | None = None,
    cooldown_s: float = 0.0,
) -> list[dict[str, Any]]:
    """Carel AHU edges → device_events: `alarm` 0→1 / 1→0 and every
    `plant_state` change, at the tick's own ts.

    Runs on EVERY logger tick (1 Hz on USB/SD), not on the 10 s archive cadence,
    because the Operator reads the fault MOMENT from these rows. `cooldown_s`
    defaults to 0 so a flap inside the CE cooldown still lands — dedup is by
    edge, not by time. A device seen for the first time (no in-memory baseline
    and nothing in carel_samples) only baselines; it never fabricates an edge.
    """
    ts = float(snapshot.get("ts") or time.time())
    carel_list = [
        d
        for d in (snapshot.get("carel") or [])
        if isinstance(d, dict) and str(d.get("id") or "").strip()
    ]
    if not carel_list:
        return []

    key_path = str(db_path(path))
    created: list[dict[str, Any]] = []
    conn = _connect(path)
    try:
        # `alarm` = the wide shape the seed reads (a 1.0.6.40 long table migrates).
        _ensure_seed_table(conn, path, "carel_samples", column="alarm")
        with conn:
            for dev in carel_list:
                did = str(dev.get("id") or "").strip()
                meta = parse_device_id(did)
                alarm_now = _f(dev.get("alarm"))
                plant_now = carel_plant_code(dev.get("plant_state"))
                key = (key_path, did)
                prev = _CAREL_PREV.get(key)
                if prev is None:
                    prev = _carel_prev_from_archive(conn, did)
                alarm_prev = prev.get("alarm")
                plant_prev = prev.get("plant_state")

                if (
                    alarm_now is not None
                    and alarm_prev is not None
                    and alarm_now != alarm_prev
                ):
                    rising = alarm_now > alarm_prev
                    kind = "carel_alarm_on" if rising else "carel_alarm_off"
                    codes = str(dev.get("alarm_text") or "").strip()
                    if rising:
                        msg = "Общая авария установки: появилась"
                        if codes:
                            msg += f" ({codes})"
                    else:
                        msg = "Общая авария установки: снята"
                    if _insert_event(
                        conn,
                        ts=ts,
                        device_id=did,
                        kind=kind,
                        phase="",
                        port_num=meta.get("port_num"),
                        addr=meta.get("addr"),
                        value=alarm_now,
                        ref_value=alarm_prev,
                        message=msg,
                        cooldown_s=cooldown_s,
                    ):
                        created.append(
                            {"ts": ts, "device_id": did, "kind": kind,
                             "phase": "", "message": msg}
                        )

                if (
                    plant_now is not None
                    and plant_prev is not None
                    and plant_now != int(plant_prev)
                ):
                    kind = "carel_plant_state"
                    msg = (
                        f"Состояние установки: {_plant_ru(plant_prev)} → "
                        f"{_plant_ru(plant_now)}"
                    )
                    if _insert_event(
                        conn,
                        ts=ts,
                        device_id=did,
                        kind=kind,
                        phase="",
                        port_num=meta.get("port_num"),
                        addr=meta.get("addr"),
                        value=float(plant_now),
                        ref_value=float(plant_prev),
                        message=msg,
                        cooldown_s=cooldown_s,
                    ):
                        created.append(
                            {"ts": ts, "device_id": did, "kind": kind,
                             "phase": "", "message": msg}
                        )

                # A None reading (probe unread this tick) keeps the old baseline
                # rather than erasing it — the next real value still compares.
                _CAREL_PREV[key] = {
                    "alarm": alarm_now if alarm_now is not None else alarm_prev,
                    "plant_state": (
                        float(plant_now) if plant_now is not None else plant_prev
                    ),
                }
    finally:
        conn.close()
    return created


def list_events(
    *,
    path: Path | None = None,
    limit: int | None = None,
    device_id: str | None = None,
    t0: float | None = None,
    t1: float | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Newest first. `t0`/`t1` (epoch s) window on ts; `kinds` restricts to a
    kind set (the chart asks for CAREL_EVENT_KINDS only)."""
    lim = max(1, min(int(limit if limit is not None else events_limit_default()), 500))
    conn = _connect(path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if device_id:
            clauses.append("device_id = ?")
            params.append(str(device_id).strip())
        if t0 is not None:
            clauses.append("ts >= ?")
            params.append(float(t0))
        if t1 is not None:
            clauses.append("ts <= ?")
            params.append(float(t1))
        kind_list = [str(k) for k in (kinds or []) if str(k)]
        if kind_list:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kind_list))
            params.extend(kind_list)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT id, ts, device_id, kind, phase, port_num, addr,"
            " value, ref_value, message"
            f" FROM device_events{where}"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            [*params, lim],
        ).fetchall()
    finally:
        conn.close()

    events: list[dict[str, Any]] = []
    for r in rows:
        did = str(r[2] or "")
        meta = parse_device_id(did)
        port = r[5] if r[5] is not None else meta.get("port_num")
        addr = r[6] if r[6] is not None else meta.get("addr")
        ts = float(r[1])
        local = datetime.fromtimestamp(ts, tz=_TZ)
        events.append(
            {
                "id": int(r[0]),
                "ts": ts,
                "ts_label": local.strftime("%Y-%m-%d %H:%M:%S"),
                "device_id": did,
                "kind": str(r[3] or ""),
                "phase": str(r[4] or ""),
                "port_num": port,
                "addr": addr,
                "value": r[7],
                "ref_value": r[8],
                "message": str(r[9] or ""),
            }
        )
    return {"ok": True, "events": events, "count": len(events)}


def purge_events(
    path: Path | None = None, *, cutoff: float | None = None
) -> int:
    cut = float(cutoff if cutoff is not None else time.time() - 30 * 86400)
    conn = _connect(path)
    try:
        with conn:
            n = conn.execute(
                "DELETE FROM device_events WHERE ts < ?", (cut,)
            ).rowcount
        return int(n or 0)
    finally:
        conn.close()
