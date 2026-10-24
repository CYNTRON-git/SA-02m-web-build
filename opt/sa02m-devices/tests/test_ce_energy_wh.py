"""СЭ-02м-3 energy is archived at the meter's own 1 Wh resolution (Operator 2026-09-30).

The meter reports a uint64 Wh counter (input regs 580-583, docs/MQTT_TOPICS.md);
the archive keeps it in kWh with 3 decimals — exactly Wh. Before, both
live_snapshot and the history read rounded to 0.1 kWh, so a 5 min / 1 h energy
bar at 200-300 W could only read 0 or 0.1. Pinned here:

- the whole write chain (MQTT cache → live_snapshot → logger row) and the
  history read keep Wh;
- an archive mixing old 0.1 kWh rows with new Wh rows gives ΔE = last − first,
  off from the truth by at most the old half-step (0.05 kWh), never negative;
- the export and the «За период» summary keep their format (headers, keys,
  units, number formats) — only the values carry more digits.
"""

from __future__ import annotations

import json
import sqlite3
import time
from io import BytesIO
from pathlib import Path

import pytest

from sa02m_devices.device_history_db import (
    METRICS,
    collect_export_table,
    export_text,
    export_xlsx,
    history,
    insert_sample,
    period_summary_ce,
)
from sa02m_devices.stand_devices import live_snapshot

_CE = "ce02m3-COM2-14"


def _live_ce(tmp_path: Path, wh: int | str) -> dict:
    """live_snapshot() over an MQTT cache holding one СЭ with the given Wh counter."""
    cache = tmp_path / "mqtt"
    cache.mkdir(exist_ok=True)
    (cache / f"{_CE}.json").write_text(
        json.dumps({
            "ok": True,
            "device": _CE,
            "controls": {
                "voltage_a": "230.0", "voltage_b": "230.0", "voltage_c": "230.0",
                "current_a": "0.4", "current_b": "0.4", "current_c": "0.4",
                "power_total": "250",
                "frequency": "50.00",
                "energy_active_import": str(wh),
            },
            "ts": time.time(),
        }),
        encoding="utf-8",
    )
    return live_snapshot(cache)


def _log_new(tmp_path: Path, db: Path, ts: float, wh: int) -> None:
    """One logger tick through the real chain (cache → live_snapshot → insert_sample)."""
    snap = _live_ce(tmp_path, wh)
    snap["ts"] = ts
    insert_sample(snap, path=db)


def _log_old(db: Path, ts: float, wh: int) -> None:
    """A row as the pre-change archive holds it: round(Wh / 1000, 1)."""
    insert_sample({"ts": ts, "dtv": [], "ce": [{
        "ok": True, "id": _CE,
        "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
        "current": {"a": 0.4, "b": 0.4, "c": 0.4},
        "power_w": {"a": None, "b": None, "c": None, "total": 250.0},
        "frequency_hz": 50.0,
        "energy_kwh_import": round(wh / 1000.0, 1),
    }]}, path=db)


def _raw_energy(db: Path) -> list[float]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute(
            "SELECT energy_kwh_import FROM ce_samples ORDER BY ts"
        ).fetchall()]
    finally:
        conn.close()


# ── the write chain keeps Wh ─────────────────────────────────────────


@pytest.mark.parametrize("wh, kwh", [
    ("12345", 12.345),
    ("1500", 1.5),
    ("0", 0.0),
    ("1", 0.001),
    ("1234567891", 1234567.891),
])
def test_live_snapshot_keeps_watt_hours(tmp_path: Path, wh: str, kwh: float):
    ce = _live_ce(tmp_path, wh)["ce"][0]
    assert ce["energy_kwh_import"] == kwh


