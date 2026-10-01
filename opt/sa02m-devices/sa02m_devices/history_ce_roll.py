"""Pre-aggregated СЭ buckets for the power / energy bar chart.

Raw `ce_samples` stay the 1 Hz archive. Two rollup grains are maintained in the
same SQLite file as samples arrive (and backfilled once from an existing
archive):

  * 3600 s — local hour. Serves the 24 ч chart (and a zoom from 1 day up to
    just under 7 days).
  * 86400 s — local civil day (Europe/Moscow, UTC+3, no DST). Serves 7 д,
    30 д, «с начала месяца» and «за месяц».

Shorter charts (1 ч → 5 min, 6 ч → 15 min, under 1 ч → 1 min) still read raw
rows, but already grouped to that bar width — the browser does not re-bucket
a ~400-point series.

Units (do not rescale):

  * `power_w_*` is instantaneous watts. A bucket's power is the arithmetic
    mean of the samples in that local bucket. It is not ΔE/Δt.
  * `energy_kwh_import` is the cumulative import register in kWh. A bucket's
    energy is ΔE: the sum of non-negative steps of that register, with the
    same glitch / reset rules as `bucketEnergyDeltaPoints` in devices.js.
    A drop is not a negative bar and a one-sample dip does not spike on
    recovery.

The logger applies one sample to the open hour and day rows (sum/count and
the energy cursor). It does not rescan `ce_samples`. Backfill walks raw rows
forward from a watermark, at most `CHUNK` rows per call, and stops for good
once the watermark has caught the table.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any

from sa02m_devices.history_ranges import _TZ, _parse_custom_window


# Rows walked per call. The logger tick stays on the small cap; a chart
# request may take a larger one, still finite, and then either serves the
# rollup or falls back to a windowed raw aggregate.
LOGGER_CHUNK = 2000
HISTORY_CHUNK = 8000

_GRAINS = (3600, 86400)
_POWER_FIELDS = ("power_w_a", "power_w_b", "power_w_c", "power_w_total")

_CREATE = """
CREATE TABLE IF NOT EXISTS ce_roll (
    device_id TEXT NOT NULL,
    grain INTEGER NOT NULL,
    bucket_ts REAL NOT NULL,
    pa_sum REAL NOT NULL DEFAULT 0,
    pa_n INTEGER NOT NULL DEFAULT 0,
    pb_sum REAL NOT NULL DEFAULT 0,
    pb_n INTEGER NOT NULL DEFAULT 0,
    pc_sum REAL NOT NULL DEFAULT 0,
    pc_n INTEGER NOT NULL DEFAULT 0,
    pt_sum REAL NOT NULL DEFAULT 0,
    pt_n INTEGER NOT NULL DEFAULT 0,
    e_delta REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, grain, bucket_ts)
);
CREATE TABLE IF NOT EXISTS ce_roll_cursor (
    device_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    ref REAL,
    low REAL NOT NULL DEFAULT 0,
    dropped INTEGER NOT NULL DEFAULT 0,
    prev_hour REAL,
    prev_day REAL
);
CREATE TABLE IF NOT EXISTS ce_roll_wm (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts REAL NOT NULL,
    device_id TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
"""

_UPSERT_ROLL = """
INSERT INTO ce_roll (
    device_id, grain, bucket_ts,
    pa_sum, pa_n, pb_sum, pb_n, pc_sum, pc_n, pt_sum, pt_n, e_delta
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(device_id, grain, bucket_ts) DO UPDATE SET
    pa_sum = pa_sum + excluded.pa_sum,
    pa_n = pa_n + excluded.pa_n,
    pb_sum = pb_sum + excluded.pb_sum,
    pb_n = pb_n + excluded.pb_n,
    pc_sum = pc_sum + excluded.pc_sum,
    pc_n = pc_n + excluded.pc_n,
    pt_sum = pt_sum + excluded.pt_sum,
    pt_n = pt_n + excluded.pt_n,
    e_delta = e_delta + excluded.e_delta
"""

_UPSERT_CURSOR = """
INSERT INTO ce_roll_cursor (
    device_id, ts, ref, low, dropped, prev_hour, prev_day
) VALUES (?,?,?,?,?,?,?)
ON CONFLICT(device_id) DO UPDATE SET
    ts = excluded.ts,
    ref = excluded.ref,
    low = excluded.low,
    dropped = excluded.dropped,
    prev_hour = excluded.prev_hour,
    prev_day = excluded.prev_day
"""


def ce_chart_bucket_s(range_key: str) -> float:
    """Bar width for СЭ power/energy. Mirrors devices.js `barBucketSec`."""
    if range_key in ("mtd", "month"):
        return 86400.0
    win = _parse_custom_window(range_key)
    if win is None:
        if range_key in ("7d", "30d"):
            return 86400.0
        if range_key == "24h":
            return 3600.0
        if range_key == "6h":
            return 900.0
        return 300.0
    if win >= 7 * 86400:
        return 86400.0
    if win >= 86400:
        return 3600.0
    if win >= 6 * 3600:
        return 900.0
    if win >= 3600:
        return 300.0
    return 60.0


def _offset_s(ts: float) -> int:
    off = datetime.fromtimestamp(float(ts), _TZ).utcoffset()
    return int(off.total_seconds()) if off else 0


def bucket_start(ts: float, bucket_s: float) -> float:
    """Local bucket start, same alignment as devices.js `barBucketStart`."""
    off = _offset_s(ts)
    shifted = float(ts) + off
    start = math.floor(shifted / float(bucket_s)) * float(bucket_s) - off
    return round(start, 3)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _prev_bucket(bucket_ts: float, bucket_s: float) -> float:
    return bucket_start(bucket_ts - 1.0, bucket_s)


def _adjacent(prev: float | None, bucket_ts: float, bucket_s: float) -> bool:
    if prev is None:
        return False
    return prev == bucket_ts or prev == _prev_bucket(bucket_ts, bucket_s)


def _counter_step(
    ref: float, low: float, dropped: bool, value: float
) -> tuple[float, float, bool, float]:
    """One step of the devices.js energy state machine. Returns ref, low, dropped, inc."""
    if value >= ref:
        return value, low, False, value - ref
    if not dropped:
        return ref, value, True, 0.0
    return value, low, False, max(0.0, value - low)


class _Cursor:
    __slots__ = ("ts", "ref", "low", "dropped", "prev_hour", "prev_day")

    def __init__(
        self,
        ts: float,
        ref: float | None,
        low: float,
        dropped: bool,
        prev_hour: float | None,
        prev_day: float | None,
    ) -> None:
        self.ts = ts
        self.ref = ref
        self.low = low
        self.dropped = dropped
        self.prev_hour = prev_hour
        self.prev_day = prev_day


def energy_deltas(
    samples: list[tuple[float, float]] | Any, bucket_s: float
) -> dict[float, float]:
    """ΔE per local bucket. `samples` is (ts, cumulative_kWh) in time order.

    The first sample only sets the reference. A later rise counts in that
    sample's bucket when the previous energy sample is in the same bucket or
    the one before it; a gap drops the step. Matches `bucketEnergyDeltaPoints`.
    """
    acc: dict[float, float] = {}
    ref: float | None = None
    low = 0.0
    dropped = False
    prev: float | None = None
    for ts, value in samples:
        number = _finite(value)
        if number is None:
            continue
        bucket = bucket_start(float(ts), bucket_s)
        if ref is None:
            ref = number
            prev = bucket
            continue
        ref, low, dropped, inc = _counter_step(ref, low, dropped, number)
        if inc and _adjacent(prev, bucket, bucket_s):
            acc[bucket] = acc.get(bucket, 0.0) + inc
        prev = bucket
    return acc


def ensure_ce_roll(conn: sqlite3.Connection) -> None:
    conn.executescript(_CREATE)


def _done(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT done FROM ce_roll_wm WHERE id = 1").fetchone()
    return bool(row and int(row[0]))


def _load_cursor(conn: sqlite3.Connection, device_id: str) -> _Cursor | None:
    row = conn.execute(
        "SELECT ts, ref, low, dropped, prev_hour, prev_day"
        " FROM ce_roll_cursor WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if not row:
        return None
    ref = None if row[1] is None else float(row[1])
    return _Cursor(
        float(row[0]),
        ref,
        float(row[2] or 0.0),
        bool(row[3]),
        None if row[4] is None else float(row[4]),
        None if row[5] is None else float(row[5]),
    )


def _save_cursor(conn: sqlite3.Connection, device_id: str, cur: _Cursor) -> None:
    conn.execute(
        _UPSERT_CURSOR,
        (
            device_id,
            cur.ts,
            cur.ref,
            cur.low,
            1 if cur.dropped else 0,
            cur.prev_hour,
            cur.prev_day,
        ),
    )


def _sum_n(value: Any) -> tuple[float, int]:
    number = _finite(value)
    if number is None:
        return 0.0, 0
    return number, 1


def _upsert_bucket(
    conn: sqlite3.Connection,
    device_id: str,
    grain: int,
    bucket_ts: float,
    powers: tuple[Any, Any, Any, Any],
    energy_inc: float,
) -> None:
    sums_n = [_sum_n(v) for v in powers]
    if all(n == 0 for _s, n in sums_n) and not energy_inc:
        return
    pa_s, pa_n = sums_n[0]
    pb_s, pb_n = sums_n[1]
    pc_s, pc_n = sums_n[2]
    pt_s, pt_n = sums_n[3]
    conn.execute(
        _UPSERT_ROLL,
        (
            device_id,
            int(grain),
            float(bucket_ts),
            pa_s, pa_n, pb_s, pb_n, pc_s, pc_n, pt_s, pt_n,
            float(energy_inc),
        ),
    )


def apply_live(
    conn: sqlite3.Connection,
    ts: float,
    device_id: str,
    pa: Any,
    pb: Any,
    pc: Any,
    pt: Any,
    energy: Any,
) -> None:
    """Fold one sample into the open hour and day. O(1); no raw scan.

    A sample at or before this device's cursor was already folded (backfill
    or a repeated tick) and is ignored so a retry cannot double the sums.
    """
    cur = _load_cursor(conn, device_id)
    if cur is not None and float(ts) <= cur.ts:
        return
    powers = (pa, pb, pc, pt)
    energy_n = _finite(energy)
    incs = {3600: 0.0, 86400: 0.0}
    if energy_n is None:
        new = cur if cur is not None else _Cursor(float(ts), None, 0.0, False, None, None)
        new.ts = float(ts)
    else:
        hour_b = bucket_start(ts, 3600.0)
        day_b = bucket_start(ts, 86400.0)
        if cur is None or cur.ref is None:
            new = _Cursor(float(ts), energy_n, 0.0, False, hour_b, day_b)
        else:
            ref, low, dropped, inc = _counter_step(
                cur.ref, cur.low, cur.dropped, energy_n
            )
            if inc and _adjacent(cur.prev_hour, hour_b, 3600.0):
                incs[3600] = inc
            if inc and _adjacent(cur.prev_day, day_b, 86400.0):
                incs[86400] = inc
            new = _Cursor(float(ts), ref, low, dropped, hour_b, day_b)
    for grain in _GRAINS:
        _upsert_bucket(
            conn, device_id, grain, bucket_start(ts, float(grain)), powers, incs[grain]
        )
    _save_cursor(conn, device_id, new)


def _fetch_raw(
    conn: sqlite3.Connection, wm: tuple[float, str] | None, limit: int
) -> list[tuple[Any, ...]]:
    sql = (
        "SELECT ts, device_id, power_w_a, power_w_b, power_w_c,"
        " power_w_total, energy_kwh_import FROM ce_samples"
    )
    if wm is None:
        sql += " ORDER BY ts, device_id LIMIT ?"
        args: tuple[Any, ...] = (int(limit),)
    else:
        sql += (
            " WHERE ts > ? OR (ts = ? AND device_id > ?)"
            " ORDER BY ts, device_id LIMIT ?"
        )
        args = (wm[0], wm[0], wm[1], int(limit))
    return conn.execute(sql, args).fetchall()


def _more_after(conn: sqlite3.Connection, ts: float, device_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ce_samples WHERE ts > ? OR (ts = ? AND device_id > ?) LIMIT 1",
        (ts, ts, device_id),
    ).fetchone()
    return row is not None


def _save_wm(
    conn: sqlite3.Connection, ts: float, device_id: str, done: bool
) -> None:
    conn.execute(
        "INSERT INTO ce_roll_wm (id, ts, device_id, done) VALUES (1,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET"
        " ts = excluded.ts, device_id = excluded.device_id, done = excluded.done",
        (float(ts), str(device_id), 1 if done else 0),
    )


def backfill_chunk(conn: sqlite3.Connection, limit: int) -> int:
    """Fold at most `limit` raw rows. Caller holds the write transaction.

    The watermark only moves forward. An empty remainder marks the rollup
    caught up; a later call returns immediately.
    """
    limit = max(1, int(limit))
    row = conn.execute(
        "SELECT ts, device_id, done FROM ce_roll_wm WHERE id = 1"
    ).fetchone()
    if row is not None and int(row[2]):
        return 0
    wm = None if row is None else (float(row[0]), str(row[1]))
    rows = _fetch_raw(conn, wm, limit)
    if not rows:
        if wm is None:
            _save_wm(conn, 0.0, "", True)
        else:
            _save_wm(conn, wm[0], wm[1], True)
        return 0
    for ts, device_id, pa, pb, pc, pt, energy in rows:
        apply_live(conn, float(ts), str(device_id), pa, pb, pc, pt, energy)
    last_ts = float(rows[-1][0])
    last_id = str(rows[-1][1])
    _save_wm(conn, last_ts, last_id, not _more_after(conn, last_ts, last_id))
    return len(rows)


def note_ce_rows(
    conn: sqlite3.Connection,
    rows: list[tuple[Any, ...]],
    *,
    chunk: int = LOGGER_CHUNK,
) -> None:
    """After the raw insert, inside the caller's write transaction.

    Once the archive has been folded, each new sample is O(1). Until then one
    bounded chunk of the backlog is folded; the new rows are in that scan
    when the watermark reaches them, so they are not also applied live.
    """
    if _done(conn):
        for ts, device_id, pa, pb, pc, pt, energy in rows:
            apply_live(conn, float(ts), str(device_id), pa, pb, pc, pt, energy)
        return
    backfill_chunk(conn, chunk)


def progress_backfill(conn: sqlite3.Connection, limit: int = HISTORY_CHUNK) -> bool:
    """One bounded chunk on a chart read. Returns whether the rollup is caught up."""
    if _done(conn):
        return True
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _done(conn):
            backfill_chunk(conn, limit)
        done = _done(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return done


def reset_ce_roll(conn: sqlite3.Connection) -> None:
    """Forget the fold so it is rebuilt from raw `ce_samples`.

    A media merge copies raw rows into a database whose watermark may already
    say the rollup is caught up. Those rows would never be folded. The next
    logger tick or chart read walks forward again; `backfill_until_done`
    bounds that walk.
    """
    conn.execute("DELETE FROM ce_roll")
    conn.execute("DELETE FROM ce_roll_cursor")
    conn.execute("DELETE FROM ce_roll_wm")


def purge_ce_roll(conn: sqlite3.Connection, cutoff: float) -> None:
    """Drop buckets that ended before `cutoff`. The open edge bucket stays.

    Deleting a bucket that still holds samples newer than the cutoff would
    punch a hole the cursor will not rebuild. Fully expired buckets
    (`bucket_ts + grain < cutoff`) are the ones raw purge has already emptied.
    """
    conn.execute(
        "DELETE FROM ce_roll WHERE bucket_ts + grain < ?",
        (float(cutoff),),
    )


def _grain_for(bucket_s: float) -> int | None:
    if bucket_s >= 86400.0:
        return 86400
    if bucket_s >= 3600.0:
        return 3600
    return None


def _read_rollup(
    conn: sqlite3.Connection,
    device_id: str,
    t0: float,
    t1: float,
    grain: int,
    kind: str,
) -> dict[str, list[tuple[int, float, int]]]:
    rows = conn.execute(
        "SELECT bucket_ts, pa_sum, pa_n, pb_sum, pb_n, pc_sum, pc_n,"
        " pt_sum, pt_n, e_delta FROM ce_roll"
        " WHERE device_id = ? AND grain = ? AND bucket_ts >= ? AND bucket_ts <= ?"
        " ORDER BY bucket_ts",
        (device_id, int(grain), float(t0), float(t1)),
    ).fetchall()
    out: dict[str, list[tuple[int, float, int]]] = {f: [] for f in _POWER_FIELDS}
    energy: list[tuple[int, float, int]] = []
    for row in rows:
        ts_ms = int(round(float(row[0]) * 1000))
        if kind == "avg":
            pairs = (
                ("power_w_a", row[1], row[2]),
                ("power_w_b", row[3], row[4]),
                ("power_w_c", row[5], row[6]),
                ("power_w_total", row[7], row[8]),
            )
            for field, total, count in pairs:
                n = int(count or 0)
                if n > 0 and total is not None:
                    out[field].append((ts_ms, float(total), n))
        else:
            delta = float(row[9] or 0.0)
            if delta > 0.0:
                energy.append((ts_ms, delta, 1))
    if kind == "delta":
        return {"energy_kwh_import": energy}
    return {f: pts for f, pts in out.items() if pts}


def _read_power_raw(
    conn: sqlite3.Connection,
    device_id: str,
    t0: float,
    t1: float,
    bucket_s: float,
) -> dict[str, list[tuple[int, float, int]]]:
    """Mean watts per local bucket, from raw rows. One pass per phase."""
    off = _offset_s((t0 + t1) / 2.0)
    out: dict[str, list[tuple[int, float, int]]] = {}
    for field in _POWER_FIELDS:
        rows = conn.execute(
            f"SELECT (CAST((ts + ?) / ? AS INTEGER) * ? - ?) AS b,"
            f" SUM({field}), COUNT({field})"
            f" FROM ce_samples"
            f" WHERE device_id = ? AND ts >= ? AND ts <= ? AND {field} IS NOT NULL"
            f" GROUP BY b HAVING b >= ? AND b <= ? ORDER BY b",
            (off, bucket_s, bucket_s, off, device_id, t0, t1, t0, t1),
        ).fetchall()
        pts: list[tuple[int, float, int]] = []
        for bucket, total, count in rows:
            n = int(count or 0)
            if n <= 0 or total is None or bucket is None:
                continue
            pts.append((int(round(float(bucket) * 1000)), float(total), n))
        if pts:
            out[field] = pts
    return out


def _read_energy_raw(
    conn: sqlite3.Connection,
    device_id: str,
    t0: float,
    t1: float,
    bucket_s: float,
) -> dict[str, list[tuple[int, float, int]]]:
    """ΔE per bucket over the window. One sample before t0 keeps the step
    that crosses the window edge when that sample sits in the previous bucket.
    """
    prev = conn.execute(
        "SELECT ts, energy_kwh_import FROM ce_samples"
        " WHERE device_id = ? AND ts < ? AND energy_kwh_import IS NOT NULL"
        " ORDER BY ts DESC LIMIT 1",
        (device_id, float(t0)),
    ).fetchone()
    rows = conn.execute(
        "SELECT ts, energy_kwh_import FROM ce_samples"
        " WHERE device_id = ? AND ts >= ? AND ts <= ?"
        " AND energy_kwh_import IS NOT NULL"
        " ORDER BY ts",
        (device_id, float(t0), float(t1)),
    ).fetchall()
    seq: list[tuple[float, float]] = []
    if prev is not None:
        seq.append((float(prev[0]), float(prev[1])))
    seq.extend((float(r[0]), float(r[1])) for r in rows)
    acc = energy_deltas(seq, bucket_s)
    pts = [
        (int(round(b * 1000)), delta, 1)
        for b, delta in sorted(acc.items())
        if float(t0) <= b <= float(t1) and delta > 0.0
    ]
    return {"energy_kwh_import": pts} if pts else {}


def load_ce_series(
    conn: sqlite3.Connection,
    device_id: str,
    t0: float,
    t1: float,
    bucket_s: float,
    kind: str,
) -> dict[str, list[tuple[int, float, int]]]:
    """`(ts_ms, sum, n)` per field. Avg series use sum/n; delta series use sum.

    Hour and day charts read `ce_roll` once the backfill has caught up.
    Until then, and for sub-hour bars, the window is aggregated from raw.
    """
    grain = _grain_for(bucket_s)
    if grain is not None and not _done(conn):
        try:
            progress_backfill(conn, HISTORY_CHUNK)
        except sqlite3.Error:
            pass
    if grain is not None and _done(conn):
        return _read_rollup(conn, device_id, t0, t1, grain, kind)
    if kind == "delta":
        return _read_energy_raw(conn, device_id, t0, t1, bucket_s)
    return _read_power_raw(conn, device_id, t0, t1, bucket_s)


def merge_ce_series(
    parts: list[dict[str, list[tuple[int, float, int]]]],
    fields: list[str],
    labels: dict[str, str],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    """Combine archives. Power sums add (then divide by the total count);
    energy deltas add. Same bucket from two files is one point, not two.
    """
    acc: dict[str, dict[int, tuple[float, int]]] = {}
    for part in parts:
        for field, pts in part.items():
            slot = acc.setdefault(field, {})
            for ts_ms, total, count in pts:
                prev = slot.get(ts_ms)
                if prev is None:
                    slot[ts_ms] = (float(total), int(count))
                else:
                    slot[ts_ms] = (prev[0] + float(total), prev[1] + int(count))
    series: list[dict[str, Any]] = []
    for field in fields:
        slot = acc.get(field)
        if not slot:
            continue
        points: list[list[Any]] = []
        for ts_ms in sorted(slot):
            total, count = slot[ts_ms]
            if count <= 0:
                continue
            value = total if kind == "delta" else total / count
            points.append([ts_ms, value])
        if points:
            series.append({
                "field": field,
                "label": labels.get(field, field),
                "points": points,
            })
    return series


def roll_is_ready(conn: sqlite3.Connection) -> bool:
    return _done(conn)


def backfill_until_done(
    conn: sqlite3.Connection, *, max_rows: int = 5_000_000
) -> int:
    """Fold the rest of the archive. `max_rows` is the hard stop."""
    folded = 0
    while folded < max_rows and not _done(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = backfill_chunk(conn, min(HISTORY_CHUNK, max_rows - folded))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if n <= 0:
            break
        folded += n
    return folded
