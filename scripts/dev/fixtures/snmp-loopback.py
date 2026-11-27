"""Qualify the actual SNMP bridge in a disposable Linux loopback namespace."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import paho.mqtt.client as mqtt
from pyasn1.codec.ber import decoder, encoder
from pysnmp.proto import api


def main(bridge):
    with tempfile.TemporaryDirectory() as temp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as agent:
        agent.bind(("127.0.0.1", 161))
        agent.settimeout(.25)
        port = 21883
        broker = subprocess.Popen(["mosquitto", "-p", str(port)], stdout=subprocess.DEVNULL)
        subscribed = threading.Event()
        seen = set()
        observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        observer.on_subscribe = lambda *_: subscribed.set()
        observer.on_message = lambda client, userdata, msg: seen.add((msg.topic, msg.payload))
        Path(temp).chmod(0o755)
        cfg = Path(temp) / "config.json"
        cfg.write_text(json.dumps({"mqtt": {"host": "127.0.0.1", "port": port}, "devices": [
            {"id": "test-" + label, "address": address, "snmp_version": v, "snmp_timeout": 1,
             "poll_interval": 20000, "channels": [{"name": "reading", "oid": "1.3.6.1.2.1.1.1.0"}]}
            for label, v, address in [("1", "1", "127.0.0.1"), ("2c", "2c", "127.0.0.1"),
                                      ("silent", "2c", "127.0.0.2")]]}))
        cfg.chmod(0o644)
        account = {"user": 33, "group": 33, "extra_groups": []}
        args = [sys.executable, "-I", "-B", str(bridge), "-c", str(cfg)]
        child = None
        try:
            for attempt in range(50):
                try:
                    observer.connect("127.0.0.1", port)
                    break
                except ConnectionRefusedError:
                    if attempt == 49 or broker.poll() is not None:
                        raise
                    time.sleep(.1)
            observer.loop_start()
            observer.subscribe("/devices/#", qos=1)
            assert subscribed.wait(5), "observer did not subscribe"
            child = subprocess.Popen(args, **account)
            replies = set()
            wanted = {("/devices/test-1/controls/reading", b"version-0"),
                      ("/devices/test-2c/controls/reading", b"version-1"),
                      ("/devices/test-silent/controls/reading/meta/error", b"r")}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not wanted.issubset(seen):
                assert child.poll() is None, "bridge exited while polling"
                try:
                    packet, endpoint = agent.recvfrom(8192)
                except TimeoutError:
                    continue
                version = int(api.decodeMessageVersion(packet))
                protocol = api.PROTOCOL_MODULES[version]
                message, remainder = decoder.decode(packet, asn1Spec=protocol.Message())
                assert not remainder and str(protocol.apiMessage.get_community(message)) == "public"
                pdu = protocol.apiMessage.get_pdu(message)
                assert pdu.tagSet == protocol.GetRequestPDU.tagSet
                ((oid, _),) = protocol.apiPDU.get_varbinds(pdu)
                assert str(oid) == "1.3.6.1.2.1.1.1.0"
                response = protocol.apiMessage.get_response(message)
                protocol.apiPDU.set_varbinds(protocol.apiMessage.get_pdu(response),
                                              [(oid, protocol.OctetString(f"version-{version}"))])
                agent.sendto(encoder.encode(response), endpoint)
                replies.add(version)
            assert replies == {0, 1} and wanted.issubset(seen), (replies, wanted - seen)
        finally:
            if child:
                child.terminate()
                child.wait(timeout=12)
            observer.disconnect()
            observer.loop_stop()
            broker.terminate()
            broker.wait(timeout=5)
        denied = subprocess.run(args, capture_output=True, timeout=15, **account)
        assert denied.returncode != 0 and b"ConnectionRefusedError" in denied.stderr, denied
        print(json.dumps({"bridge_uid": account["user"], "snmp_versions": ["1", "2c"],
                          "mqtt_values": 2, "timeout_reported": True, "broker_absent_refused": True}))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
