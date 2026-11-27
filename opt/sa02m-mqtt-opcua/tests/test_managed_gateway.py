"""Dependency-free checks for the managed generation's command and startup boundaries."""
from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from . import stubs


def load():
    stubs.load_gateway()
    ua = NS(AttributeIds=NS(Value=13), SecurityPolicyType=NS(NoSecurity=0), AnonymousIdentityToken=object)
    modules = {"asyncua": NS(ua=ua), "asyncua.sync": NS(Server=Mock()),
               "asyncua.common.callback": NS(CallbackType=NS(PostWrite=5)),
               "asyncua.server": NS(binary_server_asyncio=NS(), server=NS())}
    path = Path(__file__).resolve().parents[1] / "sa02m-mqtt-opcua-managed.py"
    spec = importlib.util.spec_from_file_location("managed_test_gateway", path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(mod)
    return mod


gw = load()


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.g = gw.OpcuaGateway({})
        self.key = ("fixture", "switch")
        self.g._write_points[42] = (self.key, False)
        self.g._controls[self.key] = NS(readonly=False)
        self.publish = Mock(return_value=NS(rc=0))
        self.g._mqtt.publish = self.publish
        self.addCleanup(patch.stopall)
        patch.object(gw.mqtt, "MQTT_ERR_SUCCESS", 0, create=True).start()

    def change(self, value, external=True, quality=True, accepted=True, attribute=13):
        data = NS(Value=NS(Value=value), StatusCode=NS(is_good=lambda: quality))
        item = NS(NodeId=42, AttributeId=attribute, Value=data)
        params = NS(NodesToWrite=[item])
        result = NS(is_good=lambda: accepted)
        self.g._after_write(NS(request_params=params, response_params=[result], is_external=external), None)

    def test_external_changes_only_in_order(self):
        self.change(True)
        self.change(False, external=False)
        self.change(True, quality=False)
        self.change(True, accepted=False)
        self.change(True, attribute=16)
        for value in (True, True, False, True):
            self.change(value)
        self.assertEqual([call.args[1] for call in self.publish.call_args_list], ["1", "1", "0", "1"])
        for call in self.publish.call_args_list:
            self.assertEqual((call.args[0], call.kwargs), ("/devices/fixture/controls/switch/on", {"qos": 1, "retain": False}))

    def test_failed_publication_is_visible_and_not_reissued(self):
        self.publish.side_effect = [RuntimeError("offline"), NS(rc=4)]
        with self.assertLogs(gw.log, level="WARNING") as captured:
            self.change(True)
            self.change(False)
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(len(captured.records), 2)

    def test_configuration_failure_stops_owned_loop(self):
        server = Mock()
        server.set_endpoint.side_effect = ValueError("invalid endpoint")
        with patch.object(gw, "OpcuaServer", return_value=server):
            with self.assertRaisesRegex(ValueError, "invalid endpoint"):
                self.g.start()
        server.stop.assert_called_once()

    def test_broker_failure_releases_server(self):
        server = Mock()
        server.tloop.is_alive.return_value = False
        self.g._mqtt.connect = Mock(side_effect=ConnectionRefusedError())
        with patch.object(gw, "OpcuaServer", return_value=server), patch.object(gw, "_transport_policy", return_value=nullcontext()):
            with self.assertRaises(ConnectionRefusedError):
                self.g.start()
        server.start.assert_called_once()
        server.stop.assert_called_once()
