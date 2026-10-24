"""Light music in the daemon (sa02m_audio.service + capture; contract
§Светомузыка): no strip selected ⇒ `disabled` with the switch at 0 and
nothing written anywhere; the default style switches the strip to the level
bar (effect 75) right before the first `music_level` write; the configured
rate reaches the sink; the light-music stream is the unbounded 16 kHz
short-buffer stream; and a recording joins that stream — no second arecord,
no pause of the light music, the stream kept until the clip ends when light
music goes off mid-clip.

Runs off POSIX too: the daemon's methods are driven directly with a fake
link, a fake stream, a fake mic lease and plain-file writes (the POSIX-only
dir_fd / flock paths are the daemon suite's, test_daemon.py)."""

from __future__ import annotations

import io
import json
import math
import os
import queue
import threading
import time
import unittest
import wave
from array import array
from unittest import mock

from sa02m_audio import capture as cap
from sa02m_audio import config as conf_mod
from sa02m_audio import constants as C
from sa02m_audio import mqttio
from sa02m_audio import service
from sa02m_audio.lightshow import Sink
from sa02m_audio.media import Verdict

from .fakes import Sandbox

TARGET = "led-COM3-13"
PREFIX = "/devices/%s/controls/" % TARGET
STRIP = {"power": "0", "effect": "12", "speed": "128", "color": "#102030", "fx_aux": "513",
         "brightness": "200", "scene_source": "manual"}
FRAME = C.STREAM_BLOCK  # 1600 bytes = 50 ms of the 16 kHz stream


def tone_block(index, freq_hz=200, amp=12000):
    n = FRAME // 2
    return array("h", [int(round(amp * math.sin(2.0 * math.pi * freq_hz * (index * n + i) / C.SAMPLE_RATE)))
                       for i in range(n)]).tobytes()


def plain_write(path, data, *, mode=0o640, durable=True):
    """atomic_write without dir_fd (absent off POSIX): the light-music logic
    under test does not depend on how the file lands."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data.encode("utf-8") if isinstance(data, str) else data)


class FakeLink:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.retained = {}
        self.puts = []
        self.commands = []
        self.extra = ()

    def put(self, topic, payload, force=False):
        with self.lock:
            self.retained[topic] = payload
            self.puts.append((topic, payload))

    def publish_command(self, topic, payload):
        with self.lock:
            self.commands.append((topic, payload))
        return True

    def set_extra_topics(self, topics):
        self.extra = tuple(topics)

    def clear_retained_command(self, topic):
        pass

    def state(self, control):
        return self.retained.get(mqttio.control_topic(control))

    def strip_writes(self):
        with self.lock:
            return [(t[len(PREFIX):-len("/on")], p) for t, p in self.commands if t.startswith(PREFIX)]


class FakeLease:
    def __init__(self) -> None:
        self.fd = None
        self.acquired = 0

    def acquire(self):
        if self.fd is not None:
            return False  # an flock is exclusive, also against ourselves
        self.fd = 1
        self.acquired += 1
        return True

    def release(self):
        self.fd = None

    @property
    def held(self):
        return self.fd is not None


class FrameClock:
    """The sink's monotonic clock: advanced one 50 ms frame per pushed block,
    so the rate limiter sees stream time, not test wall time."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeStream:
    """capture.Stream's surface as the daemon uses it; `push` plays the
    reader thread (synchronously), one 50 ms frame per block."""

    def __init__(self, device, clock=None) -> None:
        self.device = device
        self.clock = clock
        self.subs = []
        self.ended = threading.Event()
        self.last_data = time.monotonic()
        self.started = False
        self.stopped = False
        self.fed = 0

    def start(self, subscribers=(), on_spawn=None):
        self.started = True
        self.last_data = time.monotonic()

    def attach(self, sub):
        if self.ended.is_set():
            return False
        self.subs.append(sub)
        return True

    def detach(self, sub):
        if sub in self.subs:
            self.subs.remove(sub)

    @property
    def subscribers(self):
        return len(self.subs)

    def stderr_text(self):
        return ""

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass

    def push(self, block):
        if self.clock is not None:
            self.clock.t += len(block) / C.BYTES_PER_S
        self.last_data = time.monotonic()
        self.fed += len(block)
        for sub in list(self.subs):
            sub.feed(block)


