"""СЭ power/energy charts read pre-aggregated buckets.

A 7-day «Мощность» request used to answer the generic ~400-point window
(`w:604800` → 1800 s buckets). Four phases × ~337 buckets is the «1348 точек»
the chart counted while the browser re-averaged them into a daily bar.

RED on that path: the same samples come back as one point per 1800 s bucket.
GREEN: `history("power"|"energy_kwh_import")` with no explicit `bucket_s`
returns the bar bucket (1 сут for 7 д) from the rollup, and that rollup matches
a raw walk (average watts; ΔE of the cumulative kWh counter).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from sa02m_devices.device_history_db import history, insert_sample
from sa02m_devices.history_ranges import _TZ


_CE = "ce02m3-COM2-14"


def _snap(ts: float, *, p: float, e: float) -> dict:
    return {
        "ts": ts,
        "dtv": [],
        "ce": [{
            "ok": True,
            "id": _CE,
            "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
            "current": {"a": 1.0, "b": 1.0, "c": 1.0},
            "power_w": {"a": p, "b": p, "c": p, "total": p * 3},
            "frequency_hz": 50.0,
            "energy_kwh_import": e,
        }],
    }


def _seed_week(db: Path) -> float:
    """Four samples a day for 7 days, 30 min apart — one 1800 s bucket each
    on the old path (~28 points/series), one civil day on the new path."""
    now = time.time()
    e = 100.0
    for d in range(7):
        for i in range(4):
            ts = now - d * 86400 - i * 1800 - 30
            e += 0.25
            insert_sample(_snap(ts, p=100.0 + d, e=e), path=db)
    return now


def test_7d_power_is_daily_buckets_not_raw_series(tmp_path: Path):
    db = tmp_path / "hist.db"
    _seed_week(db)
    power = history("power", "w:604800", path=db, device_id=_CE)
    assert power["ok"] is True
    assert power.get("prepared") is True
    assert power.get("bucket_s") == 86400.0
    assert power.get("value_kind") == "avg"
    total = [s for s in power["series"] if s["field"] == "power_w_total"][0]
    # Four phases × ~28 half-hour buckets was 100+ points. A week of daily
    # means is a handful of buckets (the rolling window touches at most 8 days).
    assert 1 <= len(total["points"]) <= 8, (
        f"7d power returned {len(total['points'])} points; want daily buckets"
    )
    # Voltage keeps the generic fine series — this change is the СЭ bar chart.
    voltage = history("voltage", "w:604800", path=db, device_id=_CE)
    assert len(voltage["series"][0]["points"]) > len(total["points"])


def test_1h_power_is_five_minute_buckets_not_raw(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(20):
        insert_sample(
            _snap(now - 3000 + i * 150, p=100.0 + (i % 3) * 10, e=10.0 + i * 0.01),
            path=db,
        )
    power = history("power", "w:3600", path=db, device_id=_CE)
    assert power["prepared"] is True and power["bucket_s"] == 300.0
    total = [s for s in power["series"] if s["field"] == "power_w_total"][0]
    assert 1 <= len(total["points"]) <= 12
    energy = history("energy_kwh_import", "w:3600", path=db, device_id=_CE)
    assert energy["value_kind"] == "delta" and energy["bucket_s"] == 300.0
    assert energy["series"] and max(p[1] for p in energy["series"][0]["points"]) < 1.0


def test_energy_deltas_match_the_chart_rules():
    """The rollup step is the devices.js state machine, including a reset
    that must not paint the drop (or the climb back to the old register)
    as consumption."""
    from sa02m_devices.history_ce_roll import bucket_start, ce_chart_bucket_s, energy_deltas

    assert ce_chart_bucket_s("w:3600") == 300.0
    assert ce_chart_bucket_s("w:21600") == 900.0
    assert ce_chart_bucket_s("w:86400") == 3600.0
    assert ce_chart_bucket_s("w:604800") == 86400.0
    assert ce_chart_bucket_s("w:2592000") == 86400.0
    assert ce_chart_bucket_s("7d") == 86400.0
    assert ce_chart_bucket_s("month") == 86400.0
    assert ce_chart_bucket_s("w:120") == 60.0

    base = datetime(2026, 9, 30, 12, 0, tzinfo=_TZ).timestamp()
    minute = 60.0
    mono = energy_deltas(
        [
            (base, 100.0),
            (base + minute, 100.1),
            (base + 2 * minute, 100.3),
            (base + 5 * minute, 100.4),
            (base + 6 * minute, 100.6),
        ],
        300.0,
    )
    assert abs(mono[bucket_start(base, 300.0)] - 0.3) < 1e-9
    assert abs(mono[bucket_start(base + 5 * minute, 300.0)] - 0.3) < 1e-9

    reset = energy_deltas(
        [
            (base, 100.0),
            (base + minute, 100.2),
            (base + 2 * minute, 0.1),
            (base + 3 * minute, 0.3),
            (base + 4 * minute, 0.5),
        ],
        300.0,
    )
    assert abs(reset[bucket_start(base, 300.0)] - 0.6) < 1e-9
    assert all(v >= 0 for v in reset.values())
    assert max(reset.values()) < 1.0  # not the 100 → 0.1 drop

    glitch = energy_deltas(
        [
            (base, 100.0),
            (base + minute, 100.2),
            (base + 2 * minute, 5.0),
            (base + 3 * minute, 100.3),
        ],
        300.0,
    )
    assert abs(glitch[bucket_start(base, 300.0)] - 0.3) < 1e-9


def test_rollup_matches_raw_power_avg_and_energy_delta(tmp_path: Path):
    """Two finished hours. Rollup == the raw walk.

    Power is the mean of instantaneous watts, not ΔE/Δt (0.4 kWh in 30 min
    would be 800 W; the mean here is 200 W per phase, 600 W total). Energy
    is the rise of the cumulative kWh register; the reset is not a giant bar.
    """
    from sa02m_devices.history_ce_roll import bucket_start, energy_deltas

    db = tmp_path / "hist.db"
    this_hour = bucket_start(time.time(), 3600.0)
    h1 = this_hour - 2 * 3600.0
    h2 = this_hour - 3600.0
    samples = [
        (h1 + 600, 100.0, 10.0),
        (h1 + 1200, 300.0, 10.4),
        (h2 + 600, 300.0, 0.1),  # reset
        (h2 + 1200, 100.0, 0.3),
    ]
    for ts, p, e in samples:
        insert_sample(_snap(ts, p=p, e=e), path=db)

    raw_hour = energy_deltas([(ts, e) for ts, _p, e in samples], 3600.0)
    assert abs(raw_hour[h1] - 0.4) < 1e-9
    assert abs(raw_hour[h2] - 0.2) < 1e-9
    raw_day = energy_deltas([(ts, e) for ts, _p, e in samples], 86400.0)
    assert abs(sum(raw_day.values()) - 0.6) < 1e-9

    energy = history("energy_kwh_import", "w:86400", path=db, device_id=_CE)
    assert energy["prepared"] is True
    assert energy["bucket_s"] == 3600.0
    assert energy["value_kind"] == "delta"
    got = {p[0]: p[1] for p in energy["series"][0]["points"]}
    assert abs(got[int(round(h1 * 1000))] - raw_hour[h1]) < 0.051
    assert abs(got[int(round(h2 * 1000))] - raw_hour[h2]) < 0.051
    assert all(v >= 0 for v in got.values())
    assert max(got.values()) < 1.0

    power = history("power", "w:86400", path=db, device_id=_CE)
    assert power["bucket_s"] == 3600.0
    assert power["value_kind"] == "avg"
    series = {s["field"]: {p[0]: p[1] for p in s["points"]} for s in power["series"]}
    h1_ms = int(round(h1 * 1000))
    h2_ms = int(round(h2 * 1000))
    assert series["power_w_a"][h1_ms] == 200.0
    assert series["power_w_a"][h2_ms] == 200.0
    assert series["power_w_total"][h1_ms] == 600.0
    assert series["power_w_total"][h2_ms] == 600.0

    daily = history("power", "7d", path=db, device_id=_CE)
    assert daily["bucket_s"] == 86400.0
    dpa = [s for s in daily["series"] if s["field"] == "power_w_a"][0]["points"]
    assert 1 <= len(dpa) <= 2
    assert all(p[1] == 200.0 for p in dpa)

    daily_e = history("energy_kwh_import", "7d", path=db, device_id=_CE)
    assert daily_e["bucket_s"] == 86400.0
    assert abs(sum(p[1] for p in daily_e["series"][0]["points"]) - 0.6) < 0.051
