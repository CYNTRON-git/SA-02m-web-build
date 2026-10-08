"""A non-finite control in the MQTT cache never reaches the live snapshot.

`stand_devices._f` parsed with float() alone, so a control published as
"nan" / "inf" became NaN / inf in the ДТВ/СЭ/Меркурий cards, and
`/api/devices` wrote it as NaN / Infinity — tokens a browser's JSON.parse
rejects, which blanks the whole Devices tab, not one field.

RED (before the fix): voltage "nan" yields NaN and json.dumps(allow_nan=False)
raises. GREEN: every non-finite reading is None («нет данных»).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sa02m_devices.stand_devices import live_snapshot


def test_non_finite_controls_read_as_missing(tmp_path: Path):
    cache = tmp_path / "mqtt"
    cache.mkdir()
    (cache / "ce02m3-COM2-14.json").write_text(
        json.dumps({
            "ok": True,
            "device": "ce02m3-COM2-14",
            "ts": time.time(),
            "controls": {
                "voltage_a": "nan",
                "voltage_b": "230.5",
                "power_total": "inf",
                "frequency": "-Infinity",
                "energy_active_import": "NaN",
            },
        }),
        encoding="utf-8",
    )
    snap = live_snapshot(cache)
    ce = snap["ce"][0]
    assert ce["voltage"]["a"] is None
    assert ce["voltage"]["b"] == 230.5
    assert ce["power_w"]["total"] is None
    assert ce["frequency_hz"] is None
    assert ce["energy_kwh_import"] is None
    json.dumps(snap, allow_nan=False)
