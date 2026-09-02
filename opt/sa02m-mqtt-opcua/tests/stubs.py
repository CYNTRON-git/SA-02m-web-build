# -*- coding: utf-8 -*-
"""Import the shipped MQTT->OPC UA gateway on a box that has none of its deps.

`sa02m-mqtt-opcua.py` calls `sys.exit()` at import time when paho-mqtt or the
`opcua` package is missing — correct for a daemon, fatal for a test. CI installs
neither (`.github/workflows/web-quality.yml` installs pytest/pyserial/pyyaml/
cryptography only) and adding them would cost CI budget for logic that never
touches a socket. So the two device libraries are replaced by minimal recording
stubs BEFORE the module is loaded, and the gateway's own code is the real thing.

The stubs are deliberately dumb: they record what the gateway asked them to do
(publish, add_variable, set_value) so a test can assert on the CALL, which is
where every guarantee in this daemon lives. Anything the gateway relies on that
a stub does not provide shows up as an AttributeError in the test, not as a
silent pass — that is the intended failure mode.
"""
import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_DIR = os.path.dirname(_HERE)
MODULE_PATH = os.path.join(_PKG_DIR, "sa02m-mqtt-opcua.py")


class RecordingMqttClient:
    """Stands in for paho's Client. Records publish/subscribe calls."""

    def __init__(self, *a, **kw):
        self.published = []          # (topic, payload, qos, retain)
        self.subscribed = []
        self.credentials = None
        self.on_connect = self.on_message = self.on_disconnect = None
        self.connected_to = None
        self.loop_running = False

    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

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


class _Variable:
    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.writable = False

    def set_writable(self):
        self.writable = True

    def set_value(self, v):
        self.value = v

    def get_value(self):
        return self.value


class _Object:
    def __init__(self, name):
        self.name = name
        self.variables = []

    def add_object(self, ns, name):
        return _Object(name)

    def add_variable(self, ns, name, value):
        var = _Variable(name, value)
        self.variables.append(var)
        return var


class RecordingOpcuaServer:
    def __init__(self, *a, **kw):
        self.endpoint = None
        self.server_name = None
        self.security_policies = None
        self.namespaces = []
        self.started = False
        self.nodes = types.SimpleNamespace(objects=_Object("Objects"))

    def set_endpoint(self, ep):
        self.endpoint = ep

    def set_server_name(self, n):
        self.server_name = n

    def set_security_policy(self, p):
        self.security_policies = p

    def register_namespace(self, uri):
        self.namespaces.append(uri)
        return len(self.namespaces)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


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
        # A box that really has paho must still get the recording client, or the
        # gateway would open a socket during the test run.
        sys.modules["paho.mqtt.client"].Client = RecordingMqttClient

    opcua = types.ModuleType("opcua")
    opcua.Server = RecordingOpcuaServer
    opcua.ua = types.SimpleNamespace(
        SecurityPolicyType=types.SimpleNamespace(NoSecurity="NoSecurity"))
    server_pkg = types.ModuleType("opcua.server")
    # user_manager is NOT imported by the daemon any more (1.0.6.24 — a security
    # symbol nothing wires is a guarantee that is not there). The stub stays so
    # that re-adding the import fails with the DEDICATED assertion in
    # TestNoDeadSecurityImport rather than with an opaque ImportError at load.
    user_manager = types.ModuleType("opcua.server.user_manager")
    user_manager.UserManager = type("UserManager", (), {})
    server_pkg.user_manager = user_manager
    opcua.server = server_pkg
    sys.modules["opcua"] = opcua
    sys.modules["opcua.ua"] = opcua.ua
    sys.modules["opcua.server"] = server_pkg
    sys.modules["opcua.server.user_manager"] = user_manager


def load_gateway():
    """Load the shipped daemon as a module named `sa02m_mqtt_opcua`."""
    _install_stubs()
    if "sa02m_mqtt_opcua" in sys.modules:
        return sys.modules["sa02m_mqtt_opcua"]
    if not os.path.exists(MODULE_PATH):
        raise AssertionError("shipped daemon not found: %s" % MODULE_PATH)
    spec = importlib.util.spec_from_file_location("sa02m_mqtt_opcua", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sa02m_mqtt_opcua"] = mod
    spec.loader.exec_module(mod)
    return mod
