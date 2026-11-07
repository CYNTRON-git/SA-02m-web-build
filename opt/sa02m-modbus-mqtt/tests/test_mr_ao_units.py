"""Dimmer metadata describes raw permille; existing MQTT values and writes stay raw."""
import types
import unittest
from unittest.mock import Mock, patch
from test_mr02m_ai_register_truth import RecordingPub
import bridge_mr02m
from bridge_mqtt import DeviceLiveCache


class MrAoUnits(unittest.TestCase):
    def test_metadata_poll_and_writeback_keep_raw_words(self):
        for kind, unit in ((15, '‰'), (3, 'V'), (11, 'V')):
            with self.subTest(kind=kind), patch.object(DeviceLiveCache, 'flush_file'):
                pub = RecordingPub()
                callbacks = {}
                pub.subscribe_writeback = lambda did, name, cb: callbacks.update({name: cb})
                poller = bridge_mr02m.MR02mPoller({'id': 'ao-test', 'type': 'mr02m', 'address': 2,
                    'module_type': kind, 'channels': {}}, pub)
                poller._mod_type = kind
                poller._do, poller._di, poller._ao, poller._ai = 0, 0, 1, 0
                poller._publish_channel_meta()
                self.assertEqual(pub.units['ao_1'], unit)
                self.assertEqual(pub.meta[('ao_1', 'min')], '0')
                self.assertEqual(pub.meta[('ao_1', 'max')], '1000')
                port = Mock()
                poller.get_port = lambda: port
                poller._wb_offline_skip = lambda *_: False
                poller._wb_submit = lambda name, fn: fn()
                poller._setup_writeback()
                for value in (0, 1, 500, 1000):
                    poller._wb_recent.clear()
                    poller.read_holding_registers = lambda addr, start, count: [value]
                    poller._poll_ai_ao()
                    self.assertEqual(pub.controls[-1], ('ao_1', str(value)))
                    callbacks['ao_1'](None, None, types.SimpleNamespace(payload=str(value).encode(), retain=False))
                    port.write_register.assert_called_with(2, 33, value)
                writes = port.write_register.call_count
                callbacks['ao_1'](None, None, types.SimpleNamespace(payload=b'500', retain=True))
                self.assertEqual(port.write_register.call_count, writes)


if __name__ == '__main__':
    unittest.main()
