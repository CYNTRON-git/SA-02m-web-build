"""MR overview queries must read the selected window, not the device's lifetime."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import sa02m_devices.history_mr as mr
from sa02m_devices.history_export import _fmt_export_ts
from sa02m_devices.history_store import _connect, ensure_schema


DEVICE = "mr02m-COM4-12"
T0 = 1_080_000.0
T1 = T0 + 120.0


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "history.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE mr_samples (
                ts REAL NOT NULL,
                device_id TEXT NOT NULL DEFAULT '',
                ch INTEGER NOT NULL,
                value REAL,
                unit TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (ts, device_id, ch)
            );
            CREATE INDEX idx_mr_device_ch_ts ON mr_samples(device_id, ch, ts);
        """)
        conn.executemany("INSERT INTO mr_samples VALUES (?, ?, ?, ?, ?)", (
            (float(t), DEVICE, ch, float(t + ch), "old")
            for t in range(12_000) for ch in range(1, 5)
        ))
        recent = [
            (T0, DEVICE, 1, 10.0, "°C"),
            (T0 + 10, DEVICE, 1, 20.0, "°C"),
            (T0 + 60, DEVICE, 1, 30.0, "V"),
            (T1, DEVICE, 1, 40.0, "V"),
            (T0, DEVICE, 2, 1.0, "V"),
            (T0 + 10, DEVICE, 2, 3.0, "V"),
            (T0 + 60, DEVICE, 2, 5.0, "V"),
            (T1, DEVICE, 2, None, "mA"),
            (T0 + 30, DEVICE, 3, None, "no points"),
            (T0 - 1, DEVICE, 1, 999.0, "before window"),
            (T1 + 1, DEVICE, 1, 999.0, "after window"),
            (T0, "mr02m-COM4-13", 1, 999.0, "other device"),
        ]
        conn.executemany("INSERT INTO mr_samples VALUES (?, ?, ?, ?, ?)", recent)
    return path


def expected_series(bucket: float, channel: int | None = None) -> list[dict]:
    values = (
        [(0, 10.0), (10, 20.0), (60, 30.0), (120, 40.0)],
        [(0, 1.0), (10, 3.0), (60, 5.0)],
    ) if bucket <= 1 else (
        [(0, 15.0), (60, 30.0), (120, 40.0)],
        [(0, 2.0), (60, 5.0)],
    )
    return [
        {"field": f"ai_{ch}", "label": f"AI {ch}", "unit": unit, "unit_ts": T1,
         "points": [[int((T0 + offset) * 1000), value] for offset, value in points]}
        for ch, unit, points in zip((1, 2), ("V", "mA"), values)
        if channel is None or channel == ch
    ]


def measured_query(path: Path, bucket: float, channel: int | None = None):
    conn = _connect(path)
    statements = []
    work = 0

    def progress():
        nonlocal work
        work += 100
        return 0

    # Exclude one-time schema construction from the steady-state read budget.
    conn.set_trace_callback(statements.append)
    conn.set_progress_handler(progress, 100)
    try:
        result = mr._query_series_mr(conn, T0, T1, bucket, DEVICE, channel)
        conn.set_progress_handler(None, 0)
        conn.set_trace_callback(None)
        plans = [(sql, [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)])
                 for sql in statements]
        return result, plans, work
    finally:
        conn.close()


@pytest.mark.parametrize("bucket", [1.0, 60.0], ids=["raw", "aggregated"])
def test_overview_bounds_series_and_latest_units_to_time_window(legacy_db, bucket):
    result, plans, work = measured_query(legacy_db, bucket)
    # Includes both endpoints, excludes other devices and out-of-window rows,
    # and preserves a unit changed by the latest NULL-valued sample.
    assert result == expected_series(bucket)
    assert len(plans) == 2, plans
    assert "MAX(ts)" in plans[1][0], plans
    for sql, details in plans:
        searches = [row for row in details if "SEARCH mr_samples" in row]
        assert len(searches) == 1, (sql, details)
        assert "device_id=?" in searches[0], (sql, details)
        assert "ts>?" in searches[0] and "ts<?" in searches[0], (sql, details)
    # A few recent rows need far less work than scanning 48,000 old rows.
    assert work < 20_000, (work, plans)


@pytest.mark.parametrize("bucket", [1.0, 60.0], ids=["raw", "aggregated"])
def test_single_channel_keeps_channel_and_time_range_access(legacy_db, bucket):
    result, plans, work = measured_query(legacy_db, bucket, 2)
    assert result == expected_series(bucket, 2)
    assert len(plans) == 2, plans
    for sql, details in plans:
        searches = [row for row in details if "SEARCH mr_samples" in row]
        assert len(searches) == 1, (sql, details)
        assert "idx_mr_device_ch_ts" in searches[0], (sql, details)
        assert "device_id=? AND ch=? AND ts>? AND ts<?" in searches[0], (sql, details)
    assert work < 20_000, (work, plans)


def test_overview_export_preserves_values_units_order_and_gaps(legacy_db, monkeypatch):
    monkeypatch.setattr(mr, "resolve_time_range", lambda _key: (T0, T1, 5.0))
    table = mr.collect_export_table_mr("1h", device_id=DEVICE, path=legacy_db)
    assert table == {
        "ok": True,
        "error": "",
        "headers": ["Время", "AI 1, V", "AI 2, mA"],
        "rows": [
            [_fmt_export_ts(T0, 60.0), 15.0, 2.0],
            [_fmt_export_ts(T0 + 60, 60.0), 30.0, 5.0],
            [_fmt_export_ts(T1, 60.0), 40.0, None],
        ],
        "device_id": DEVICE,
        "range": "1h",
        "bucket_s": 60.0,
        "t0": T0,
        "t1": T1,
        "metric_ids": ["ai_1", "ai_2"],
        "kind": "mr",
        "title": "MR-02m AI",
    }


def test_existing_database_index_upgrade_is_idempotent_and_preserves_rows(legacy_db):
    with closing(sqlite3.connect(legacy_db)) as conn:
        before = conn.execute("SELECT * FROM mr_samples ORDER BY ts, device_id, ch").fetchall()
    rootpages = []
    for _ in range(2):
        assert ensure_schema(legacy_db) == legacy_db
        with closing(sqlite3.connect(legacy_db)) as conn:
            assert conn.execute("SELECT * FROM mr_samples ORDER BY ts, device_id, ch").fetchall() == before
            for name, columns in (
                ("idx_mr_device_ch_ts", ["device_id", "ch", "ts"]),
                ("idx_mr_device_ts_ch", ["device_id", "ts", "ch"]),
            ):
                actual = [row[2] for row in conn.execute(f"PRAGMA index_info({name})")]
                assert actual == columns
            rootpages.append(conn.execute(
                "SELECT name, rootpage FROM sqlite_master WHERE type='index' ORDER BY name"
            ).fetchall())
    assert rootpages[0] == rootpages[1]
