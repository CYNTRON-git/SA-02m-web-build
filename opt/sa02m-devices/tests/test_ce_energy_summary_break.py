"""The СЭ period summary survives a counter break inside its window.

`period_summary_ce` reported ΔE as `last − first`. When the register drops
inside the window — a meter counter reset, or the CE energy unit change on a deployed
board (`docs/contracts/ce-energy-mqtt.md` «Разрыв ряда») — that is negative,
clamped to 0, and the summary showed 0 kWh / 0 ₽ for real consumption.

RED (before the fix): 10.0 → 10.5, drop to 0.03, growth to 0.05 gives ΔE 0.
GREEN: 0.52 — the sum of non-negative steps under the same step rule the bars
use (`history_ce_roll._counter_step`), so summary and bars agree.

The bars' own drop rule («падение = сброс, без отрицательного столбца») is
pinned in `test_history_ce_roll.py::test_energy_deltas_match_the_chart_rules`
(server) and `scripts/dev/test-devices-history-charts.mjs` (client); here only
the agreement of the summary with the bars on the same break is pinned.
"""

from __future__ import annotations

import time
from pathlib import Path

from sa02m_devices.device_history_db import history, insert_sample, period_summary_ce


_CE = "ce02m3-COM2-14"


def _snap(ts: float, e: float) -> dict:
    return {
        "ts": ts,
        "dtv": [],
        "ce": [{
            "ok": True,
            "id": _CE,
            "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
            "current": {"a": 0.1, "b": 0.1, "c": 0.1},
            "power_w": {"a": 10.0, "b": 10.0, "c": 10.0, "total": 30.0},
            "frequency_hz": 50.0,
            "energy_kwh_import": e,
        }],
    }


def _seed(db: Path, values: list[float]) -> None:
    """One sample a minute, ending a minute before now."""
    now = time.time()
    n = len(values)
    for i, e in enumerate(values):
        insert_sample(_snap(now - (n - i) * 60.0, e), path=db)


def test_summary_counts_steps_across_a_counter_break(tmp_path: Path):
    db = tmp_path / "hist.db"
    _seed(db, [10.0, 10.5, 0.03, 0.05])
    s = period_summary_ce("w:3600", path=db, device_id=_CE, kwh_rub=10.0)
    e = s["energy_kwh_import"]
    assert e["first"] == 10.0 and e["last"] == 0.05
    assert abs(e["delta"] - 0.52) < 1e-9, f"ΔE {e['delta']!r}; want 0.52"
    assert s["cost_rub"] == 5.2


def test_summary_monotonic_window_keeps_last_minus_first(tmp_path: Path):
    db = tmp_path / "hist.db"
    _seed(db, [10.0, 10.2, 10.25, 10.5])
    e = period_summary_ce("w:3600", path=db, device_id=_CE)["energy_kwh_import"]
    assert abs(e["delta"] - 0.5) < 1e-9


def test_summary_agrees_with_the_bars_on_the_same_break(tmp_path: Path):
    db = tmp_path / "hist.db"
    _seed(db, [10.0, 10.5, 0.03, 0.05])
    bars = history("energy_kwh_import", "w:3600", path=db, device_id=_CE)
    assert bars["value_kind"] == "delta"
    values = [p[1] for ser in bars["series"] for p in ser["points"]]
    assert values and all(v >= 0 for v in values)
    summary = period_summary_ce("w:3600", path=db, device_id=_CE)
    assert abs(sum(values) - summary["energy_kwh_import"]["delta"]) < 1e-6
