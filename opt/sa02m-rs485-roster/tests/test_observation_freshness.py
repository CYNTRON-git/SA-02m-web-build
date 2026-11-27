"""A service heartbeat and an empty scheduler tick cannot renew a device observation."""
import json
from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
with patch.object(sys, "path", sys.path[:]):
    for directory in ("sa02m-rs485-roster", "sa02m-modbus-mqtt", "sa02m-devices"):
        sys.path.insert(0, str(ROOT / "opt" / directory))
    import bridge_device
    import bridge_mqtt
    import bridge_serial
    import modbus_mqtt_bridge as bridge
    import polling_roster
    from sa02m_devices.stand_devices import live_snapshot


@pytest.fixture
def reader(tmp_path, monkeypatch):
    did = "dtv-COM3-27"
    clock = [1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(bridge_mqtt, "LIVE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(bridge, "LIVE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(bridge_mqtt.DeviceLiveCache, "_online", {})
    monkeypatch.setenv("INVOCATION_ID", "ab" * 16)
    cfg = [{"type":"dtv", "id":did, "port":"/dev/COM3", "address":27,
            "fast_modbus":False, "poll_diag_s":3600,
            "channels":{"sys":[{"key":"uptime_s", "enabled":False}]}}]
    pub = Mock()
    states = {}
    pub.device_online.side_effect = states.__setitem__
    pub.device_online_snapshot.return_value = states
    by_bus, _, _ = bridge.compose_pollers(cfg, pub)
    poller = next(iter(by_bus.values()))[0]
    transport = Mock()
    transport.read_input_registers.return_value = [21]
    monkeypatch.setattr(poller, "get_port", lambda: transport)
    file = tmp_path / "_roster.json"
    bridge.DeviceLiveCache.set_control(did, "temp_hdc1080", "21")
    def observe():
        values = poller.read_input_registers(27, 0, 1)
        bridge.DeviceLiveCache.set_control(did, "temp_hdc1080", str(values[0]))
        bridge.DeviceLiveCache.flush_file(did)
    def dashboard():
        bridge.write_bridge_roster(cfg, pub, file)
        runtime = json.loads(file.read_text())
        rows = bridge.bridge_roster.prepare(cfg)
        return polling_roster.polling_ports(rows, runtime, clock[0], lambda: "ab" * 16)["COM3"]["ours"][0]["online"]
    def card():
        data = live_snapshot(tmp_path)["dtv"][0]
        return data["ok"], data["age_s"]
    return poller, transport, clock, observe, dashboard, card


def test_metadata_only_flush_before_first_read(reader):
    p, bus, _, _, dashboard, card = reader
    bridge.DeviceLiveCache.flush_file(p.device_id)
    assert not bus.method_calls
    assert dashboard() is None and card() == (False, None)


def test_export_during_actual_scheduler_hold(reader):
    p, bus, clock, observe, dashboard, card = reader
    assert dashboard() is None
    observe()
    assert dashboard() is True and card() == (True, 0)
    scheduler = bridge_device.PortCycleScheduler(p.port_path, p.baudrate, [p])
    scheduler._scan_hold.set()
    clock[0] = 1600
    with pytest.raises(bridge_serial.ScanHold):
        scheduler._classic_slice(1, True)
    assert bus.read_input_registers.call_count == 1 and card() == (False, 600)
    assert dashboard() is None


def test_empty_slow_tick_and_repeated_transport_results(reader):
    p, bus, clock, observe, dashboard, card = reader
    observe()
    clock[0] = 1600
    p.poll_slow_if_due(1600)
    assert bus.method_calls == [("read_input_registers",(27,0,1),{})]
    assert card() == (False, 600)
    observe()
    assert p.pub.device_online.call_count == 1 and card() == (True, 0) and dashboard() is True
    bus.read_input_registers.side_effect = OSError("transport timeout")
    for attempt in range(1, p._fail_threshold + 2):
        clock[0] += 1
        with bridge.DeviceLiveCache.flush_changes(), pytest.raises(OSError):
            p.read_input_registers(27, 0, 1)
        offline = attempt >= p._fail_threshold
        assert card() == (not offline, 0 if offline else attempt)
        assert dashboard() is (not offline)
    assert p.pub.device_online.call_count == 2


@pytest.mark.parametrize("stamp", [None, True, -1, "1000", float("nan"), float("inf"), 2000])
def test_invalid_observation_never_uses_recent_file(tmp_path, monkeypatch, stamp):
    monkeypatch.setattr(time, "time", lambda: 1000)
    d = {"device":"dtv-COM3-1", "ts":1000, "ok":True, "observed_at":stamp,
         "controls":{"temp_hdc1080":"21"}}
    (tmp_path/"dtv-COM3-1.json").write_text(json.dumps(d))
    row = live_snapshot(tmp_path)["dtv"][0]
    assert row["ok"] is False and row["age_s"] is None and row["ts"] is None


def test_all_families_age_the_observation_during_file_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1610)
    for prefix in ("dtv", "ce02m3", "spodes", "carel", "mr02m", "mtdx62-mb"):
        did = prefix + "-COM3-27"
        raw = {"device":did, "ts":1610, "ok":True, "observed_at":1000,
               "controls":{"module_type":"12AI", "plant_state":"run", "temp_hdc1080":"21"}}
        (tmp_path / (did+".json")).write_text(json.dumps(raw))
    rows = live_snapshot(tmp_path)["devices"]
    assert len(rows) == 6
    assert all((r["ok"], r["age_s"], r["ts"]) == (False, 610, 1000) for r in rows)