def test_live_snapshot_absent_counter_stays_none(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    (cache / f"{_CE}.json").write_text(
        json.dumps({"ok": True, "device": _CE, "ts": time.time(),
                    "controls": {"voltage_a": "230.0"}}),
        encoding="utf-8",
    )
    assert live_snapshot(cache)["ce"][0]["energy_kwh_import"] is None


def test_logger_row_keeps_watt_hours(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    _log_new(tmp_path, db, now - 120, 12345)
    _log_new(tmp_path, db, now - 60, 12346)
    assert _raw_energy(db) == [12.345, 12.346]


def test_history_serves_watt_hours(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    _log_new(tmp_path, db, now - 120, 12345)
    _log_new(tmp_path, db, now - 60, 12346)
    h = history("energy_kwh_import", "1h", path=db, device_id=_CE, bucket_s=1.0)
    assert h["ok"] and h["unit"] == "kWh" and h["decimals"] == 3
    assert [p[1] for p in h["series"][0]["points"]] == [12.345, 12.346]
    assert METRICS["energy_kwh_import"]["decimals"] == 3


# ── ΔE / cost ─────────────────────────────────────────────────────────


def test_summary_counts_a_quarter_kwh(tmp_path: Path):
    """250 W for an hour = 250 Wh; at 0.1 kWh the endpoints made it 0.2 or 0.3."""
    db = tmp_path / "hist.db"
    now = time.time()
    for i in range(6):
        _log_new(tmp_path, db, now - 3000 + i * 500, 12000 + i * 50)
    s = period_summary_ce("1h", path=db, device_id=_CE, kwh_rub=10.0)
    e = s["energy_kwh_import"]
    assert (e["first"], e["last"], e["delta"]) == (12.0, 12.25, 0.25)
    assert s["cost_rub"] == 2.5


def test_mixed_archive_delta_spans_the_boundary(tmp_path: Path):
    """Old 0.1 kWh rows then new Wh rows: ΔE = last_new − first_old, within the
    old half-step of the truth (one rounded endpoint instead of two)."""
    db = tmp_path / "hist.db"
    now = time.time()
    old_wh = [12040, 12080, 12120]     # stored 12.0, 12.1, 12.1
    new_wh = [12180, 12260, 12345]     # stored 12.18, 12.26, 12.345
    for i, wh in enumerate(old_wh):
        _log_old(db, now - 3000 + i * 300, wh)
    for i, wh in enumerate(new_wh):
        _log_new(tmp_path, db, now - 1500 + i * 300, wh)
    assert _raw_energy(db) == [12.0, 12.1, 12.1, 12.18, 12.26, 12.345]

    s = period_summary_ce("1h", path=db, device_id=_CE, kwh_rub=10.0)
    e = s["energy_kwh_import"]
    assert e["first"] == 12.0 and e["last"] == 12.345
    assert e["delta"] == pytest.approx(0.345, abs=1e-9)
    true_kwh = (new_wh[-1] - old_wh[0]) / 1000.0
    assert abs(e["delta"] - true_kwh) <= 0.05 + 1e-9
    assert s["cost_rub"] == 3.45

    # The history read serves old rows as stored and new rows at Wh, side by side.
    h = history("energy_kwh_import", "1h", path=db, device_id=_CE, bucket_s=1.0)
    assert [p[1] for p in h["series"][0]["points"]] == [
        12.0, 12.1, 12.1, 12.18, 12.26, 12.345,
    ]


# The archived reset-to-zero expectation was not migrated: the current
# energy contract detects counter resets and integrates subsequent increments.
# Its reset/glitch cases live in test_ce_energy_summary_break.py.


# ── formats unchanged ────────────────────────────────────────────────


_ENERGY_GROUP_HEADERS = [
    "Время", "Ua, V", "Ub, V", "Uc, V", "Ia, A", "Ib, A", "Ic, A",
    "Pa, W", "Pb, W", "Pc, W", "P∑, W", "f, Hz", "E, kWh",
]


def test_export_table_format_unchanged_values_at_wh(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = float(int(time.time()) // 60 * 60)
    _log_new(tmp_path, db, now - 170, 12345)
    one = collect_export_table("1h", metric_id="energy_kwh_import", device_id=_CE, path=db)
    assert one["ok"] and one["kind"] == "ce" and one["title"] == "Энергия (импорт)"
    assert one["headers"] == ["Время", "E, kWh"]
    assert len(one["rows"]) == 1 and one["rows"][0][1] == 12.345
    grp = collect_export_table("1h", group="energy", device_id=_CE, path=db)
    assert grp["headers"] == _ENERGY_GROUP_HEADERS
    assert grp["rows"][0][-1] == 12.345


def test_export_text_format_unchanged(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = float(int(time.time()) // 60 * 60)
    _log_new(tmp_path, db, now - 170, 12345)
    body, name = export_text("1h", metric_id="energy_kwh_import", device_id=_CE, path=db)
    lines = body.splitlines()
    assert lines[0] == f"# устройство: {_CE}"
    assert lines[1] == "# метрика: Энергия (импорт)"
    assert lines[2].startswith("# период: ")
    assert lines[3] == "# шаг: 1 мин"
    assert lines[4] == "Время\tE, kWh"
    assert lines[5].split("\t")[1] == "12.345"
    assert name.startswith(f"ce_export_{_CE}_energy_kwh_import_") and name.endswith(".txt")


def test_export_xlsx_format_unchanged(tmp_path: Path):
    openpyxl = pytest.importorskip("openpyxl")
    db = tmp_path / "hist.db"
    now = float(int(time.time()) // 60 * 60)
    _log_new(tmp_path, db, now - 170, 12345)
    raw, name = export_xlsx("1h", metric_id="energy_kwh_import", device_id=_CE, path=db)
    assert name.endswith(".xlsx")
    ws = openpyxl.load_workbook(BytesIO(raw)).active
    assert [ws.cell(7, 1).value, ws.cell(7, 2).value] == ["Время", "E, kWh"]
    assert ws.cell(8, 2).value == 12.345
    assert ws.cell(8, 2).number_format == "0.###"


def test_summary_format_unchanged(tmp_path: Path):
    db = tmp_path / "hist.db"
    now = time.time()
    _log_new(tmp_path, db, now - 600, 12345)
    _log_new(tmp_path, db, now - 300, 12400)
    s = period_summary_ce("1h", path=db, device_id=_CE, kwh_rub=10.0)
    assert set(s) == {
        "ok", "device", "device_id", "range", "t0", "t1", "samples", "power_w",
        "energy_kwh_import", "kwh_rub", "kwh_rub_default", "kwh_rub_unit",
        "cost_rub", "cost_basis", "cost_note",
    }
    assert set(s["power_w"]) == {"a", "b", "c", "total"}
    assert s["energy_kwh_import"] == {
        "first": 12.345, "last": 12.4, "delta": 0.055, "unit": "kWh",
    }
    assert s["kwh_rub_unit"] == "RUB/kWh" and s["cost_basis"] == "energy_kwh"
    assert s["cost_rub"] == 0.55
