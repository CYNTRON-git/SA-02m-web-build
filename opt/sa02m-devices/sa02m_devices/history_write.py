"""live_snapshot → archive rows (DTV/CE every tick, MR/Carel on their own
cadence) and the retention purge."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from sa02m_devices.history_metrics import (
    CAREL_COLUMNS,
    _DTV_SENSOR_COLUMNS,
    _DTV_SENSOR_LIST_COLS,
    carel_plant_code,
)
from sa02m_devices.history_store import EVENT_RETENTION_S, RETENTION_S, _connect, log


def _dtv_sensor_col_values(dtv: dict[str, Any]) -> dict[str, float | None]:
    """Flatten the dtv dict's `*_sensors` rosters into {column: value} for insert."""
    vals: dict[str, float | None] = {c: None for c in _DTV_SENSOR_COLUMNS}
    for field, chip_map in _DTV_SENSOR_LIST_COLS.items():
        roster = dtv.get(field)
        if not isinstance(roster, list):
            continue
        for item in roster:
            if not isinstance(item, dict):
                continue
            col = chip_map.get(str(item.get("t") or ""))
            if not col:
                continue
            v = item.get("v")
            if v is None:
                continue
            try:
                vals[col] = float(v)
            except (TypeError, ValueError):
                continue
    return vals


def _as_device_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [d for d in value if isinstance(d, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _insert_dtv(conn: sqlite3.Connection, ts: float, dtv: dict[str, Any]) -> None:
    if not (
        dtv.get("ok")
        or any(
            dtv.get(k) is not None
            for k in (
                "room_temp", "humidity", "eco2_ppm", "tvoc_mg_m3",
                "pressure_mmhg", "light_pct",
            )
        )
    ):
        return
    sensor_cols = _dtv_sensor_col_values(dtv)
    col_sql = ", ".join(_DTV_SENSOR_COLUMNS)
    col_ph = ", ".join("?" for _ in _DTV_SENSOR_COLUMNS)
    conn.execute(
        f"""
        INSERT OR REPLACE INTO dtv_samples(
            ts, device_id, room_temp, humidity, eco2_ppm,
            tvoc_mg_m3, pressure_mmhg, light_pct, presence,
            {col_sql}
        ) VALUES (?,?,?,?,?,?,?,?,?,{col_ph})
        """,
        (
            ts,
            str(dtv.get("id") or ""),
            dtv.get("room_temp"),
            dtv.get("humidity"),
            dtv.get("eco2_ppm"),
            dtv.get("tvoc_mg_m3"),
            dtv.get("pressure_mmhg"),
            dtv.get("light_pct"),
            dtv.get("presence"),
            *(sensor_cols[c] for c in _DTV_SENSOR_COLUMNS),
        ),
    )


def _insert_ce(conn: sqlite3.Connection, ts: float, ce: dict[str, Any]) -> None:
    volt = ce.get("voltage") if isinstance(ce.get("voltage"), dict) else {}
    curr = ce.get("current") if isinstance(ce.get("current"), dict) else {}
    pwr = ce.get("power_w") if isinstance(ce.get("power_w"), dict) else {}
    if not (ce.get("ok") or volt.get("a") is not None):
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO ce_samples(
            ts, device_id, voltage_a, voltage_b, voltage_c,
            current_a, current_b, current_c,
            power_w_a, power_w_b, power_w_c, power_w_total,
            frequency_hz, energy_kwh_import
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ts,
            str(ce.get("id") or ""),
            volt.get("a"), volt.get("b"), volt.get("c"),
            curr.get("a"), curr.get("b"), curr.get("c"),
            pwr.get("a"), pwr.get("b"), pwr.get("c"),
            pwr.get("total"),
            ce.get("frequency_hz"),
            ce.get("energy_kwh_import"),
        ),
    )


def insert_sample(snapshot: dict[str, Any], path: Path | None = None) -> None:
    """Записать снимок live_snapshot() — все ДТВ/СЭ в списках (по умолчанию)."""
    ts = float(snapshot.get("ts") or time.time())
    dtv_list = _as_device_list(snapshot.get("dtv"))
    ce_list = _as_device_list(snapshot.get("ce"))
    conn = _connect(path)
    try:
        with conn:
            for dtv in dtv_list:
                _insert_dtv(conn, ts, dtv)
            for ce in ce_list:
                _insert_ce(conn, ts, ce)
    finally:
        conn.close()


def _ai_ch_num(field: str) -> int:
    """«ai_5» → 5 (сортировка каналов); нераспознанное → большое число."""
    s = str(field or "")
    if s.startswith("ai_"):
        s = s[3:]
    try:
        return int(s)
    except (TypeError, ValueError):
        return 10_000


def _insert_mr(conn: sqlite3.Connection, ts: float, mr: dict[str, Any]) -> None:
    """Одна строка на ВКЛЮЧЁННЫЙ канал с конечным значением (иначе — нет строки,
    как «—» на карточке; отключённый канал в графике не появляется)."""
    device_id = str(mr.get("id") or "")
    channels = mr.get("channels")
    if not isinstance(channels, list):
        return
    rows: list[tuple[Any, ...]] = []
    for c in channels:
        if not isinstance(c, dict):
            continue
        if not c.get("enabled"):
            continue
        ch = c.get("ch")
        val = c.get("value")
        if ch is None or val is None or not _number_is_finite(val):
            continue
        try:
            rows.append(
                (ts, device_id, int(ch), float(val), str(c.get("unit") or ""))
            )
        except (TypeError, ValueError):
            continue
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO mr_samples(ts, device_id, ch, value, unit)"
            " VALUES (?,?,?,?,?)",
            rows,
        )