class FakePart:
    """store.Part's surface for a StreamClip: a plain temp file."""

    def __init__(self, path, rec_id) -> None:
        self.path = path
        self.id = rec_id
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        self.fd = os.open(path, flags, 0o640)
        self.abandoned = False
        self.closed = False

    def remember_written(self, fd):
        pass

    def abandon(self):
        self.abandoned = True

    def close(self):
        self.closed = True


class LightMusicDaemonBase(unittest.TestCase):
    conf_values = {"light_music_target": TARGET}

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.sb.write_conf(**self.conf_values)
        patcher = mock.patch.object(service, "atomic_write", plain_write)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.streams = []
        self.captures = []
        self.clock = FrameClock()
        # The real Sink, on the stream's frame clock instead of time.monotonic.
        sink_patch = mock.patch.object(
            service, "Sink", lambda *a, **kw: Sink(*a, mono=self.clock, **kw))
        sink_patch.start()
        self.addCleanup(sink_patch.stop)

        def stream_factory(device):
            s = FakeStream(device, self.clock)
            self.streams.append(s)
            return s

        def capture_factory(*args, **kwargs):
            self.captures.append(args)
            raise AssertionError("a second arecord was spawned while light music runs")

        sb = self.sb
        self.d = service.Daemon(
            conf_path=sb.conf, media=sb.media, mounts_file=sb.mounts, root=sb.root,
            dev_of=sb.dev_of, asound_dir=sb.asound, mic_lock=sb.mic_lock,
            status_file=sb.status, events_file=sb.events, daemon_lock=sb.lock,
            check_dependencies=lambda: [], stream_factory=stream_factory,
            capture_factory=capture_factory)
        self.d.conf = conf_mod.load(sb.conf)
        self.link = FakeLink()
        self.d.link = self.link
        self.d.lease = FakeLease()
        # Store / medium reads are dir_fd-based (POSIX); the light-music paths
        # under test only need «the medium is fine» and a part to write.
        self.d.store.summary = lambda name: (0, 0, None)
        self.d.probe.verdict = lambda name, probe=True, **kw: Verdict(name, C.MEDIUM_OK, 1 << 30, 1, "")
        self.parts = []

        def open_part(name, rec_id):
            part = FakePart(os.path.join(sb.root, rec_id + ".wav.part"), rec_id)
            self.parts.append(part)
            return part

        self.d.store.open_part = open_part

    def deliver(self, topic, payload, retain=False):
        self.d.handle(mqttio.Inbound(topic, payload, retain))

    def strip_reports(self, values=STRIP):
        self.deliver("/devices/%s/meta/error" % TARGET, "")
        for control, value in values.items():
            self.deliver(PREFIX + control, value)

    def start_light_music(self):
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.strip_reports()
        self.assertEqual(self.d.lm_state, C.LM_RUNNING)
        return self.streams[-1]


class NoStripSelectedTest(LightMusicDaemonBase):
    conf_values = {}

    def test_no_target_reads_disabled_and_the_switch_stays_off(self):
        """No strip selected: light_music_state `disabled` («лента не
        выбрана»), `light_music` 0; a `1` on the switch starts nothing and
        writes to no strip; the switch topic is not even subscribed."""
        d = self.d
        self.assertEqual(d.conf.light_music_target, "")
        d.light_update()
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.deliver("/devices/%s/meta/error" % TARGET, "")
        for control, value in STRIP.items():
            self.deliver(PREFIX + control, value)
        d.light_update()
        self.assertEqual(d.lm_state, C.LM_DISABLED)
        self.assertFalse(d.lm_wanted)
        self.assertNotEqual(self.link.state("light_music"), "1")
        self.assertEqual(self.link.commands, [])
        self.assertEqual(self.streams, [])
        self.assertIsNone(d.sink)
        self.assertNotIn(mqttio.LIGHT_TOPIC, d._light_topics())
        self.assertEqual(d._light_topics(), ())
        status = d.write_status(C.STATE_IDLE)
        self.assertEqual((status["light_music"], status["light_music_state"],
                          status["light_music_update_hz"]), (False, C.LM_DISABLED, 0.0))


