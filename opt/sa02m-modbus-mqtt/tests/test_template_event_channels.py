"""Template catalog, service admission and WB event/classic MQTT value equivalence."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_template_poller import FakeSerial, _write_template, bridge
from test_fmb_wire_wb import with_crc
from bridge_template_channels import channel_catalog
from bridge_serial import FMB_EVT_HOLDING, FMB_EVT_INPUT, FMB_EVT_DISCRETE


class TemplateEvents(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict("os.environ", {"SA02M_WB_TEMPLATES_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.pub = Mock()
        self.rows = [
            {"name": "angle", "reg_type": "input", "address": "0x10fd", "format": "s16", "scale": .1},
            {"name": "power", "reg_type": "holding", "address": 5, "format": "s32",
             "scale": .5, "word_order": "little_endian"},
            {"name": "ieee", "title": "Voltage", "reg_type": "input", "address": 8, "format": "float"},
            {"name": "contact", "reg_type": "discrete", "address": 0, "sporadic": True},
            {"name": "polled", "reg_type": "holding", "address": 16, "readonly": False},
        ]
        self.directory = Path(directory.name)
        self.define(self.rows)

    def define(self, rows):
        self.device = {"channels": [{"channels": rows}]}
        _write_template(self.directory, "mix", self.device)

    def compose(self, **cfg):
        row = {"type": "template", "id": "probe", "address": 13,
               "port": "/dev/test-double", "template": "mix"}
        row.update(cfg)
        if row.get("transport") == "tcp":
            del row["port"]
        with patch.object(bridge, "_pollers", []):
            classic, fast, refused = bridge.compose_pollers([row], self.pub)
        self.assertEqual(refused, [])
        return next(iter(classic.values()))[0], fast

    def test_selection_admission_and_classic_fallback(self):
        for options in ({}, {"fast_modbus": True}, {"fmb_event_channels": []},
                        {"fmb_event_channels": "angle"}, {"fmb_event_channels": ["absent"]},
                        {"fmb_event_channels": ["angle", 1]},
                        {"fmb_event_channels": ["angle"], "fast_modbus": False},
                        {"fmb_event_channels": ["angle"], "transport":"tcp", "host":"192.0.2.1"}):
            with self.subTest(options=options):
                _, fast = self.compose(**options)
                self.assertFalse(fast)
        p, managers = self.compose(fmb_event_channels=["contact", "angle", "ieee", "power", "angle"])
        self.assertEqual(len(managers), 1)
        self.assertEqual(set(p.fmb_event_ranges()), {(FMB_EVT_INPUT,0x10fd,1), (FMB_EVT_INPUT,8,1),
                                                     (FMB_EVT_HOLDING,5,1), (FMB_EVT_DISCRETE,0,1)})
        self.assertEqual([p.fmb_event_width(*slot) for slot in (
            (FMB_EVT_HOLDING,5),(FMB_EVT_INPUT,0x10fd),(FMB_EVT_DISCRETE,0),
            (FMB_EVT_HOLDING,6),(FMB_EVT_INPUT,5),(FMB_EVT_HOLDING,16))], [4,2,1,None,None,None])

    def test_classic_and_event_share_scalars_and_writeback_guard(self):
        p, _ = self.compose(fmb_event_channels=["angle","ieee","power","contact","polled"])
        bank = FakeSerial({0x10fd:0xff85,5:0xfffe,6:0xffff,8:0x3fc0,9:0,16:5},discretes={0:1})
        with patch.object(p, "get_port", return_value=bank), patch.object(bridge.DeviceLiveCache,"flush_file"):
            p.poll_io()
        self.assertEqual({args[1]:args[2] for args, _ in self.pub.pub_control.call_args_list},
                         {"angle":"-12.3","power":"-1.0","ieee":"1.5","contact":"1","polled":"5"})
        self.pub.reset_mock()
        for event in ((FMB_EVT_INPUT,0x10fd,0xff85),(FMB_EVT_HOLDING,5,0xfffffffe),
                      (FMB_EVT_INPUT,8,0x3fc00000),(FMB_EVT_DISCRETE,0,1)):
            p.fmb_dispatch(*event)
        self.assertEqual({args[1]:args[2] for args, _ in self.pub.pub_control.call_args_list},
                         {"angle":"-12.3","power":"-1.0","ieee":"1.5","contact":"1"})
        self.pub.reset_mock()
        p.fmb_dispatch(FMB_EVT_INPUT, 5, 1)
        p.fmb_dispatch(FMB_EVT_HOLDING, 6, 1)
        p.fmb_dispatch(FMB_EVT_INPUT, 0x10fd, 0x10000)
        p._wb_recent["polled"] = ("9", time.monotonic() - 60)
        p.fmb_dispatch_observed(FMB_EVT_HOLDING,16,5,query_started_at=time.monotonic()-70,context=None)
        self.pub.pub_control.assert_not_called()

    def test_catalog_truthful_about_aliases_and_overlap(self):
        additional = [
            {"name":"tail", "reg_type":"holding", "address":6},
            {"name":"angle", "reg_type":"discrete", "address":3},
            {"name":"oversize", "reg_type":"input", "address":65535, "format":"s32"},
            {"name":"invalid", "reg_type":"input", "address":True},
        ]
        self.define(self.rows+additional)
        catalog=channel_catalog({"device":self.device})
        self.assertEqual([r["title"] for r in catalog if r["name"]=="ieee"], ["Voltage"])
        self.assertEqual({r["name"] for r in catalog if r["fast_eligible"]}, {"ieee","polled","contact"})
        self.assertEqual({r["name"] for r in catalog if not r["readonly"]}, {"polled"})
        p, _ = self.compose(fmb_event_channels=["power","angle","tail","ieee"])
        self.assertEqual(p.fmb_event_ranges(),[(FMB_EVT_INPUT,8,1)])

    def test_nonbinary_event_leaves_good_neighbor_and_classic_readback_intact(self):
        self.define(self.rows + [{"name": "relay", "reg_type": "coil", "address": 1}])
        poller, managers = self.compose(fmb_event_channels=["relay", "contact", "angle"])
        manager, = managers.values()
        serial = Mock()
        serial.max_read_regs = None
        ack = with_crc(bytes.fromhex("0d 46 18 01 01"))
        # Nonbinary coil and discrete values, followed by a valid signed input.
        records = bytes.fromhex("01 01 00 01 ff  01 02 00 00 02  02 04 10 fd 85 ff")
        data = with_crc(bytes([13, 0x46, 0x11, 1, 3, len(records)]) + records)
        serial.fmb_send_recv.side_effect = [ack, ack, ack, data, bytes.fromhex("fd 46 12 52 5d")]
        self.assertTrue(manager._configure_device(serial, 13, manager._devices[13]))
        _, events = manager._poll_once(serial)
        for event in events:
            manager._dispatch(*event, observation=manager._poll_observation)
        self.pub.pub_control.assert_called_once_with("probe", "angle", "-12.3")
        self.pub.pub_error.assert_called_once_with("probe", "angle", "")
        manager._poll_once(serial)
        self.assertEqual(serial.fmb_send_recv.call_args.args[0][5:7], bytes([13, 1]))

        bank = FakeSerial(discretes={0: 1}, coils={1: 0})
        with patch.object(poller, "get_port", return_value=bank), patch.object(bridge.DeviceLiveCache, "flush_file"):
            poller.poll_io()
        self.pub.pub_control.assert_any_call("probe", "contact", "1")
        self.pub.pub_control.assert_any_call("probe", "relay", "0")

    def test_unsafe_names_are_skipped_before_classic_event_and_writeback_registration(self):
        names = ['relay/on', 'relay/meta', 'relay/meta/type', '+', 'room#1', 'bad\x00name',
                 'bad\x85', 'bad\ud800', 'bad\ufdd0', 'bad\U0001ffff', 'He said "on"']
        self.define([{'name': name, 'reg_type': 'holding', 'address': i, 'readonly': False}
                     for i, name in enumerate(names)])
        catalog = channel_catalog({'device': self.device})
        valid = names[-1]
        self.assertEqual([row['name'] for row in catalog], [valid])
        with self.assertLogs('dev.probe', level='WARNING') as warnings:
            poller, managers = self.compose(fmb_event_channels=names)
        self.assertTrue(any('relay/on' in msg and 'MQTT control ID' in msg for msg in warnings.output))
        self.assertTrue(managers)
        self.assertEqual(poller.fmb_event_ranges(), [(FMB_EVT_HOLDING, len(names) - 1, 1)])
        poller.setup()
        self.assertEqual([c.args[:2] for c in self.pub.subscribe_writeback.call_args_list], [('probe', valid)])
        with patch.object(poller, 'get_port', return_value=FakeSerial()), patch.object(bridge.DeviceLiveCache, 'flush_file'):
            poller.poll_io()
        self.pub.pub_control.assert_called_once_with('probe', valid, '0')


if __name__ == "__main__":
    unittest.main()