def insert_mr_sample(snapshot: dict[str, Any], path: Path | None = None) -> None:
    """Записать MR-02m AI из снимка (отдельная каденция от dtv/ce — 10 с)."""
    ts = float(snapshot.get("ts") or time.time())
    mr_list = _as_device_list(snapshot.get("mr"))
    if not mr_list:
        return
    conn = _connect(path)
    try:
        with conn:
            for mr in mr_list:
                _insert_mr(conn, ts, mr)
    finally:
        conn.close()


_CAREL_INSERT_SQL = (
    "INSERT OR REPLACE INTO carel_samples"
    f"(ts, device_id, {', '.join(CAREL_COLUMNS)})"
    f" VALUES (?,?,{','.join('?' for _ in CAREL_COLUMNS)})"
)


def _insert_carel(conn: sqlite3.Connection, ts: float, carel: dict[str, Any]) -> None:
    """Одна широкая строка на такт: колонка на метрику, NULL — не снято.

    An unfitted probe, a non-finite reading and an unknown `plant_state` word all
    archive NULL rather than an invented value; a tick where EVERY metric is
    absent writes no row at all (what the long writer did with no rows to write).
    """
    device_id = str(carel.get("id") or "")
    values: list[float | None] = []
    for col in CAREL_COLUMNS:
        val = carel.get(col)
        if col == "plant_state":
            val = carel_plant_code(val)
        if val is None or not _number_is_finite(val):
            values.append(None)
            continue
        try:
            values.append(float(val))
        except (TypeError, ValueError):
            values.append(None)
    if any(v is not None for v in values):
        conn.execute(_CAREL_INSERT_SQL, (ts, device_id, *values))


def insert_carel_sample(snapshot: dict[str, Any], path: Path | None = None) -> None:
    """Записать Carel AHU из снимка (та же 10 с каденция, что у MR)."""
    ts = float(snapshot.get("ts") or time.time())
    carel_list = _as_device_list(snapshot.get("carel"))
    if not carel_list:
        return
    conn = _connect(path)
    try:
        with conn:
            for carel in carel_list:
                _insert_carel(conn, ts, carel)
    finally:
        conn.close()


def purge_old(path: Path | None = None, *, now: float | None = None) -> dict[str, int]:
    t_now = float(now if now is not None else time.time())
    cutoff = t_now - RETENTION_S
    ev_cutoff = t_now - EVENT_RETENTION_S
    conn = _connect(path)
    try:
        with conn:
            c1 = conn.execute(
                "DELETE FROM dtv_samples WHERE ts < ?", (cutoff,)
            ).rowcount
            c2 = conn.execute(
                "DELETE FROM ce_samples WHERE ts < ?", (cutoff,)
            ).rowcount
            c3 = conn.execute(
                "DELETE FROM mr_samples WHERE ts < ?", (cutoff,)
            ).rowcount
            c4 = conn.execute(
                "DELETE FROM carel_samples WHERE ts < ?", (cutoff,)
            ).rowcount
        ev_deleted = 0
        try:
            from sa02m_devices import device_events

            ev_deleted = device_events.purge_events(path=path, cutoff=ev_cutoff)
        except Exception as exc:  # noqa: BLE001
            # A swallowed failure here left a healthy-looking report over a
            # journal that was never trimmed; the samples above are already
            # purged, so report zero and say why.
            log.warning("device_events purge failed: %s", exc)
        return {
            "dtv_deleted": int(c1 or 0),
            "ce_deleted": int(c2 or 0),
            "mr_deleted": int(c3 or 0),
            "carel_deleted": int(c4 or 0),
            "events_deleted": int(ev_deleted),
            "cutoff": cutoff,
        }
    finally:
        conn.close()


def _number_is_finite(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and abs(f) != float("inf")
