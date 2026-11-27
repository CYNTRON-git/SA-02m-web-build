import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest import mock

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
import aggregate
import polling_roster as polling
import providers
import roster_files


def config(path, rows):
    path.write_text(json.dumps({"devices": rows}), encoding="utf-8")


def test_producer_admission_no_io(tmp_path, monkeypatch):
    with monkeypatch.context() as env:
        env.syspath_prepend(str(polling.BRIDGE))
        import bridge_device, bridge_mqtt, bridge_serial
    def forbidden(*args, **kwargs):
        pytest.fail("bus or network I/O from roster projection")
    for obj, fn in ((bridge_device.DevicePoller, "get_port"), (bridge_serial, "get_port"),
                    (bridge_mqtt.MQTTPublisher, "connect"), (bridge_mqtt.MQTTPublisher, "pub")):
        monkeypatch.setattr(obj, fn, forbidden)
    rows = [{"type":"dtv", "id":"first", "channels_enabled":{"io": False}},
            {"type":"carel", "id":"ahu", "family":"crst", "port":"/dev/COM3", "address":22},
            {"type":"template", "id":"mtd", "port":"/dev/COM3", "template":"mtdx62-mb"},
            {"type":"mr02m", "id":"mixed", "port":"/dev/COM4"},
            {"type":"spodes", "id":"hdlc", "port":"/dev/COM4"},
            {"type":"template", "id":"tcp", "transport":"tcp", "host":"192.0.2.1"},
            {"type":"led", "id":"remote", "transport":"rtu_tcp", "host":"192.0.2.2"},
            {"type":"spodes", "id":"wrap", "transport":"wrapper", "host":"192.0.2.3"},
            {"type":"spodes", "id":"gateway", "transport":"transparent", "host":"192.0.2.4"},
            {"type":"bogus", "id":"bad"}, None]
    path, memo = tmp_path/"config", tmp_path/"memo"
    config(path, rows)
    _, saved = polling.configured_rows(path, memo)
    assert [(r["com"], r["addr"], r["ours"]) for r in saved] == [
        ("COM1", 1, True), ("COM3", 22, False), ("COM3", 1, False)]
    assert all(set(r) == {"com", "addr", "binding", "ours", "model"} for r in saved)
    assert all(r["binding"] for r in saved)
    assert memo.stat().st_mode & 0o777 == 0o600


def test_memo_race_and_fail_closed(tmp_path, monkeypatch, capsys):
    path, memo = tmp_path/"config", tmp_path/"memo"
    config(path, [{"id":"dtv", "type":"dtv"}])
    with mock.patch.object(polling, "_source_rows", wraps=polling._source_rows) as parse:
        a = polling.configured_rows(path, memo)
        assert polling.configured_rows(path, memo) == a
        assert parse.call_count == 1
        memo.write_text('{"rows":false}')
        assert polling.configured_rows(path, memo) == a
        memo.chmod(0o644)
        assert polling.configured_rows(path, memo) == a
        assert parse.call_count == 3
        other = tmp_path/"new"
        config(other, [])
        os.replace(other, path)
        assert polling.configured_rows(path, memo)[1] == []
        assert parse.call_count == 4
    for data in ("devices: secret-sentinel", "devices: []\ndevices: []", "x: &a [*a]\ndevices: []",
                 "devices: [\nsecret-sentinel", "a" * (roster_files.CONFIG_LIMIT + 1)):
        path.write_text(data)
        assert polling.projection(None, time.time(), path, memo) == {"polling_ports": {}}
    path.unlink()
    if os.name == "posix":
        os.mkfifo(path)
        assert polling.projection(None, time.time(), path, memo) == {"polling_ports": {}}
        path.unlink()
        path.symlink_to(memo)
        assert polling.projection(None, time.time(), path, memo) == {"polling_ports": {}}
        path.unlink()
    config(path, [{"type":"dtv","id":"dtv"}])
    def replace_while_reading(*_):
        config(path, [])
        return []
    monkeypatch.setattr(polling, "_source_rows", replace_while_reading)
    assert polling.projection(None, time.time(), path, memo) == {"polling_ports": {}}
    assert "secret-sentinel" not in capsys.readouterr().err


def test_exact_bindings_and_instance(tmp_path):
    from fixture_polling import INSTANCE, setup
    path, raw, _ = setup(tmp_path)
    data = json.loads(path.read_text())
    runtime = json.loads(raw.read_text())
    now = time.time()
    def ports(owner=INSTANCE, rt=runtime):
        return polling.projection(rt, now, path, tmp_path/"memo", lambda: owner)["polling_ports"]
    assert ports()["COM4"]["third_party"] == {"total":2, "online":1}
    for stamp in (None, now - 61, now + 1, float("nan")):
        old = dict(runtime, devices=[dict(row, observed_at=stamp) for row in runtime["devices"]])
        assert ports(rt=old)["COM4"]["third_party"]["online"] is None
    for change in ({"id":"other"}, {"baudrate":9600}, {"stopbits":2}, {"module_type":4},
                   {"type":"led"}, {"address":77}):
        base = data["devices"][0]
        altered = [dict(base, **change)]
        config(path, altered)
        assert ports()["COM4"]["ours"][0]["online"] is None
    config(path, [data["devices"][3], dict(data["devices"][3], template="changed")])
    assert ports()["COM4"]["third_party"] == {"total":2, "online":None}
    config(path, [dict(data["devices"][3], template="changed")])
    assert ports()["COM4"]["third_party"] == {"total":1, "online":None}
    config(path, [data["devices"][3], data["devices"][4]])
    assert ports()["COM4"]["third_party"] == {"total":2, "online":1}
    for r in runtime["devices"]:
        r["observed_online"] = False
    assert ports()["COM4"]["third_party"]["online"] == 0
    runtime["devices"][3]["observed_online"] = 1
    assert ports()["COM4"]["third_party"]["online"] is None
    config(path, data["devices"])
    for result in (ports(None), ports("2"*32), ports(rt=dict(runtime, ts=now-61)),
                   ports(rt=dict(runtime, ts=now+1)), ports(rt=dict(runtime, ts=float("nan"))),
                   ports(rt={"devices":runtime["devices"], "ts":now}), ports(rt=None)):
        assert result["COM4"]["third_party"]["online"] is None
        assert [item["online"] for item in result["COM4"]["ours"]] == [None]*3


