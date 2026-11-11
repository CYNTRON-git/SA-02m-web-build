"""Serial responses to the real composer's FC46 requests, up to MQTT publish."""
import itertools
import unittest
from collections import deque
from unittest import mock

from test_fmb_event_parsers import bridge, legacy_record, wb_record
from test_mr02m_ai_register_truth import FakeModule
import bridge_mqtt
from bridge_serial import _append_crc


# Firmware module signatures and physical DO/DI/AO/AI counts.
PROFILES = {
    1: (6, 8, 0, 0), 2: (16, 0, 0, 0), 3: (0, 0, 12, 0),
    4: (6, 0, 0, 0), 5: (0, 14, 0, 0), 6: (0, 0, 6, 6),
    7: (0, 0, 0, 12), 8: (4, 6, 0, 0), 10: (0, 10, 0, 0),
    11: (6, 5, 2, 0), 12: (0, 0, 2, 6), 15: (4, 6, 4, 0),
}


class EventLine:
    """The modeled devices send events only for successfully subscribed registers."""

    def __init__(self, legacy=()):
        self.legacy = set(legacy)
        self.enabled = set()
        self.replies = deque()
        self.polls = []

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        assert frame[-2:] == bridge.crc16(frame[:-2]).to_bytes(2, "little")
        addr, fc, sub = frame[:3]
        assert fc == 0x46
        if sub == 0x10:
            assert addr == 0xfd
            self.polls.append((frame[3], frame[5], frame[6]))
            return self.replies.popleft()
        assert sub == 0x18 and frame[3] == len(frame) - 6
        kind, start, count = frame[4], int.from_bytes(frame[5:7], "big"), frame[7]
        if addr in self.legacy:
            assert frame[3] == 5 and frame[8] == 1
            mask = bytes([0])
            selected = range(count)
        else:
            # LOW (1) per register, or 0 = event off (DTV 23/24 since 1.0.7.36):
            # the post-apply mask carries a bit only for the enabled ones.
            settings = frame[8:-2]
            assert frame[3] == 4 + count and set(settings) <= {0, 1} and any(settings)
            kind -= 1
            if kind == 3 and start == 403:
                assert settings == bytes([1]) * count
                selected = range(0, count, 7)
                masks = {36: '8140201008', 78: '81402010080402814020'}
                mask = bytes.fromhex(masks[count])
            else:
                selected = [i for i in range(count) if settings[i]]
                mask = bytes(sum(1 << j for j in range(8) if n + j in selected)
                             for n in range(0, count, 8))
        self.enabled.update((addr, kind, start + i) for i in selected)
        return _append_crc(bytes([addr, fc, sub, len(mask)]) + mask)

    def emit(self, addr, values):
        records = []
        for kind, reg, value in values:
            if (addr, kind, reg) in self.enabled:
                records.append(legacy_record(kind, reg, value) if addr in self.legacy
                               else wb_record(kind + 1, reg, value))
        if records:
            payload = b"".join(records)
            assert len(payload) <= 128
            self.replies.append(_append_crc(bytes([addr, 0x46, 0x11, 7, len(records),
                                                    len(payload)]) + payload))
        self.replies.append(_append_crc(bytes([addr, 0x46, 0x12, 7])))


def mr_events(code, counts):
    do, di, ao, ai = counts
    rows = []
    for n in range(do):
        rows.append((2, 1 + n, n % 2, "do_%d" % (1 + n), str(n % 2)))
    for n in range(di):
        rows.append((3, 18 + n, n % 2, "di_%d" % (1 + n), str(n % 2)))
    # On .23 the combined 6DO5DI2AO and 4TO6DI use the discrete firmware
    # event producer; their AO observations still require classic polling.
    for n in range(0 if code in (11, 15) else ao):
        rows.append((2, 33 + n, 400 + n, "ao_%d" % (1 + n), str(400 + n)))
    for n in range(ai):
        rows.append((3, 403 + 7 * n, 0xffff - n, "ai_%d" % (1 + n),
                     str(round((-1 - n) * .1, 3))))
    return rows


