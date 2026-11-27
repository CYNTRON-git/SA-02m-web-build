"""Exercise production protocols on a dedicated loopback namespace with synthetic points."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import tempfile
import time

import paho.mqtt.client as mqtt
from opcua import Client, ua  # A previously deployed client, for both generations

DEVICE = "sa02m-fixture"
BASE = f"/devices/{DEVICE}/controls"
NAMESPACE = "urn:sa02m:mqtt-opcua"


def wait_for(check, seconds=25):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(check)
        time.sleep(0.1)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(directory, command, report, strict_writes=False):
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Run in a dedicated network namespace containing only lo")
    broker_port, ua_port = free_port(), free_port()
    assert broker_port != ua_port and 4840 not in (broker_port, ua_port)
    broker_cfg = directory / "mosquitto.conf"
    broker_cfg.write_text(f"listener {broker_port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
    endpoint = f"opc.tcp://127.0.0.1:{ua_port}/sa02m/"
    config = directory / "gateway.json"
    config.write_text(json.dumps({
        "mqtt": {"host": "127.0.0.1", "port": broker_port},
        "opcua": {"host": "127.0.0.1", "port": ua_port, "allow_from": ["127.0.0.1/32"]},
        "groups": [{"controls": [{"topic": f"{DEVICE}/{p}"} for p in ("temp", "switch", "late")]}],
    }))
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    messages = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="opcua-fixture-observer")
    client.on_message = lambda c, u, m: messages.put((m.topic, m.payload.decode(), m.qos, m.retain))
    ua_client = Client(endpoint, timeout=4)
    ready = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify_path = str(directory / "notify")
    ready.bind(notify_path)
    ready.settimeout(55)
    gateway = None
    connected = False
    started = time.monotonic()
    env = dict(os.environ, NOTIFY_SOCKET=notify_path, WATCHDOG_USEC="4000000")
    # Only the legacy test interpreter needs its private distro XML libraries.
    # Start the broker and managed application with the normal loader policy.
    if env.pop("SA02M_OPCUA_PROBE_OLD_LOADER", None) is not None:
        env.pop("LD_LIBRARY_PATH", None)
    with (directory / "broker.log").open("wb") as blog, (directory / "gateway.log").open("wb") as glog:
        broker = subprocess.Popen([shutil.which("mosquitto"), "-c", str(broker_cfg)], env=env, stdout=blog, stderr=blog)

        def mqtt_connect():
            try:
                client.connect("127.0.0.1", broker_port, 10)
                return True
            except ConnectionRefusedError:
                return False

        def publish(tail, payload):
            ack = client.publish(f"{BASE}/{tail}", payload, qos=1, retain=True)
            ack.wait_for_publish(3)
            assert ack.is_published()

        def connect_ua():
            try:
                ua_client.connect()
                return True
            except ConnectionRefusedError:
                return False

        try:
            wait_for(mqtt_connect)
            client.loop_start()
            client.subscribe(f"{BASE}/+/on", qos=1)
            # Retained metadata must reach the subscriber before its first value.
            # This is the same ordered input for both daemon generations.
            for tail, payload in [("temp/meta/type", "temperature"), ("temp/meta/readonly", "1"),
                                  ("temp/meta/title", '{"en":"Air"}'), ("switch/meta/type", "switch"),
                                  ("switch/meta/readonly", "0")]:
                publish(tail, payload)
            gateway = subprocess.Popen([*command, "-c", str(config)], env=env, stdout=glog, stderr=glog)
            assert ready.recv(64) == b"READY=1"
            report["ready_s"] = round(time.monotonic() - started, 3)
            assert ready.recv(64) == b"WATCHDOG=1"
            wait_for(connect_ua, 90)
            connected = True
            report["endpoint_s"] = round(time.monotonic() - started, 3)
            ns = ua_client.get_namespace_index(NAMESPACE)
            root = ua_client.get_objects_node()
            assert f"{ns}:{DEVICE}" not in [n.get_browse_name().to_string() for n in root.get_children()]
            wait_for(lambda: "MQTT connected" in (directory / "gateway.log").read_text(errors="replace"))
            time.sleep(0.4)
            publish("temp", "21.5")
            publish("switch", "0")
            publish("excluded", "ignored")
            def lookup(parent, browse):
                return next((n for n in parent.get_children() if n.get_browse_name().to_string() == f"{ns}:{browse}"), None)
            device = wait_for(lambda: lookup(root, DEVICE))
            temp = wait_for(lambda: lookup(device, "temp (Air)"))
            switch = wait_for(lambda: lookup(device, "switch"))
            wait_for(lambda: temp.get_value() == 21.5 and switch.get_value() is False)
            assert not lookup(device, "excluded")
            report["nodes"] = {name: [node.nodeid.to_string(), node.get_data_type_as_variant_type().name, node.get_value()]
                               for name, node in (("temp", temp), ("switch", switch))}
            assert report["nodes"]["temp"][1] == "Double" and report["nodes"]["switch"][1] == "Boolean"
            # Wait for the legacy poller to record its initial value.
            wait_for(lambda: "initial nodes)" in (directory / "gateway.log").read_text(errors="replace"))
            time.sleep(1.2)
            try:
                temp.set_value(23.0)
            except ua.UaStatusCodeError as exc:
                assert exc.code in (ua.StatusCodes.BadUserAccessDenied, ua.StatusCodes.BadNotWritable)
            else:
                raise AssertionError("read-only point accepted a client write")
            switch.set_value(True)
            assert messages.get(timeout=6) == (f"{BASE}/switch/on", "1", 1, False)
            # A new subscription must not replay the command as a retained value.
            client.unsubscribe(f"{BASE}/+/on")
            client.subscribe(f"{BASE}/+/on", qos=1)
            publish("late/meta/type", "range")
            publish("late", "7")
            late = wait_for(lambda: lookup(device, "late"))
            report["nodes"]["late"] = [late.nodeid.to_string(), late.get_data_type_as_variant_type().name, late.get_value()]
            assert report["nodes"]["late"][1:] == ["Int64", 7]
            ua_client.disconnect()
            connected = False
            ua_client.connect()
            connected = True
            assert ua_client.get_node(temp.nodeid).get_value() == 21.5
            assert ready.recv(64) == b"WATCHDOG=1"
            time.sleep(1.1)
            assert messages.empty()
            publish("switch", "0")
            wait_for(lambda: switch.get_value() is False)
            time.sleep(1.4)
            report["telemetry_commands"] = []
            while not messages.empty():
                report["telemetry_commands"].append(list(messages.get_nowait()))
            if strict_writes:
                assert not report["telemetry_commands"], "MQTT telemetry emitted an output command"
                try:
                    switch.set_value("wrong type")
                except ua.UaStatusCodeError as exc:
                    assert exc.code == ua.StatusCodes.BadTypeMismatch
                else:
                    raise AssertionError("Boolean point accepted String")
                bad = ua.DataValue(ua.Variant(False, ua.VariantType.Boolean))
                bad.StatusCode = ua.StatusCode(ua.StatusCodes.BadSensorFailure)
                try:
                    switch.set_value(bad)
                except ua.UaStatusCodeError:
                    pass  # A provider may reject a client-supplied quality.
                # Telemetry moved the baseline from True to False. Repeating a
                # successful external value emits nothing; distinct changes in
                # one poll interval must each appear in order.
                for desired in (True, True, False, True):
                    switch.set_value(desired)
                for expected in ("1", "0", "1"):
                    assert messages.get(timeout=4) == (f"{BASE}/switch/on", expected, 1, False)
                time.sleep(0.4)
                assert messages.empty()
                report["external_commands"] = ["1", "0", "1"]
            # One synthetic client occupies the gateway ID briefly. Mosquitto
            # closes the prior TCP session; the gateway must reconnect, replay
            # its subscriptions and expose the next retained measurement.
            rival = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sa02m-mqtt-opcua")
            rival_connected = []
            rival.on_connect = lambda *args: rival_connected.append(True)
            rival.connect("127.0.0.1", broker_port, 10)
            rival.loop_start()
            try:
                wait_for(lambda: rival_connected)
            finally:
                rival.disconnect()
                rival.loop_stop()
            wait_for(lambda: (directory / "gateway.log").read_text(errors="replace").count("MQTT connected") >= 2)
            publish("temp", "22.0")
            wait_for(lambda: temp.get_value() == 22.0)
            report["mqtt_reconnected"] = True
            status = Path(f"/proc/{gateway.pid}/status").read_text().splitlines()
            report["memory"] = {s.split(":")[0]: s.split(":")[1].strip() for s in status if s.startswith(("VmRSS:", "VmHWM:"))}
        finally:
            try:
                if connected:
                    ua_client.disconnect()
            finally:
                if gateway is not None:
                    stopped = time.monotonic()
                    gateway.send_signal(signal.SIGTERM)
                    try:
                        report["exit"] = gateway.wait(timeout=20)
                    finally:
                        if gateway.poll() is None:
                            gateway.kill()
                            gateway.wait()
                    report["stop_s"] = round(time.monotonic() - stopped, 3)
                client.disconnect()
                client.loop_stop()
                broker.terminate()
                broker.wait(timeout=8)
                ready.close()
    assert report["exit"] == 0 and hashlib.sha256(config.read_bytes()).hexdigest() == config_hash


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--strict-writes", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("A gateway command is required")
    result = {}
    with tempfile.TemporaryDirectory(prefix="ua-fixture-") as tmp:
        path = Path(tmp)
        try:
            run(path, args.command, result, args.strict_writes)
        finally:
            args.output.mkdir(parents=True, exist_ok=True)
            for log in path.glob("*.log"):
                shutil.copyfile(log, args.output / log.name)
            (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
