"""Exercise actual Modbus decoding before accepting MR live state or writes."""
from __future__ import annotations

import struct
import ast
import os
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from sa02m_flasher import device_config as dc, module_profiles as mp, modbus_rtu as rtu


def frame(body: bytes) -> bytes:
    return body + struct.pack("<H", rtu.crc16_modbus(body))


class Bus:
    def __init__(self, *, kind=2, live=True, on=False, ack=True):
        self.kind, self.live, self.on, self.ack = kind, live, on, ack
        self.calls = []
        self.closed = 0
        self.coil_reply = None
        self.missing = set()

    def close(self):
        self.closed += 1

    def __call__(self, request, timeout_ms=0):
        self.calls.append(bytes(request))
        slave, fc = request[:2]
        start, count = struct.unpack(">HH", request[2:6])
        if not self.live or (fc, start) in self.missing:
            return b""
        if fc == 5:
            return request if self.ack else b""
        if fc in (1, 2):
            if self.coil_reply is not None:
                return self.coil_reply
            data = bytes([int(self.on)]) + bytes((count + 7) // 8 - 1)
        elif fc in (3, 4):
            values = [0] * count
            if fc == 4 and start == 0:
                values[0] = self.kind
            if fc == 4 and start == 1:
                values[0] = int(self.on)
            data = struct.pack(">" + "H" * count, *values)
        else:
            raise AssertionError("Unexpected request")
        return frame(bytes([slave, fc, len(data)]) + data)


def device(kind=2):
    return {"address": 2, "baudrate": 9600, "signature": mp.MP02_TYPE_NAMES[kind],
            "module_type": kind, "serial": 123}


class TestMrReadbackTruth(unittest.TestCase):
    def test_valid_coils_and_input_fallback_preserve_on_and_off(self):
        for on in (False, True):
            for fallback in (False, True):
                with self.subTest(on=on, fallback=fallback):
                    bus = Bus(on=on)
                    if fallback:
                        bus.missing.add((1, 1))
                    self.assertEqual(dc._read_do_bits(bus, 2, 16), [on] + [False] * 15)

    def test_unavailable_or_truncated_coils_never_fabricate_off(self):
        replies = [b"", frame(b"\x02\x01\x01\x00"),
                   frame(b"\x03\x01\x02\x00\x00"), b"\x02\x01\x02\x00\x00\x00\x00"]
        for reply in replies:
            with self.subTest(reply=reply.hex()):
                bus = Bus()
                bus.coil_reply = reply
                bus.missing.add((4, 1))
                with self.assertRaises(RuntimeError):
                    dc._read_do_bits(bus, 2, 16)

    def test_short_input_fallback_does_not_pad_missing_channels(self):
        def send(request, timeout=0):
            return frame(b"\x02\x04\x02\x00\x00") if request[1] == 4 else b""
        with self.assertRaises(RuntimeError):
            dc._read_do_bits(send, 2, 16)

    def test_absent_module_snapshot_and_off_command_both_fail(self):
        for action in ("snapshot", "off", "on"):
            with self.subTest(action=action):
                bus = Bus(live=False)
                with patch.object(dc, "_open_transport", return_value=(bus, bus.close)):
                    with self.assertRaises(RuntimeError):
                        if action == "snapshot":
                            dc.snapshot_for_device("unused", device(), snapshot_detail="minimal")
                        else:
                            dc.write_allowed_coil("unused", device(), 1, action == "on")
                self.assertEqual(bus.closed, 1)
                self.assertLessEqual(sum(req[1] == 5 for req in bus.calls), 1)

    def test_ack_loss_requires_valid_matching_readback_without_second_write(self):
        for wanted in (False, True):
            for actual in (False, True):
                with self.subTest(wanted=wanted, actual=actual):
                    bus = Bus(on=actual, ack=False)
                    with patch.object(dc, "_open_transport", return_value=(bus, bus.close)):
                        if wanted == actual:
                            result = dc.write_allowed_coil("unused", device(), 1, wanted)
                            self.assertEqual(result["mr"]["do"]["bits"][0], actual)
                            self.assertEqual(bus.closed, 2)
                        else:
                            with self.assertRaises(RuntimeError):
                                dc.write_allowed_coil("unused", device(), 1, wanted)
                            self.assertEqual(bus.closed, 1)
                    self.assertEqual(sum(req[1] == 5 for req in bus.calls), 1)

    def test_required_live_blocks_reject_missing_data(self):
        for kind, missing in ((5, (4, 18)), (3, (4, 33)), (7, (3, 400)), (7, (4, 400))):
            for detail in ("full", "minimal", "panel"):
                # Minimal has no AI measurement request; it must still prove
                # the channel configuration exists before presenting channels.
                if missing == (4, 400) and detail == "minimal":
                    continue
                with self.subTest(kind=kind, missing=missing, detail=detail):
                    bus = Bus(kind=kind)
                    bus.missing.add(missing)
                    with patch.object(dc, "_open_transport", return_value=(bus, bus.close)):
                        with self.assertRaises(RuntimeError):
                            dc.snapshot_for_device("unused", device(kind), snapshot_detail=detail, active_tab="ai_1")

    def test_optional_counter_failure_is_unknown_not_zero(self):
        self.assertEqual(dc._read_u32_pairs(lambda *_: b"", 2, 45, 16), [])
        bus = Bus()
        self.assertEqual(dc._read_u32_pairs(bus, 2, 45, 16), [0] * 16)
        self.assertEqual(dc._read_bit_mirror(lambda *_: b"", 2, 630, 8), [])


class TestMrReadbackHttp(unittest.TestCase):
    from tests import test_csrf_dispatch as csrf
    setUp = csrf.TestCsrfDispatch.setUp
    tearDown = csrf.TestCsrfDispatch.tearDown
    _req = csrf.TestCsrfDispatch._req

    def test_real_dispatch_lease_and_codec_never_return_success_for_absent_module(self):
        csrf = self.csrf
        tree = ast.parse(csrf._SERVICE_SRC)
        handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        nodes = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        nodes.update({n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)})
        entered, exited = [], []

        @contextmanager
        def lease(path, services, *, preserve_released):
            self.assertTrue(preserve_released)
            entered.append(path)
            try:
                yield
            finally:
                exited.append(path)

        @contextmanager
        def lock(port):
            self.assertEqual(port, "COM4")
            yield

        ns = {**csrf._NS, "os": os, "device_config": dc, "port_lease": lease,
              "_device_config_port_lock": lock, "_guard_no_active_flasher_job": lambda *a, **kw: None}
        names = ("_read_json_body", "_device_config_request", "_run_device_config_modbus",
                 "_handle_device_config_snapshot", "_handle_device_config_coil")
        exec("from __future__ import annotations\n" + "\n".join(csrf._src_of(nodes[n]) for n in names), ns)
        actual = type("MrHandler", (csrf._StubHandler,), {n: ns[n] for n in names if n != "_read_json_body"})
        self.srv.RequestHandlerClass = actual
        self.ctx.cfg.ports_map = {"COM4": str(self.sdir)}
        self.ctx.cfg.mplc_stop_services = []
        for route in ("snapshot", "coil"):
            for live in (False, True):
                with self.subTest(route=route, live=live):
                    bus = Bus(live=live)
                    body = {"port": "COM4", "device": device(), "coil": 1, "value": False}
                    with patch.object(dc, "_open_transport", return_value=(bus, bus.close)):
                        status, data = self._req("POST", "/device_config/" + route, csrf=csrf._CSRF, body=body)
                    self.assertEqual(status, 200 if live else 409, data)
                    self.assertEqual(entered, exited)
                    if live:
                        self.assertIs(data["ok"], True)
                        self.assertEqual(data["mr"]["do"]["bits"], [False] * 16)
                    else:
                        self.assertIsNot(data.get("ok"), True)
                        self.assertIn("readback", data["error"])
                    self.assertLessEqual(sum(req[1] == 5 for req in bus.calls), 1)


if __name__ == "__main__":
    unittest.main()