class TestFmbLineComposition(unittest.TestCase):
    def test_each_profile_from_subscription_to_retained_mqtt_and_cache(self):
        configs = []
        cases = {}
        a, b = "/dev/COM3", "/dev/COM4"
        lines = {a: EventLine(legacy=[20]), b: EventLine()}
        for i, (code, counts) in enumerate(PROFILES.items()):
            port = (a, b)[i % 2]
            addr = i // 2 + 5  # same address on two buses, different modules
            did = "mr-%d-%d" % (code, addr)
            configs.append(dict(type="mr02m", id=did, port=port, address=addr,
                                baudrate=115200, module_type=code))
            cases[did] = mr_events(code, counts)
        configs += [
            dict(type="dtv", id="sens", port=a, address=20, baudrate=115200),
            dict(type="ce02m3", id="ce", port=b, address=20, baudrate=115200,
                 fast_modbus=True),
            dict(type="mr02m", id="tenzo", port=b, address=21, baudrate=115200,
                 module_type=9),
        ]
        cases["sens"] = [
            (0, 1, 1, "buzzer", "1"), (0, 2, 0, "leds", "0"),
            (3, 25, 78, "light_pct", "78.0"), (3, 26, 0, "input_pb2", "0.0"),
            (3, 27, 1, "presence", "1.0"), (3, 28, 13, "moving_distance", "13.0"),
            (3, 29, 17, "still_distance", "17.0"), (3, 30, 24, "detect_distance", "24.0"),
        ]
        cases["ce"] = [
            (3, 500, 2301, "voltage_a", "230.1"), (3, 501, 2310, "voltage_b", "231.0"),
            (3, 502, 2309, "voltage_c", "230.9"), (3, 510, 1234, "current_a", "1.234"),
            (3, 511, 133, "current_b", "0.133"), (3, 512, 26, "current_c", "0.026"),
            (3, 513, 8, "current_n", "0.008"),
        ]
        for name in ("_controls", "_errors", "_sensor_types"):
            guard = mock.patch.object(bridge_mqtt.DeviceLiveCache, name, {})
            guard.start()
            self.addCleanup(guard.stop)
        with mock.patch.object(bridge_mqtt.mqtt, "Client") as client, \
                mock.patch('time.monotonic', new=itertools.count(1000.0, 0.001).__next__), \
                mock.patch.object(bridge, "_pollers", []), \
                mock.patch.object(bridge, "get_port", side_effect=lambda path, baud: lines[path]):
            publisher = bridge_mqtt.MQTTPublisher({})
            buses, managers, refused = bridge.compose_pollers(configs, publisher)
            self.assertFalse(refused)
            self.assertEqual(set(managers), {a + ":115200", b + ":115200"})
            for bus, manager in managers.items():
                for poller in buses[bus]:
                    # Keep the post-handshake insurance poll from supplying the
                    # values under test. Only received FC46 records may do so.
                    poller.poll_io = mock.Mock()
                    if poller.device_id == "ce":
                        poller._fw_version = (1, 0, 6, 13)
                manager.configure_all()
                self.assertTrue(all(d["configured"] and not d["pending"]
                                    for d in manager._devices.values()))
                for poller in buses[bus]:
                    code = poller.cfg.get('module_type')
                    if code not in (6, 7, 12):
                        continue
                    poller._mod_type = code
                    poller._do, poller._di, poller._ao, poller._ai = PROFILES[code]
                    module = FakeModule({ch: 3 for ch in range(1, poller._ai + 1)})
                    with mock.patch.object(poller, 'get_port', return_value=module), \
                            mock.patch.object(poller, 'read_holding_registers', side_effect=module.read_holding):
                        poller._poll_ai_ao()
                    self.assertTrue(poller.fmb_needs_own_empty())
                    lines[poller.port_path].emit(poller.address, [])
                    self.assertEqual(manager.event_burst(), 0)
                    self.assertFalse(poller.fmb_needs_own_empty())
            bridge_mqtt.DeviceLiveCache._controls.clear()
            self.assertNotIn(21, managers[b + ":115200"]._devices)
            for cfg in configs:
                did = cfg["id"]
                if did not in cases:
                    continue
                rows = cases[did]
                with self.subTest(device=did, port=cfg["port"]):
                    line = lines[cfg["port"]]
                    line.emit(cfg["address"], [row[:3] for row in rows])
                    client.return_value.publish.reset_mock()
                    self.assertEqual(managers[cfg["port"] + ":115200"].event_burst(), 0)
                    expected = {name: value for _, _, _, name, value in rows}
                    self.assertEqual(bridge_mqtt.DeviceLiveCache._controls.get(did), expected)
                    calls = [c for c in client.return_value.publish.call_args_list
                             if "/meta/" not in c.args[0]]
                    self.assertEqual([(c.args, c.kwargs) for c in calls],
                                     [(("/devices/%s/controls/%s" % (did, name), value),
                                       {"qos": 1, "retain": True})
                                      for name, value in expected.items()])
                    self.assertEqual(line.polls[-1][1:], (cfg["address"], 7))
                    if cfg.get("module_type") in (11, 15):
                        poller = next(p for p in buses[cfg["port"] + ":115200"]
                                      if p.device_id == did)
                        code = cfg["module_type"]
                        poller._mod_type = code
                        poller._do, poller._di, poller._ao, poller._ai = PROFILES[code]
                        values = list(range(401, 401 + poller._ao))
                        with mock.patch.object(poller, "read_holding_registers",
                                               return_value=values) as read:
                            poller._poll_ai_ao()
                        read.assert_called_once_with(cfg["address"], 33, len(values))
                        observed = bridge_mqtt.DeviceLiveCache._controls[did]
                        self.assertEqual([observed["ao_%d" % i]
                                          for i in range(1, len(values) + 1)],
                                         [str(v) for v in values])

    def test_disabled_channels_ignore_fast_events_for_all_four_io_kinds(self):
        from test_fmb_events import FakePub
        for code, types in ((1, [(2, 1), (0, 1), (3, 18)]),
                            (6, [(2, 33), (3, 403)])):
            with self.subTest(code=code):
                pub = FakePub()
                channels = {kind: [{"ch": 1, "enabled": False}]
                            for kind in ("do", "di", "ao", "ai")}
                poller = bridge.MR02mPoller({"id": "disabled", "address": 2,
                                             "module_type": code, "channels": channels}, pub)
                poller.fmb_event_ranges()
                for kind, address in types:
                    poller.fmb_dispatch(kind, address, 1)
                self.assertEqual(pub.controls, [])
                self.assertEqual(pub.errors, [])


if __name__ == "__main__":
    unittest.main()
