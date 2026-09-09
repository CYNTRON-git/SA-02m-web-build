"""The METRICS engine generalised for the Carel wide table (plan 1.0.6.41 B1).

Three engine facts the wide-table migration (B2) needs, each RED on the split
tree before B1 and GREEN after: a METRICS entry may carry its own bucket
aggregate (`agg: "max"` — a state flag averaged over a bucket loses its edge);
`collect_export_table` derives `kind` from the metric set's `device` and titles
the `ahu` group; `history_batch` threads `bucket_s` through to `history()`.
The DTV/CE behaviour is pinned unchanged alongside (explicit `agg` still wins,
energy export keeps its explicit max, kind stays dtv/ce/mixed).
"""

from __future__ import annotations

import time
from pathlib import Path

from sa02m_devices.device_history_db import (
    HISTORY_GROUPS,
    METRICS,
    collect_export_table,
    history,
    history_batch,
    insert_sample,
)

_CE = "ce02m3-COM2-14"


def _ce_snap(ts: float, ua: float, e: float = 1.0) -> dict:
    return {"ts": ts, "dtv": [], "ce": [{
        "ok": True, "id": _CE,
        "voltage": {"a": ua, "b": ua, "c": ua},
        "current": {"a": 0.1, "b": 0.1, "c": 0.1},
        "power_w": {"a": 5.0, "b": 7.0, "c": 8.0, "total": 20.0},
        "frequency_hz": 50.0, "energy_kwh_import": e,
    }]}


def _seed(db: Path) -> float:
    """Three samples inside ONE 60 s bucket: Ua 220 / 240 / 230, E 1.0 / 1.2 / 1.1."""
    base = float(int(time.time()) // 60 * 60) - 300
    for i, (ua, e) in enumerate(((220.0, 1.0), (240.0, 1.2), (230.0, 1.1))):
        insert_sample(_ce_snap(base + i * 10, ua, e), path=db)
    return base


def _max_entry(**over) -> dict:
    return {
        "table": "ce_samples", "fields": ["voltage_a"], "labels": {"voltage_a": "Ua"},
        "label": "Ua max", "unit": "V", "device": "ce", "decimals": 1, "agg": "max",
        **over,
    }


def test_metrics_entry_agg_drives_the_bucket_aggregate(tmp_path: Path, monkeypatch):
    db = tmp_path / "hist.db"
    _seed(db)
    monkeypatch.setitem(METRICS, "test_ua_max", _max_entry())
    h = history("test_ua_max", "1h", path=db, device_id=_CE, bucket_s=60.0)
    assert h["ok"] and [p[1] for p in h["series"][0]["points"]] == [240.0]
    # An entry without `agg` keeps averaging (the DTV/CE default is untouched).
    avg = history("voltage", "1h", path=db, device_id=_CE, bucket_s=60.0)
    ua = [s for s in avg["series"] if s["field"] == "voltage_a"][0]["points"]
    assert [p[1] for p in ua] == [230.0]


def test_explicit_agg_argument_still_wins_over_the_entry(tmp_path: Path, monkeypatch):
    db = tmp_path / "hist.db"
    _seed(db)
    monkeypatch.setitem(METRICS, "test_ua_max", _max_entry())
    h = history("test_ua_max", "1h", path=db, device_id=_CE, bucket_s=60.0, agg="avg")
    assert [p[1] for p in h["series"][0]["points"]] == [230.0]
    e = history("energy_kwh_import", "1h", path=db, device_id=_CE, bucket_s=60.0, agg="max")
    assert [p[1] for p in e["series"][0]["points"]] == [1.2]


def test_export_table_kind_from_the_metric_set_device(tmp_path: Path, monkeypatch):
    db = tmp_path / "hist.db"
    _seed(db)
    # A metric whose device is not dtv/ce (the wide Carel table's shape, on a table
    # that exists on this tree) must yield ITS device as the export kind. TWO
    # metrics, because a ONE-metric export is titled by that metric's own label
    # (pinned below) — the group title needs a real group.
    monkeypatch.setitem(METRICS, "test_ahu_x", _max_entry(device="carel", label="X"))
    monkeypatch.setitem(
        METRICS, "test_ahu_y",
        _max_entry(device="carel", label="Y", fields=["voltage_b"],
                   labels={"voltage_b": "Ub"}),
    )
    monkeypatch.setitem(HISTORY_GROUPS, "ahu", ["test_ahu_x", "test_ahu_y"])
    t = collect_export_table("1h", group="ahu", device_id=_CE, path=db)
    assert t["ok"] and t["kind"] == "carel" and t["title"] == "Carel AHU"
    # A single-metric export keeps its metric's label as the title (unchanged).
    one = collect_export_table("1h", metric_id="test_ahu_x", device_id=_CE, path=db)
    assert one["kind"] == "carel" and one["title"] == "X"
    # The entry's own aggregate reaches the export table too (the state group
    # exports its max, like the chart) — 240, not the 230 average.
    assert t["rows"] == [[t["rows"][0][0], 240.0, 240.0]]
    # DTV/CE derivation unchanged: single-device → that device, mixed → mixed, and
    # the energy column keeps its explicit max (1.2) while the chart stays avg.
    assert collect_export_table("1h", group="energy", device_id=_CE, path=db)["kind"] == "ce"
    monkeypatch.setitem(HISTORY_GROUPS, "mixed_test", ["room_temp", "voltage"])
    assert collect_export_table("1h", group="mixed_test", path=db)["kind"] == "mixed"
    energy = collect_export_table("1h", metric_id="energy_kwh_import", device_id=_CE, path=db)
    assert energy["rows"][0][1] == 1.2
    chart = history("energy_kwh_import", "1h", path=db, device_id=_CE, bucket_s=60.0)
    assert [p[1] for p in chart["series"][0]["points"]] == [1.1]


def test_history_batch_threads_bucket_s(tmp_path: Path):
    db = tmp_path / "hist.db"
    _seed(db)
    raw = history_batch("1h", group="energy", path=db, device_id=_CE, bucket_s=1.0)
    ua = [m for m in raw["metrics"] if m["metric"] == "voltage"][0]["series"]
    assert [p[1] for p in ua[0]["points"]] == [220.0, 240.0, 230.0]
    # Default (no bucket_s) keeps the chart bucket — 5 s for "1h", so still 3 points
    # here but through the aggregate path; the two must agree on the values.
    chart = history_batch("1h", group="energy", path=db, device_id=_CE)
    ua2 = [m for m in chart["metrics"] if m["metric"] == "voltage"][0]["series"]
    assert [p[1] for p in ua2[0]["points"]] == [220.0, 240.0, 230.0]