class SwitchTest(LightMusicDaemonBase):
    def test_target_set_and_switched_off_reads_off(self):
        self.d.light_update()
        self.assertEqual(self.d.lm_state, C.LM_OFF)
        self.assertIn(mqttio.LIGHT_TOPIC, self.d._light_topics())
        self.deliver(mqttio.LIGHT_TOPIC, "0")
        self.assertEqual(self.d.lm_state, C.LM_OFF)
        self.assertEqual(self.link.state("light_music"), "0")
        self.assertEqual(self.streams, [])

    def test_waits_for_the_strips_state_before_switching_it(self):
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.assertEqual(self.d.lm_state, C.LM_STRIP_OFFLINE)
        self.assertEqual(self.streams, [])
        self.assertEqual(self.link.commands, [])

    def test_clearing_the_target_turns_it_off_and_restores_the_strip(self):
        stream = self.start_light_music()
        for i in range(5):
            stream.push(tone_block(i))
        self.sb.write_conf()  # no light_music_target
        self.assertTrue(self.d.reload_conf())
        self.assertEqual(self.d.lm_state, C.LM_DISABLED)
        self.assertEqual(self.link.state("light_music"), "0")
        self.assertTrue(stream.stopped)
        self.assertFalse(self.d.lease.held)
        writes = self.link.strip_writes()
        self.assertEqual(writes[-len(STRIP):], [
            ("power", "0"), ("effect", "12"), ("fx_aux", "513"), ("speed", "128"),
            ("color", "#102030"), ("brightness", "200"), ("scene_source", "manual")])


class StripAvailabilityTest(LightMusicDaemonBase):
    """The bridge's device-level `meta/error` under the WB convention
    (docs/MQTT_TOPICS.md): `r` = offline; cleared = an EMPTY retained publish,
    which deletes the retained message — a subscriber that connects after the
    clear (the normal boot order: bridge first, audio daemon later) receives
    no `meta/error` at all. Found on bench 1.135 (2026-10-24): `led-COM3-13`
    live with its controls retained and no `meta/error` topic, light music
    stuck in `strip_offline` forever."""

    def report_controls(self, values=STRIP):
        for control, value in values.items():
            self.deliver(PREFIX + control, value, retain=True)

    def test_late_subscriber_with_no_meta_error_and_the_strips_state_starts(self):
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.report_controls()  # retained state only: no meta/error ever arrives
        self.assertTrue(self.d._led_online(TARGET))
        self.assertEqual(self.d.lm_state, C.LM_RUNNING)
        self.assertEqual(len(self.streams), 1)
        self.assertEqual(self.d.lm_session["snapshot"], STRIP)

    def test_meta_error_r_is_offline_even_with_the_state_retained(self):
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.deliver("/devices/%s/meta/error" % TARGET, "r", retain=True)
        self.report_controls()
        self.assertFalse(self.d._led_online(TARGET))
        self.assertEqual(self.d.lm_state, C.LM_STRIP_OFFLINE)
        self.assertEqual(self.streams, [])
        self.assertEqual(self.link.commands, [])

    def test_r_cleared_live_brings_the_strip_back(self):
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.deliver("/devices/%s/meta/error" % TARGET, "r", retain=True)
        self.report_controls()
        self.assertEqual(self.d.lm_state, C.LM_STRIP_OFFLINE)
        self.deliver("/devices/%s/meta/error" % TARGET, "")  # the bridge's clear, live
        self.assertEqual(self.d.lm_state, C.LM_RUNNING)

    def test_no_state_at_all_is_not_online(self):
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.d.light_update()
        self.assertFalse(self.d._led_online(TARGET))
        self.assertEqual(self.d.lm_state, C.LM_STRIP_OFFLINE)
        self.assertEqual(self.streams, [])

    def test_a_partial_state_is_not_online(self):
        """Every required snapshot control must have been reported: a strip
        missing one (here the slow-polled `fx_aux`) is still waited for."""
        partial = {k: v for k, v in STRIP.items() if k != "fx_aux"}
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.report_controls(partial)
        self.assertFalse(self.d._led_online(TARGET))
        self.assertEqual(self.d.lm_state, C.LM_STRIP_OFFLINE)
        self.report_controls({"fx_aux": STRIP["fx_aux"]})
        self.assertEqual(self.d.lm_state, C.LM_RUNNING)

    def test_an_empty_control_payload_is_not_a_report(self):
        """A zero-length message deletes a retained topic: it says nothing
        about the strip being there."""
        self.deliver(mqttio.LIGHT_TOPIC, "1")
        self.report_controls({k: "" for k in STRIP})
        self.assertFalse(self.d._led_online(TARGET))
        self.assertEqual(self.d.lm_state, C.LM_STRIP_OFFLINE)

    def test_meta_error_is_subscribed_before_the_controls(self):
        """mosquitto hands a new subscription's retained messages over in the
        order the subscriptions were made, so a retained `r` reaches the
        daemon before the strip's retained controls could make it online."""
        topics = self.d._light_topics()
        prefix = "/devices/%s/" % TARGET
        self.assertLess(topics.index(prefix + "meta/error"), topics.index(prefix + "controls/+"))
        self.assertLess(topics.index(prefix + "meta/error"),
                        topics.index(prefix + "controls/+/meta/error"))

    def write_crash_session(self):
        plain_write(self.d.light_session_file,
                    json.dumps({"target": TARGET, "snapshot": STRIP, "switched": True}))
        self.d._load_session()

    def test_crash_session_restored_to_a_late_subscribed_strip(self):
        self.write_crash_session()
        self.d.light_update()
        self.assertEqual(self.link.commands, [])  # nothing known yet: wait
        self.assertTrue(os.path.exists(self.d.light_session_file))
        self.report_controls({k: "0" if k == "power" else v for k, v in STRIP.items()})
        writes = self.link.strip_writes()
        self.assertEqual(writes[-1], ("scene_source", "manual"))
        self.assertIn(("effect", "12"), writes)
        self.assertIsNone(self.d.lm_session)
        self.assertFalse(os.path.exists(self.d.light_session_file))

    def test_crash_session_not_restored_to_an_offline_strip(self):
        self.write_crash_session()
        self.deliver("/devices/%s/meta/error" % TARGET, "r", retain=True)
        self.report_controls()
        self.assertEqual(self.link.commands, [])
        self.assertIsNotNone(self.d.lm_session)
        self.assertTrue(os.path.exists(self.d.light_session_file))


