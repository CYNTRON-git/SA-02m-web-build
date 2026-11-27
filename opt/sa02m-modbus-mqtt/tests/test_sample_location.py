"""An unchanged MQTT id follows a physical move only when the reader moves."""
import json
from pathlib import Path
import sys
import time
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "sa02m-devices"))
import bridge_mqtt
import modbus_mqtt_bridge as bridge
from sa02m_devices.stand_devices import live_snapshot


def test_sample_location_is_written_with_measurements(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_mqtt, "LIVE_CACHE_DIR", tmp_path)
    did = "dtv-COM5-3"
    cache = bridge.DeviceLiveCache
    rows = [{"id":did, "type":"dtv", "port":"/dev/COM5", "address":3}]
    def register():
        bridge.compose_pollers(rows, mock.Mock())
    def read():
        cache.flush_file(did)
        return live_snapshot(tmp_path)["dtv"][0]
    register()
    cache.set_online(did, True)
    cache.set_control(did, "temp_hdc1080", "21")
    rows[0]["port"] = "/dev/COM4"
    # Editing the configuration does not change the running reader.
    assert read()["port_num"] == 5
    register()
    assert live_snapshot(tmp_path)["dtv"][0]["port_num"] == 5
    # A writeback or idle flush on the new configuration has not observed COM4.
    cache.set_control(did, "temp_hdc1080", "22")
    unconfirmed = read()
    assert unconfirmed["location_source"] == "unknown" and unconfirmed["age_s"] is None
    cache.set_online(did, True)
    card = read()
    assert (card["id"], card["com"], card["addr"]) == (did, "COM4", 3)
    assert card["location_source"] == "sample" and card["ok"] is True and card["room_temp"] == 22
    rows.append(dict(rows[0], port="/dev/COM5"))
    register()
    card = read()
    assert (card["location_source"], card["com"], card["port_num"], card["addr"]) == ("unknown","",None,None)
    f = tmp_path/(did+".json")
    payload = json.loads(f.read_text())
    for invalid in ({"com":"COM12", "addr":3}, {"com":"COM4", "host":"127.0.0.1","addr":3}, False):
        payload["poll_location"] = invalid
        f.write_text(json.dumps(payload))
        card = live_snapshot(tmp_path)["dtv"][0]
        assert card["location_source"] == "unknown" and card["port_num"] is None
    del payload["poll_location"]
    f.write_text(json.dumps(payload))
    card = live_snapshot(tmp_path)["dtv"][0]
    assert card["location_source"] == "legacy_id" and card["port_num"] == 5


def test_every_card_uses_its_sample_coordinates(tmp_path):
    controls = {"module_type":"12AI", "plant_state":"run", "temp_hdc1080":"21"}
    for prefix in ("dtv", "ce02m3", "spodes", "carel", "mr02m", "mtdx62-mb"):
        did = prefix + "-COM5-3"
        (tmp_path / (did + ".json")).write_text(json.dumps({
            "device":did, "ok":True, "ts":time.time(), "controls":controls,
            "poll_location":{"com":"COM4", "addr":11}}))
    cards = live_snapshot(tmp_path)["devices"]
    assert len(cards) == 6
    assert all((card["com"], card["addr"], card["location_source"]) == ("COM4",11,"sample")
               and "порт 4" in card["label"] for card in cards)
    target = tmp_path / "carel-COM5-3.json"
    payload = json.loads(target.read_text())
    payload["poll_location"] = {"host":"192.0.2.20", "addr":11}
    target.write_text(json.dumps(payload))
    card = live_snapshot(tmp_path)["carel"][0]
    assert card["port_num"] is None and card["host"] == "192.0.2.20" and card["addr"] == 11
