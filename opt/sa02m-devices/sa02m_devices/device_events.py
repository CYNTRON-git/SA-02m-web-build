"""Журнал событий СЭ: скачки напряжения и пики тока относительно суток."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sa02m_devices.device_history_db import db_path
from sa02m_devices.stand_devices import parse_device_id
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
) -> bool:
    if _recent_exists(
        conn,
        device_id=device_id,
        kind=kind,
        phase=phase,
        ts=ts,
        cooldown_s=event_cooldown_s(),
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
        # Нужна таблица ce_samples (создаётся history_db)
        from sa02m_devices.device_history_db import ensure_schema

        ensure_schema(path)
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


def list_events(
    *,
    path: Path | None = None,
    limit: int | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    lim = max(1, min(int(limit if limit is not None else events_limit_default()), 500))
    conn = _connect(path)
    try:
        where = ""
        params: list[Any] = []
        if device_id:
            where = " WHERE device_id = ?"
            params.append(str(device_id).strip())
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