class DefaultStyleTest(LightMusicDaemonBase):
    def test_level_bar_effect_75_then_music_level_values(self):
        self.assertEqual(self.d.conf.light_music_style, C.LM_STYLE_LEVEL)
        stream = self.start_light_music()
        self.assertEqual(self.link.commands, [])  # nothing before the first audio frame
        for i in range(20):
            stream.push(tone_block(i))
        writes = self.link.strip_writes()
        self.assertEqual(writes[:2], [("power", "1"), ("effect", "75")])
        self.assertGreater(len(writes), 2)
        for control, value in writes[2:]:
            self.assertEqual(control, "music_level")
            self.assertRegex(value, r"^(100|[1-9]?[0-9])\Z")
        status = self.d.write_status(C.STATE_IDLE)
        self.assertEqual((status["light_music"], status["light_music_state"]), (True, C.LM_RUNNING))
        self.assertEqual(self.d.lm_style, C.LM_STYLE_LEVEL)


class RateFromConfTest(LightMusicDaemonBase):
    conf_values = {"light_music_target": TARGET, "light_music_rate_hz": 3}

    def test_the_configured_rate_reaches_the_sink(self):
        self.start_light_music()
        self.assertEqual(self.d.sink._limiter.max_hz, 3.0)

    def test_an_out_of_range_rate_is_clamped_to_10(self):
        self.sb.write_conf(light_music_target=TARGET, light_music_rate_hz=50)
        self.d.conf = conf_mod.load(self.sb.conf)
        self.assertEqual(self.d.conf.light_music_rate_hz, C.LIGHT_MUSIC_RATE_MAX)
        self.start_light_music()
        self.assertEqual(self.d.sink._limiter.max_hz, 10.0)


class StreamShapeTest(unittest.TestCase):
    def test_light_music_stream_is_unbounded_16k_mono_short_buffer(self):
        argv = cap.stream_argv("plughw:CARD=Microphone,DEV=0")
        self.assertEqual(argv[0], C.ARECORD)  # no `timeout`: it runs until stopped
        self.assertNotIn(C.TIMEOUT, argv)
        joined = " ".join(argv)
        self.assertIn("-f S16_LE -r 16000 -c 1 -t raw", joined)
        for flag in C.ARECORD_BUFFER_FLAGS:
            self.assertIn(flag, argv)
        self.assertIs(service.Daemon.__init__.__kwdefaults__["stream_factory"], cap.Stream)