def test_helper_and_cloud(tmp_path):
    from fixture_polling import INSTANCE, setup
    path, raw, scan = setup(tmp_path)
    p = providers.MqttBridgeProvider(path=raw)
    with mock.patch.object(p, "snapshot", wraps=p.snapshot) as read:
        payload = aggregate.aggregate([p, providers.FlasherScanProvider(scan)], path=path,
                                      memo=tmp_path/"memo", owner=lambda: INSTANCE)
        assert read.call_count == 1
    out = tmp_path/"result"
    aggregate.write_atomic(payload, out)
    spec = importlib.util.spec_from_file_location("cloud_agent", PKG.parent/"sa02m-cloud-agent/sa02m-cloud-agent.py")
    agent = importlib.util.module_from_spec(spec)
    with mock.patch("logging.FileHandler", side_effect=OSError):
        spec.loader.exec_module(agent)
    assert agent.read_roster_modules(str(out)) == {"ports":payload["ports"]}
    def helper():
        result = subprocess.run([sys.executable, str(PKG/"rs485_roster_cgi.py"), str(out)],
                                env=dict(os.environ, SA02M_MQTT_CONFIG=str(path)),
                                capture_output=True, check=True, text=True)
        return dict(line.split("\t",1) for line in result.stdout.splitlines())
    assert "2" not in helper() and "3" in helper()
    block = payload["polling_ports"]["COM4"]
    chip = next(r for r in block["ours"] if r["addr"] == 6)
    chip["observed_at"] = time.time() - 61
    block["third_party_observed_at"] = time.time() - 61
    aggregate.write_atomic(payload, out)
    modules = json.loads(helper()["3"])
    assert {r["addr"]:r["online"] for r in modules["ours"]} == {3:None, 6:None, 8:False}
    assert modules["live"] and modules["third_party"] == {"total":2, "online":None}
    old = dict(payload, ts=time.time()-61)
    for untrusted in (old, {"ts":time.time(), "ports":payload["ports"]}):
        aggregate.write_atomic(untrusted, out)
        assert not helper()
    payload["polling_ports"]["COM4"]["ts"] -= 61
    aggregate.write_atomic(payload, out)
    assert all(r["online"] is None for r in json.loads(helper()["3"])["ours"])
    config(path, [])
    assert not helper()
    with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl",1)):
        assert polling.active_invocation() is None


def test_warm_memo_follows_physical_tty_alias(tmp_path, monkeypatch):
    path, memo = tmp_path/"config", tmp_path/"memo"
    config(path, [{"id":"stable", "type":"dtv", "port":"/dev/ttyS8"}])
    realpath = os.path.realpath
    def local_alias(value, *args, **kwargs):
        if isinstance(value, str) and value.startswith("/dev/COM"):
            link = tmp_path / os.path.basename(value)
            if link.is_symlink():
                return os.readlink(link)
        return realpath(value, *args, **kwargs)
    monkeypatch.setattr(os.path, "realpath", local_alias)
    assert polling.configured_rows(path, memo)[1] == []
    (tmp_path/"COM3").symlink_to("/dev/ttyS8")
    assert [r["com"] for r in polling.configured_rows(path, memo)[1]] == ["COM3"]
    (tmp_path/"COM3").unlink()
    (tmp_path/"COM4").symlink_to("/dev/ttyS8")
    warm = polling.configured_rows(path, memo)[1]
    assert [r["com"] for r in warm] == ["COM4"]
    assert warm == polling.configured_rows(path, tmp_path/"cold")[1]


def test_during_delivery_keeps_previous_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.write_bytes(b"previous generation")
    env = dict(os.environ, SA02M_RS485_ROSTER_OUT=str(cache))
    for name in ("model_alias.py", "aggregate.py", "rs485_roster_cgi.py", "providers.py"):
        shutil.copyfile(PKG/name, tmp_path/name)
    def run(name):
        return subprocess.run([sys.executable, str(tmp_path/name)], env=env, capture_output=True)
    assert run("aggregate.py").returncode == 0
    assert run("rs485_roster_cgi.py").stdout == b""
    shutil.copyfile(PKG/"roster_files.py", tmp_path/"roster_files.py")
    assert run("aggregate.py").returncode == 0
    assert cache.read_bytes() == b"previous generation"
    (tmp_path/"model_alias.py").write_text("import missing_unrelated_package\n")
    assert run("aggregate.py").returncode != 0
    assert cache.read_bytes() == b"previous generation"


def test_inflight_producer_change_does_not_publish_memo(tmp_path, monkeypatch):
    config_path, memo = tmp_path/"config", tmp_path/"memo"
    config(config_path, [{"type":"dtv", "id":"device"}])
    inputs = polling._inputs()
    with mock.patch.object(polling, "_inputs", side_effect=[inputs, ["changed"]]):
        assert polling.projection(None, time.time(), config_path, memo) == {"polling_ports": {}}
    assert not memo.exists()
