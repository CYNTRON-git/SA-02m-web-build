# -*- coding: utf-8 -*-
"""Import the shipped MQTT-SNMP bridge on a box that has none of its deps.

`sa02m-mqtt-snmp.py` calls `sys.exit()` at import time when paho-mqtt or pysnmp
is missing. CI installs neither (`.github/workflows/web-quality.yml` installs
pytest/pyserial/pyyaml/cryptography only), and adding pysnmp to CI would cost
budget for logic that never opens a UDP socket in these tests. So both device
libraries are replaced by minimal recording stubs BEFORE the module is loaded;
the bridge's own code is the real thing.

The bridge has TWO import branches (pysnmp 7.x async first, 4.x sync as the
fallback) and sets `_PYSNMP7` from whichever succeeded. The stub satisfies the
7.x branch, so `_PYSNMP7` is True in these tests and `_snmp_get` is never
called — every test drives the layers around it (`_make_oid`, `_format_value`,
`_publish_meta`, `poll`) with the SNMP GET itself injected.
"""
import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_DIR = os.path.dirname(_HERE)
MODULE_PATH = os.path.join(_PKG_DIR, "sa02m-mqtt-snmp.py")


class RecordingMqttClient:
    def __init__(self, *a, **kw):
        self.published = []          # (topic, payload, qos, retain)
        self.credentials = None
        self.on_connect = self.on_disconnect = None
        self.connected_to = None
        self.loop_running = False

    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def connect(self, host, port, keepalive):
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        self.loop_running = True

    def loop_stop(self):
        self.loop_running = False

    def disconnect(self):
        self.connected_to = None


class TimeTicksStub:
    """Stands in for pysnmp's TimeTicks — the bridge only isinstance()-tests it."""

    def __init__(self, value=0):
        self.value = value

    def prettyPrint(self):
        return str(self.value)


def _install_stubs():
    if "paho" not in sys.modules:
        paho = types.ModuleType("paho")
        paho_mqtt = types.ModuleType("paho.mqtt")
        client_mod = types.ModuleType("paho.mqtt.client")
        client_mod.Client = RecordingMqttClient
        client_mod.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
        paho.mqtt = paho_mqtt
        paho_mqtt.client = client_mod
        sys.modules["paho"] = paho
        sys.modules["paho.mqtt"] = paho_mqtt
        sys.modules["paho.mqtt.client"] = client_mod
    else:
        sys.modules["paho.mqtt.client"].Client = RecordingMqttClient

    pysnmp = types.ModuleType("pysnmp")
    hlapi = types.ModuleType("pysnmp.hlapi")
    hlapi_async = types.ModuleType("pysnmp.hlapi.asyncio")
    for name in ("CommunityData", "ContextData", "ObjectIdentity",
                 "ObjectType", "SnmpEngine"):
        setattr(hlapi_async, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    hlapi_async.UdpTransportTarget = type(
        "UdpTransportTarget", (), {"create": staticmethod(lambda *a, **k: None)})
    hlapi_async.get_cmd = lambda *a, **k: None
    proto = types.ModuleType("pysnmp.proto")
    rfc1902 = types.ModuleType("pysnmp.proto.rfc1902")
    rfc1902.TimeTicks = TimeTicksStub
    pysnmp.hlapi = hlapi
    hlapi.asyncio = hlapi_async
    pysnmp.proto = proto
    proto.rfc1902 = rfc1902
    sys.modules["pysnmp"] = pysnmp
    sys.modules["pysnmp.hlapi"] = hlapi
    sys.modules["pysnmp.hlapi.asyncio"] = hlapi_async
    sys.modules["pysnmp.proto"] = proto
    sys.modules["pysnmp.proto.rfc1902"] = rfc1902


def load_bridge():
    """Load the shipped daemon as a module named `sa02m_mqtt_snmp`."""
    _install_stubs()
    if "sa02m_mqtt_snmp" in sys.modules:
        return sys.modules["sa02m_mqtt_snmp"]
    if not os.path.exists(MODULE_PATH):
        raise AssertionError("shipped daemon not found: %s" % MODULE_PATH)
    spec = importlib.util.spec_from_file_location("sa02m_mqtt_snmp", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sa02m_mqtt_snmp"] = mod
    spec.loader.exec_module(mod)
    return mod
