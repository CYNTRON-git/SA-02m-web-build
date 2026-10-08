"""СЭ energy keeps Wh resolution from the MQTT cache to the hourly ΔE bar.

The defect (backlog 2026-10-07): `_build_ce` rounded `energy_active_import/1000`
to ONE decimal, and the chart path rounded the served ΔE to the metric's
`decimals` (1) again. A ~32 W load adds ≈0.03 kWh an hour, so every hourly
`delta` bucket read 0 until the register crossed a 0.1 kWh step.

RED (before the fix): the snapshot yields 1234.6 for 1234567, and two samples
30 Wh apart give an hourly ΔE of 0 (or no bucket at all).
GREEN: 1234.567 in the snapshot and the archive, 0.03 in the hourly bucket;
the summary and the export keep their shape.

This pins RESOLUTION only: the unit is the Wh the bridge publishes (one home,
docs/contracts/ce-energy-mqtt.md), stored as is at three decimals of kWh.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from sa02m_devices.device_history_db import (
    collect_export_table,
    history,
    insert_sample,
    period_summary_ce,
)
from sa02m_devices.stand_devices import live_snapshot


_CE = "ce02m3-COM2-14"


def _snapshot(cache: Path, energy_raw: str) -> dict:
    (cache / f"{_CE}.json").write_text(
        json.dumps({
            "ok": True,
            "device": _CE,
            "ts": time.time(),
            "controls": {
                "voltage_a": "230.0",
                "power_total": "32",
                "energy_active_import": energy_raw,
            },
        }),
        encoding="utf-8",
    )
    return live_snapshot(cache)


def _seed_two_samples(tmp_path: Path) -> tuple[Path, float]:
    """Two snapshots 30 Wh apart, 60 s apart, at the end of the window."""
    cache = tmp_path / "mqtt"
    cache.mkdir()
    db = tmp_path / "hist.db"
    now = time.time()
    for offset, raw in ((90.0, "1234567"), (30.0, "1234597")):
        snap = _snapshot(cache, raw)
        snap["ts"] = now - offset
        insert_sample(snap, path=db)
    return db, now


def test_snapshot_keeps_watt_hour_resolution(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    snap = _snapshot(cache, "1234567")
    assert snap["ce"][0]["energy_kwh_import"] == 1234.567


def test_archive_row_keeps_watt_hour_resolution(tmp_path: Path):
    db, _now = _seed_two_samples(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        rows = [
            r[0]
            for r in conn.execute(
                "SELECT energy_kwh_import FROM ce_samples ORDER BY ts"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert rows == [1234.567, 1234.597]


def test_hourly_delta_of_a_small_load_is_not_zero(tmp_path: Path):
    db, _now = _seed_two_samples(tmp_path)
    energy = history("energy_kwh_import", "w:86400", path=db, device_id=_CE)
    assert energy["ok"] is True
    assert energy["value_kind"] == "delta" and energy["bucket_s"] == 3600.0
    values = [p[1] for s in energy["series"] for p in s["points"]]
    assert values == [0.03], f"hourly ΔE {values!r}; want [0.03]"


def test_summary_and_export_keep_their_shape(tmp_path: Path):
    db, _now = _seed_two_samples(tmp_path)
    summary = period_summary_ce("w:86400", path=db, device_id=_CE, kwh_rub=10.0)
    e = summary["energy_kwh_import"]
    assert set(e) == {"first", "last", "delta", "unit"} and e["unit"] == "kWh"
    assert e["first"] == 1234.567 and e["last"] == 1234.597
    assert abs(e["delta"] - 0.03) < 1e-9
    assert summary["cost_rub"] == 0.3
    table = collect_export_table(
        "w:86400", metric_id="energy_kwh_import", device_id=_CE, path=db
    )
    assert table["ok"] is True and len(table["headers"]) == 2
    assert table["rows"] and table["rows"][-1][1] == 1234.597
