"""The Carel archive as a WIDE row: pivot migration, writer, one-home columns.

Plan 1.0.6.41 B2. Every DB here is built by the OLD long writer's DDL
(`ts, device_id, metric, value, unit`) — the shape a deployed 1.0.6.40 board
left behind — so the migration is exercised against the real predecessor and
not against a table this branch created. The read-side shape is pinned by
`test_history_characterization.py` §7 (the golden dicts) and stays untouched.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from sa02m_devices import history_store
from sa02m_devices.device_history_db import (
    CAREL_COLUMNS,
    CAREL_METRIC_META,
    HISTORY_GROUPS,
    METRICS,
    ensure_schema,
    history_carel,
    insert_carel_sample,
    purge_old,
)

_DEV = "carel-COM3-1"
_DEV2 = "carel-COM3-2"

# The 1.0.6.40 schema, verbatim (git show 1.0.6.40:…/history_store.py).
_LONG_DDL = """
    CREATE TABLE IF NOT EXISTS carel_samples (
        ts REAL NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        metric TEXT NOT NULL,
        value REAL,
        unit TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (ts, device_id, metric)
    );
    CREATE INDEX IF NOT EXISTS idx_carel_device_metric_ts
        ON carel_samples(device_id, metric, ts);
"""

# One tick as the old writer wrote it: a row per metric that had a value.
# `room_temp`/`outdoor_temp` are the unfitted probes — no row at all.
def _long_tick(alarm: float, plant: float, supply: float = 26.5) -> dict[str, float]:
    return {
        "supply_temp": supply, "return_water_temp": 75.1, "setpoint": 27.2,
        "heat_valve": 12.0, "fan_supply": 45.0,
        "alarm": alarm, "alarm_count": 2.0 if alarm else 0.0,
        "plant_state": plant, "unit_on": 1.0,
    }


def _write_long(db: Path, ticks: list[tuple[float, dict[str, float]]],
                device_id: str = _DEV) -> int:
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_LONG_DDL)
        n = 0
        with conn:
            for ts, values in ticks:
                for metric, val in values.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO carel_samples"
                        "(ts, device_id, metric, value, unit) VALUES (?,?,?,?,?)",
                        (ts, device_id, metric, val, CAREL_METRIC_META[metric][1]),
                    )
                    n += 1
        return n
    finally:
        conn.close()


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _names(db: Path, kind: str) -> set[str]:
    return {
        r[0] for r in _rows(
            db, "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
        )
    }


# ── the pivot ────────────────────────────────────────────────────────


def test_a_long_table_is_pivoted_to_one_row_per_tick_on_first_open(tmp_path: Path):
    db = tmp_path / "hist.db"
    base = float(int(time.time()) // 60 * 60) - 300
    long_rows = _write_long(db, [
        (base, _long_tick(0.0, 1.0)),
        (base + 10, _long_tick(1.0, 2.0)),
        (base + 20, _long_tick(0.0, 1.0)),
    ])
    # A second device sharing one of those timestamps → its own wide row.
    long_rows += _write_long(db, [(base, _long_tick(0.0, 0.0, supply=19.0))], _DEV2)

    ensure_schema(db)

    wide = _rows(
        db,
        "SELECT ts, device_id, supply_temp, alarm, alarm_count, plant_state,"
        " unit_on, room_temp FROM carel_samples ORDER BY device_id, ts",
    )
    assert wide == [
        (base, _DEV, 26.5, 0.0, 0.0, 1.0, 1.0, None),
        (base + 10, _DEV, 26.5, 1.0, 2.0, 2.0, 1.0, None),
        (base + 20, _DEV, 26.5, 0.0, 0.0, 1.0, 1.0, None),
        (base, _DEV2, 19.0, 0.0, 0.0, 0.0, 1.0, None),
    ]
    # The long table survives as the one-release rollback (fork F4), with its
    # every row, and the metric index it no longer needs is gone.
    assert _rows(db, "SELECT count(*) FROM carel_samples_v1")[0][0] == long_rows
    assert "idx_carel_device_metric_ts" not in _names(db, "index")
    assert "idx_carel_device_ts" in _names(db, "index")
    assert "carel_samples__wide" not in _names(db, "table")

    # Second open: the wide table has no `metric` column, so nothing runs again
    # — the rollback table is NOT overwritten by the pivot's own output.
    ensure_schema(db)
    assert _rows(db, "SELECT count(*) FROM carel_samples_v1")[0][0] == long_rows
    assert _rows(db, "SELECT count(*) FROM carel_samples")[0][0] == 4


def test_a_failing_pivot_rolls_back_and_leaves_the_long_table_intact(tmp_path: Path):
    """`executescript` runs DDL in autocommit unless the SCRIPT opens the
    transaction (the legacy PK migration is unprotected this way). A failure
    after the wide table is filled must leave NO half-migrated file behind."""
    db = tmp_path / "hist.db"
    base = time.time() - 100
    long_rows = _write_long(db, [(base, _long_tick(1.0, 2.0))])
    # An object already occupying the rollback name fails the rename step —
    # after the wide table has been created and populated.
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript("CREATE VIEW carel_samples_v1 AS SELECT 1;")
    finally:
        conn.close()

    with pytest.raises(sqlite3.Error):
        ensure_schema(db)

    assert "carel_samples__wide" not in _names(db, "table")
    assert {"metric", "value", "unit"} <= {
        str(r[1]) for r in _rows(db, "PRAGMA table_info(carel_samples)")
    }
    assert _rows(db, "SELECT count(*) FROM carel_samples")[0][0] == long_rows


def test_the_opener_that_loses_the_pivot_race_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two openers, one long archive: the loser must find the work done, not crash.

    `_migrate_carel_to_wide` decides (`_carel_is_long`) and acts
    (`executescript`) in two steps, and EVERY caller opens its own connection —
    the logger tick, an archive read, the eMMC staging. A second opener that
    evaluated the check before the winner committed proceeds into the script,
    blocks on its `BEGIN IMMEDIATE`, takes the write lock AFTER the schema
    changed, and its `SELECT … WHERE metric = …` then reads a table that has no
    `metric` column any more (`no such column: metric`). That is the ordinary
    shape on a board, not a corner: on the bench 1.135 archive the window is
    11,5 s wide (`docs/contracts/carel-ahu.md` §переход).

    The interleaving is made deterministic by running the winner INSIDE the
    loser's own check — the answer the loser then carries into the script is the
    real one it computed on the still-long archive, not a fabricated flag.
    Everything else is shipped code: two connections, the shipped SQL, real
    SQLite.
    """
    db = tmp_path / "race.db"
    base = float(int(time.time()) // 60 * 60) - 300
    long_rows = _write_long(db, [
        (base, _long_tick(0.0, 1.0)),
        (base + 10.0, _long_tick(1.0, 2.0)),
    ])

    real_is_long = history_store._carel_is_long
    winner_ran: list[bool] = []

    def check_then_let_the_winner_commit(conn: sqlite3.Connection) -> bool:
        answer = real_is_long(conn)
        if answer and not winner_ran:
            # Appended BEFORE the nested open, so the winner's own check takes
            # the plain path and this stays one level deep.
            winner_ran.append(True)
            ensure_schema(db)  # opener A migrates and COMMITs
        return answer  # opener B carries the answer it got on the long archive

    monkeypatch.setattr(
        history_store, "_carel_is_long", check_then_let_the_winner_commit
    )

    ensure_schema(db)  # opener B — the loser; must not raise

    assert winner_ran, "the winner never ran — the race was not staged"
    cols = {str(r[1]) for r in _rows(db, "PRAGMA table_info(carel_samples)")}
    assert "metric" not in cols
    # Exactly ONE pivot happened: the winner's. The loser neither re-pivoted the
    # wide table nor clobbered the rollback copy.
    assert _rows(db, "SELECT count(*) FROM carel_samples")[0][0] == 2
    assert _rows(db, "SELECT count(*) FROM carel_samples_v1")[0][0] == long_rows
    assert "carel_samples__wide" not in _names(db, "table")

    # The loser's connection is left usable — the logger tick that lost the race
    # goes on writing into the wide table instead of dying on it.
    monkeypatch.setattr(history_store, "_carel_is_long", real_is_long)
    insert_carel_sample(_snap(base + 20.0, alarm=0, plant_state="run"), path=db)
    assert _rows(db, "SELECT count(*) FROM carel_samples")[0][0] == 3


def _pivot_statements(db: Path) -> tuple[int, float]:
    """Run the pivot on `db` and return (SQL statements executed, seconds).

    The statement COUNT is what makes the cost shape assertable on any host: a
    bulk pivot runs a fixed script whatever the archive holds, a per-row Python
    loop runs one statement per tick. Wall-clock on a dev box cannot tell the
    two apart (measured 2026-09-09: 1 µs/row either way on the 75 600-row
    fixture below), which is why the retired «≤ 10 s» assertion was decoration.
    """
    conn = sqlite3.connect(str(db))
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        t0 = time.monotonic()
        history_store._migrate_carel_to_wide(conn)
        elapsed = time.monotonic() - t0
    finally:
        conn.set_trace_callback(None)
        try:
            conn.close()
        except sqlite3.ProgrammingError:  # a failed pivot closes it itself
            pass
    return len(seen), elapsed


def test_the_pivot_is_one_bulk_pass_whose_cost_does_not_scale_per_row(
    tmp_path: Path,
):
    """The pivot's COST SHAPE — a fixed script, not a per-row loop.

    This is deliberately NOT the bench criterion. What was measured on the board
    (bench 1.135, 2026-09-09, the real archive; one home:
    `docs/contracts/carel-ahu.md` §переход): 504 903 long rows, 11,5 s,
    ~23 µs/row, linear — so the plan's «≤ 10 s» criterion was MISSED there, and
    the wall that matters is the API's `sqlite3.connect(timeout=30)`, which the
    same rate crosses at ~1,3 млн rows. A synthetic fixture on a dev host
    measures none of that: this one is 75 600 long rows against the bench's
    504 903, and its row count is not preserved by the pivot in any case — it is
    a pivot (75 600 long rows in, 8 400 wide rows out).

    So the wall-clock number here is REPORT-ONLY (printed beside the bench rate)
    and the assertion is the shape instead: the same fixed number of SQL
    statements on a 180-row archive and on a 75 600-row one. That fails RED on
    the regression the retired bound only claimed to catch.
    """
    base = time.time() - 30 * 86400
    small = tmp_path / "small.db"
    _write_long(small, [(base + i * 10.0, _long_tick(0.0, 1.0)) for i in range(20)])
    big = tmp_path / "big.db"
    ticks = [(base + i * 10.0, _long_tick(float(i % 2), 1.0)) for i in range(8_400)]
    long_rows = _write_long(big, ticks)  # 8400 × 9 = 75 600 long rows

    n_small, _ = _pivot_statements(small)
    n_big, elapsed = _pivot_statements(big)

    # Non-vacuity: both archives really were pivoted, so the counts are counts
    # of a pivot that ran — not of an early return.
    for db in (small, big):
        cols = {str(r[1]) for r in _rows(db, "PRAGMA table_info(carel_samples)")}
        assert "metric" not in cols and "supply_temp" in cols
    assert 0 < n_small < 25, f"{n_small} statements is not a fixed script"

    assert n_big == n_small, (
        f"the pivot ran {n_big} statements on 75 600 rows against {n_small} on "
        f"180 — the cost is per-row, not one bulk pass"
    )
    # Lossless at scale, and the rollback copy keeps every long row.
    assert _rows(big, "SELECT count(*) FROM carel_samples")[0][0] == len(ticks)
    assert _rows(big, "SELECT count(*) FROM carel_samples_v1")[0][0] == long_rows
    print(
        f"pivot cost (report-only, this host): {long_rows} long rows in "
        f"{elapsed:.2f}s = {elapsed / long_rows * 1e6:.0f} µs/row in "
        f"{n_big} statements; bench 1.135 measured 23 µs/row"
    )


# ── one home for the column list ─────────────────────────────────────


def test_the_carel_column_list_has_one_home(tmp_path: Path):
    """`CAREL_COLUMNS` (derived from the `ahu_` METRICS group) is what the DDL,
    the writer and the pivot all read — a metric added to METRICS reaches the
    table without a second edit, and cannot drift out of the archive."""
    assert len(CAREL_COLUMNS) == 13
    assert list(CAREL_METRIC_META) == list(CAREL_COLUMNS)
    assert HISTORY_GROUPS["ahu"] == [f"ahu_{c}" for c in CAREL_COLUMNS]
    for col in CAREL_COLUMNS:
        meta = METRICS[f"ahu_{col}"]
        assert meta["table"] == "carel_samples" and meta["fields"] == [col]
        assert meta["device"] == "carel"
        assert (meta["label"], meta["unit"], meta["decimals"]) == CAREL_METRIC_META[col]

    db = tmp_path / "fresh.db"
    ensure_schema(db)
    cols = [str(r[1]) for r in _rows(db, "PRAGMA table_info(carel_samples)")]
    assert cols == ["ts", "device_id", *CAREL_COLUMNS]


# ── the writer ───────────────────────────────────────────────────────


def _snap(ts: float, **over) -> dict:
    dev = {"id": _DEV, "kind": "carel", "supply_temp": 26.5, "room_temp": None}
    dev.update(over)
    return {"ts": ts, "carel": [dev]}


def test_one_row_per_tick_and_no_row_when_every_probe_is_absent(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    insert_carel_sample(_snap(now - 30, alarm=0, plant_state="run"), path=db)
    insert_carel_sample(_snap(now - 20, alarm=1, plant_state="alarm"), path=db)
    assert _rows(db, "SELECT count(*) FROM carel_samples")[0][0] == 2

    # Every metric absent (a controller unread this tick) writes NO row, as the
    # long writer wrote no rows; an unknown plant word still writes none of ITS.
    insert_carel_sample({"ts": now - 10, "carel": [{"id": _DEV, "kind": "carel"}]},
                        path=db)
    assert _rows(db, "SELECT count(*) FROM carel_samples")[0][0] == 2
    insert_carel_sample(_snap(now - 5, plant_state="???"), path=db)
    row = _rows(
        db, "SELECT supply_temp, plant_state FROM carel_samples ORDER BY ts DESC"
    )[0]
    assert row == (26.5, None)

    # purge counts TICKS now, not cells.
    insert_carel_sample(_snap(now - 40 * 86400, alarm=0), path=db)
    assert purge_old(path=db, now=now)["carel_deleted"] == 1


# ── the events seed, over the migrated table ─────────────────────────


def test_the_event_seed_reads_the_last_non_null_column_after_a_pivot(tmp_path: Path):
    """A restart on a board whose archive is still long: the pivot runs on the
    detector's own open, and the baseline is the last NON-NULL alarm/state —
    the latest row carries neither (probes unread that tick)."""
    from sa02m_devices.device_events import (
        detect_carel_events,
        reset_carel_event_state,
    )

    db = tmp_path / "hist.db"
    base = time.time() - 120
    _write_long(db, [(base, _long_tick(1.0, 2.0))])
    # A later tick with only continuous values — no alarm / plant_state rows.
    _write_long(db, [(base + 10, {"supply_temp": 26.5})])

    reset_carel_event_state()
    created = detect_carel_events(
        {"ts": base + 20, "carel": [
            {"id": _DEV, "kind": "carel", "alarm": 0, "plant_state": "run"},
        ]},
        path=db,
    )
    assert [e["kind"] for e in created] == ["carel_alarm_off", "carel_plant_state"]
    assert "carel_samples_v1" in _names(db, "table")


# ── the eMMC → USB promote (plan B2, RED before `ensure_schema(src)`) ─


def test_promote_merges_a_long_staging_into_a_wide_destination(tmp_path: Path):
    """`_merge_table` copies the INTERSECTION of the two column sets. A long
    staging against a wide destination intersects on `{ts, device_id}` only —
    without migrating the source first the promote lands value-less rows and
    the archive it was meant to rescue is silently empty."""
    from sa02m_devices.device_history_migrate import merge_db_into

    src = tmp_path / "staging.db"
    dst = tmp_path / "active.db"
    base = float(int(time.time()) // 60 * 60) - 300
    _write_long(src, [
        (base, _long_tick(0.0, 1.0, supply=21.0)),
        (base + 10, _long_tick(1.0, 2.0, supply=22.0)),
    ])
    insert_carel_sample(
        {"ts": base + 20, "carel": [{"id": _DEV, "kind": "carel",
                                     "supply_temp": 23.0, "alarm": 0,
                                     "plant_state": "run"}]},
        path=dst,
    )

    report = merge_db_into(src, dst)
    assert report["ok"] and report["carel_merged"] == 2

    h = history_carel(_DEV, "1h", metric="supply_temp", path=dst, bucket_s=1.0)
    assert [p[1] for p in h["series"][0]["points"]] == [21.0, 22.0, 23.0]
    alarm = history_carel(_DEV, "1h", metric="alarm", path=dst, bucket_s=1.0)
    assert [p[1] for p in alarm["series"][0]["points"]] == [0.0, 1.0, 0.0]