class SharedStreamRecordingTest(LightMusicDaemonBase):
    def test_record_joins_the_light_music_stream_without_a_pause(self):
        """record/on while light music runs: the clip is a second subscriber
        of the SAME stream — no second arecord, the mic lease not re-taken,
        the strip still updated while the clip is written."""
        d = self.d
        stream = self.start_light_music()
        for i in range(10):
            stream.push(tone_block(i))
        sink = d.sink
        lease_takes = d.lease.acquired
        d.start(2)
        self.assertEqual(self.captures, [])
        self.assertIsInstance(d.capture, cap.StreamClip)
        self.assertIs(d.capture.stream, stream)
        self.assertEqual(d.lease.acquired, lease_takes)
        self.assertEqual(stream.subs, [sink, d.capture])
        self.assertEqual(self.link.state("record"), "1")
        before = len(self.link.strip_writes())
        for i in range(10, 10 + 2 * 20 + 5):  # 2 s of clip + 0.25 s more
            stream.push(tone_block(i, freq_hz=300 + 20 * (i % 9), amp=2000 + 900 * (i % 11)))
        self.assertGreater(len(self.link.strip_writes()), before, "light music paused during the clip")
        result = d.results.get_nowait()
        self.assertEqual(result.outcome, cap.OUT_OK)
        self.assertEqual(result.data_bytes, 2 * C.BYTES_PER_S)
        self.assertEqual(stream.subs, [sink])  # the clip left, the sink stayed
        with wave.open(self.parts[-1].path, "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()),
                             (16000, 1, 2 * C.SAMPLE_RATE))
        with mock.patch.object(d, "_store_clip", return_value=C.ST_OK):
            d.finalize(result)
        self.assertIs(d.stream, stream)
        self.assertFalse(stream.stopped)
        self.assertTrue(d.lease.held)
        self.assertIs(d.sink, sink)
        self.assertEqual(d.lm_state, C.LM_RUNNING)
        self.assertEqual(self.link.state("record"), "0")

    def test_light_music_off_mid_clip_keeps_the_stream_until_the_clip_ends(self):
        d = self.d
        stream = self.start_light_music()
        for i in range(5):
            stream.push(tone_block(i))
        d.start(2)
        clip = d.capture
        for i in range(5, 15):
            stream.push(tone_block(i))
        self.deliver(mqttio.LIGHT_TOPIC, "0")
        self.assertEqual(d.lm_state, C.LM_OFF)
        self.assertIsNone(d.sink)
        self.assertFalse(stream.stopped, "the stream was stopped under a running clip")
        self.assertIs(d.stream, stream)
        self.assertEqual(stream.subs, [clip])
        restored = self.link.strip_writes()
        self.assertEqual(restored[-1], ("scene_source", "manual"))
        for i in range(15, 60):
            stream.push(tone_block(i))
        self.assertEqual(self.link.strip_writes(), restored, "a stream write after the restore")
        result = d.results.get_nowait()
        self.assertEqual((result.outcome, result.data_bytes), (cap.OUT_OK, 2 * C.BYTES_PER_S))
        with mock.patch.object(d, "_store_clip", return_value=C.ST_OK):
            d.finalize(result)
        self.assertTrue(stream.stopped)
        self.assertIsNone(d.stream)
        self.assertFalse(d.lease.held)


class _FakeProc:
    def __init__(self, data: bytes) -> None:
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")
        self.pid = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class RealStreamFanOutTest(unittest.TestCase):
    def test_one_stream_feeds_the_sink_and_a_clip_every_block(self):
        """capture.Stream's reader thread hands the same 16 kHz blocks to the
        light-music sink and to a clip: 2 s of audio → 40 sink frames, a 1 s
        WAV, both from one arecord child."""
        data = b"".join(tone_block(i) for i in range(40))
        spawned = []

        def popen(argv, **kwargs):
            spawned.append(argv)
            return _FakeProc(data)

        tmp = Sandbox()
        self.addCleanup(tmp.cleanup)
        stream = cap.Stream("plughw:CARD=Microphone,DEV=0", popen=popen)
        values = []
        sink = Sink(C.LM_STYLE_LEVEL, 10, values.append)
        part = FakePart(os.path.join(tmp.root, "clip.wav.part"), "clip")
        results = queue.Queue()
        clip = cap.StreamClip(stream, 1, part, results.put)
        clip.open()
        stream.start(subscribers=(sink, clip))
        stream.join(10)
        self.assertTrue(stream.ended.wait(10))
        self.assertEqual(len(spawned), 1)
        self.assertEqual(sink.frames, 40)
        self.assertTrue(values)
        result = results.get(timeout=5)
        self.assertEqual((result.outcome, result.data_bytes), (cap.OUT_OK, C.BYTES_PER_S))
        with wave.open(part.path, "rb") as w:
            self.assertEqual(w.getnframes(), C.SAMPLE_RATE)


if __name__ == "__main__":
    unittest.main()
