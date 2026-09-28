"""sse.py: the line parser, and the reader thread against the fake stream."""

from __future__ import annotations

import os
import queue
import tempfile
import time
import unittest

from sa02m_homeconnect import constants as C
from sa02m_homeconnect.budget import Budget
from sa02m_homeconnect.oauth import OAuthClient
from sa02m_homeconnect.sse import (
    STREAM_DOWN, STREAM_EVENT, STREAM_UP, EventStream, ProtocolError, SseParser,
)
from sa02m_homeconnect.token_store import TokenStore
from sa02m_homeconnect.transport import Transport

from .fake_bsh import FakeBSH, sse_event

HA = "BOSCH-WAT28400-68A40E000001"


def parse(text: str):
    p = SseParser()
    out = []
    for line in text.split("\n"):
        ev = p.feed_line(line)
        if ev is not None:
            out.append(ev)
    return out


class ParserTest(unittest.TestCase):
    def test_fields_multiline_data_and_comments(self) -> None:
        evs = parse(": comment\nevent: status\nid: X1\ndata: {\"items\":\ndata: []}\n\n")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].type, "STATUS")
        self.assertEqual(evs[0].id, "X1")
        self.assertEqual(evs[0].data, {"items": []})

    def test_keep_alive_without_data(self) -> None:
        evs = parse("event: KEEP-ALIVE\ndata: \n\n")
        self.assertEqual(evs[0].type, "KEEP-ALIVE")
        self.assertIsNone(evs[0].data)

    def test_missing_id_is_filled_from_data(self) -> None:
        evs = parse(sse_event("DISCONNECTED", {"haId": HA, "key": "BSH.Common.Appliance.Disconnected"}))
        self.assertEqual(evs[0].id, HA)
        evs = parse(sse_event("NOTIFY", {"items": [{"key": "k", "value": 1, "haId": HA}]}))
        self.assertEqual(evs[0].id, HA)

    def test_blank_lines_alone_dispatch_nothing(self) -> None:
        self.assertEqual(parse("\n\n\n"), [])

    def test_non_json_data_kept_as_none(self) -> None:
        self.assertIsNone(parse("event: STATUS\ndata: not json\n\n")[0].data)

    def test_oversize_line_and_event_are_protocol_errors(self) -> None:
        p = SseParser()
        with self.assertRaises(ProtocolError):
            p.feed_line("data: " + "x" * C.SSE_MAX_LINE)
        p = SseParser()
        with self.assertRaises(ProtocolError):
            for _ in range(C.SSE_MAX_EVENT // 1000 + 2):
                p.feed_line("data: " + "y" * 1000)


class StreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeBSH()
        self.base = self.fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.budget = Budget(os.path.join(self.tmp.name, "budget.json"))
        self.transport = Transport(self.base, allow_loopback_http=True, timeout=5)
        self.oauth = OAuthClient(self.transport, self.budget,
                                 TokenStore(os.path.join(self.tmp.name, "tokens.json")),
                                 client_id="CLIENT_ID_FOR_TESTS_0001", host="api")
        self.oauth.poll(self.oauth.start_device_flow())
        self.out: "queue.Queue" = queue.Queue()
        self.stream = None

    def tearDown(self) -> None:
        if self.stream is not None:
            self.stream.stop()
        self.fake.stop()
        self.tmp.cleanup()

    def make(self, **kw) -> EventStream:
        args = dict(read_timeout=2.0, backoff_min=0.05, backoff_max=0.2, rng=lambda: 0.5)
        args.update(kw)
        self.stream = EventStream(self.transport, self.budget, self.oauth, self.out, **args)
        return self.stream

    def collect(self, until, timeout: float = 5.0):
        got = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                got.append(self.out.get(timeout=0.05))
            except queue.Empty:
                pass
            if until(got):
                return got
        self.fail("stream messages never satisfied the condition: %r" % got)

    def test_events_then_eof_then_reconnect(self) -> None:
        self.fake.sse_scripts = [
            [sse_event("KEEP-ALIVE"),
             sse_event("STATUS", {"items": [{"key": "BSH.Common.Status.DoorState",
                                             "value": "BSH.Common.EnumType.DoorState.Open",
                                             "haId": HA}]}, HA)],
            [sse_event("DISCONNECTED", {"haId": HA}), "HANG"],
        ]
        self.make().start()
        got = self.collect(lambda g: sum(1 for k, _ in g if k == STREAM_UP) >= 2
                           and any(k == STREAM_EVENT and p.type == "DISCONNECTED" for k, p in g))
        kinds = [k for k, _ in got]
        self.assertEqual(kinds[0], STREAM_UP)
        events = [p for k, p in got if k == STREAM_EVENT]
        self.assertEqual([e.type for e in events], ["KEEP-ALIVE", "STATUS", "DISCONNECTED"])
        self.assertEqual(events[2].id, HA)  # filled from data.haId
        self.assertIn((STREAM_DOWN, "EOFError"), got)
        self.assertEqual(self.budget.by_kind["sse"], 2)
        req = self.fake.calls(C.EVENTS_PATH)[0]
        self.assertEqual(req["headers"]["accept"], "text/event-stream")
        self.assertEqual(req["headers"]["authorization"], "Bearer AT-1")

    def test_silent_socket_past_keepalive_deadline_reconnects(self) -> None:
        self.fake.sse_scripts = [["HANG"], ["HANG"]]
        self.make(read_timeout=0.3).start()
        got = self.collect(lambda g: (STREAM_DOWN, "keepalive_timeout") in g
                           and sum(1 for k, _ in g if k == STREAM_UP) >= 2)
        self.assertGreaterEqual(self.fake.sse_connections, 2)

    def test_401_refreshes_once_and_reconnects(self) -> None:
        self.fake.overrides[C.EVENTS_PATH] = [(401, {}, {"error": {"key": "invalid_token"}})]
        self.fake.sse_scripts = [["HANG"]]
        self.make().start()
        self.collect(lambda g: (STREAM_UP, None) in g)
        self.assertEqual(len(self.fake.grants("refresh_token")), 1)

    def test_oversize_line_reconnects(self) -> None:
        self.fake.sse_scripts = [["data: " + "z" * (C.SSE_MAX_LINE + 10) + "\n\n"], ["HANG"]]
        self.make().start()
        got = self.collect(lambda g: sum(1 for k, _ in g if k == STREAM_UP) >= 2)
        self.assertIn((STREAM_DOWN, "ProtocolError"), got)

    def test_backoff_grows_between_failed_connects(self) -> None:
        self.fake.overrides[C.EVENTS_PATH] = [(503, {}, {})] * 3
        self.fake.sse_scripts = [["HANG"]]
        waits = []
        stream = self.make(backoff_min=0.02, backoff_max=10.0)
        real_wait = stream.stop_event.wait

        def recording_wait(t=None):
            waits.append(t)
            return real_wait(min(t, 0.05) if t else t)

        stream.stop_event.wait = recording_wait  # type: ignore[assignment]
        stream.start()
        self.collect(lambda g: (STREAM_UP, None) in g)
        self.assertEqual(waits[:3], [0.02, 0.04, 0.08])

    def test_stop_interrupts_a_hanging_read(self) -> None:
        self.fake.sse_scripts = [["HANG"]]
        stream = self.make(read_timeout=30)
        stream.start()
        self.collect(lambda g: (STREAM_UP, None) in g)
        t0 = time.time()
        stream.stop(join_s=5)
        self.assertLess(time.time() - t0, 3)
        self.assertFalse(stream.alive)


if __name__ == "__main__":
    unittest.main()
