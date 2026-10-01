"""MTD262-MB archive: illuminance, target distance, presence.

Presence is stored 0/1. Lux and metres are the bridge's published values
(scale already applied) and must not be scaled again.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from sa02m_devices.device_history_db import (
    HISTORY_GROUPS,
    METRICS,
    history,
    insert_mtd_sample,
)


_ID = "mtdx62-mb-COM3-20"


def test_mtd_metrics_are_registered():
    assert HISTORY_GROUPS["mtd"] == [
        "mtd_illuminance",
        "mtd_target_distance",
        "mtd_presence",
    ]
    assert METRICS["mtd_illuminance"]["unit"] == "lux"
    assert METRICS["mtd_illuminance"]["decimals"] == 1
    assert METRICS["mtd_illuminance"]["table"] == "mtd_samples"
    assert METRICS["mtd_target_distance"]["unit"] == "m"
    assert METRICS["mtd_target_distance"]["decimals"] == 2
    assert METRICS["mtd_presence"]["decimals"] == 0
    assert METRICS["mtd_presence"]["agg"] == "max"
    assert "presence" in HISTORY_GROUPS["climate"]


def test_insert_keeps_published_units_and_presence_bit(tmp_path: Path):
    db = tmp_path / "devices_history.db"
    now = time.time()
    insert_mtd_sample(
        {
            "ts": now - 5,
            "mtd": [{
                "id": _ID,
                "kind": "mtd",
                "presence": 1,
                "illuminance_lux": 346.5,
                "target_distance_m": 1.42,
            }],
        },
        path=db,
    )
    insert_mtd_sample(
        {
            "ts": now - 4,
            "mtd": [{
                "id": _ID,
                "presence": 0,
                "illuminance_lux": 10.0,
                "target_distance_m": 0.0,
            }],
        },
        path=db,
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT illuminance_lux, target_distance_m, presence"
            " FROM mtd_samples WHERE device_id = ? ORDER BY ts",
            (_ID,),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(346.5, 1.42, 1.0), (10.0, 0.0, 0.0)]

    lux = history(
        "mtd_illuminance", "1h", path=db, device_id=_ID, bucket_s=1.0
    )
    dist = history(
        "mtd_target_distance", "1h", path=db, device_id=_ID, bucket_s=1.0
    )
    pres = history(
        "mtd_presence", "1h", path=db, device_id=_ID, bucket_s=1.0
    )
    assert lux["ok"] and lux["unit"] == "lux"
    assert [p[1] for p in lux["series"][0]["points"]] == [346.5, 10.0]
    assert [p[1] for p in dist["series"][0]["points"]] == [1.42, 0.0]
    assert [p[1] for p in pres["series"][0]["points"]] == [1.0, 0.0]
