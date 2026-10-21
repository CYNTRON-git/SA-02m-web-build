"""JSON command replies (contract §Передача): shape, allow list, one publish
per clip id, a device outside the list is not written."""

from __future__ import annotations

import json
import os
import queue
import unittest

from sa02m_audio import mqttio
from sa02m_audio import service
from sa02m_audio import voice
from sa02m_audio.upload import UploadResult

from . import POSIX, POSIX_REASON
from .fakes import DaemonHarness, FakePaho, Sandbox, wait_for

REC = "20261016T120000Z-abc123"
LED = {"device": "led-COM3-13", "control": "brightness", "value": "40"}
POWER = {"device": "led-COM3-13", "control": "power", "value": "1"}
FOREIGN = {"device": "mr02m-COM4-11", "control": "do_3", "value": "1"}

ALLOW = {
    "devices": {
        "led-COM3-13": {
            "power": {"min": 0, "max": 1},
            "brightness": {"min": 0, "max": 255},
        }
    }
}


class BodyTest(unittest.TestCase):
    def test_non_json_and_mismatch_are_empty(self):
        self.assertEqual(voice.commands_from_body(b"ok", REC), ())
        self.assertEqual(voice.commands_from_body(b"[1,2]", REC), ())
        body = json.dumps({"recording_id": "other", "commands": [POWER]}).encode()
        self.assertEqual(voice.commands_from_body(body, REC), ())

    def test_present_recording_id_must_be_matching_string(self):
        for rid in (123, False, {}, [], None, "", "other"):
            with self.subTest(rid=rid):
                body = json.dumps({"recording_id": rid, "commands": [POWER]}).encode()
                self.assertEqual(voice.commands_from_body(body, REC), ())
        for doc in ({"commands": [POWER]}, {"recording_id": REC, "commands": [POWER]}):
            self.assertEqual(voice.commands_from_body(json.dumps(doc).encode(), REC), (POWER,))

    def test_int_value_is_accepted_as_decimal(self):
        body = json.dumps({"commands": [{"device": "led-COM3-13", "control": "brightness", "value": 40}]}).encode()
        self.assertEqual(voice.commands_from_body(body, REC), (LED,))


class ApplyTest(unittest.TestCase):
    def allow(self):
        return voice.load_allow(self.path)

    def setUp(self):
        self.td = Sandbox()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.etc_dir, "voice-allow.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(ALLOW, fh)
        self.link = _Link()
        self.seen = voice.SeenIds()

    def test_listed_command_is_published_once(self):
        status, detail = voice.apply_commands(self.link, REC, (POWER, LED), self.allow(), self.seen)
        self.assertEqual(status, "ok")
        self.assertEqual(detail, "applied 2")
        self.assertEqual(self.link.sent, [
            ("/devices/led-COM3-13/controls/power/on", "1"),
            ("/devices/led-COM3-13/controls/brightness/on", "40"),
        ])
        again, detail = voice.apply_commands(self.link, REC, (POWER,), self.allow(), self.seen)
        self.assertEqual((again, detail, self.link.sent), ("ok", "duplicate recording id", self.link.sent))

    def test_foreign_device_is_not_published(self):
        status, detail = voice.apply_commands(self.link, REC, (FOREIGN, LED), self.allow(), self.seen)
        self.assertEqual(status, "ok")
        self.assertIn("rejected 1", detail)
        self.assertEqual([t for t, _ in self.link.sent], ["/devices/led-COM3-13/controls/brightness/on"])

    def test_missing_allow_publishes_nothing(self):
        os.remove(self.path)
        status, _ = voice.apply_commands(self.link, REC, (LED,), voice.load_allow(self.path), self.seen)
        self.assertEqual((status, self.link.sent), ("refused", []))

    def test_out_of_range_is_refused(self):
        hot = {"device": "led-COM3-13", "control": "brightness", "value": "256"}
        status, _ = voice.apply_commands(self.link, REC, (hot,), self.allow(), self.seen)
        self.assertEqual((status, self.link.sent), ("refused", []))


class LinkPublishTest(unittest.TestCase):
    def test_command_is_not_retained_and_bad_topics_are_refused(self):
        paho = FakePaho()
        link = mqttio.Link(queue.Queue(), client_factory=lambda: paho)
        self.assertFalse(link.publish_command("/devices/led-COM3-13/controls/power/on", "1"))
        link.start()
        self.assertTrue(link.publish_command("/devices/led-COM3-13/controls/brightness/on", "40"))
        self.assertFalse(link.publish_command("/devices/led-COM3-13/controls/brightness", "40"))
        self.assertFalse(link.publish_command("/devices/sa02m-audio/controls/record", "1"))
        self.assertEqual(
            [item for item in paho.snapshot() if not item[2]],
            [("/devices/led-COM3-13/controls/brightness/on", "40", False)],
        )


class DaemonApplyTest(unittest.TestCase):
    @unittest.skipUnless(POSIX, POSIX_REASON)
    def test_invalid_utf8_refuses_result_and_loop_processes_next_result(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        sb.write_conf(voice_commands=True)
        allow_path = os.path.join(sb.etc_dir, "voice-allow.json")
        with open(allow_path, "wb") as fh:
            fh.write(b"\xff")
        harness = DaemonHarness(sb)
        self.addCleanup(harness.stop)
        harness.start()
        harness.daemon.results.put(UploadResult(REC, "emmc", "ok", "HTTP 200", 1000, -12, (POWER,)))
        self.assertTrue(wait_for(lambda: any(e.get("event") == "voice" for e in harness.events())))
        events = [e for e in harness.events() if e.get("event") == "voice"]
        self.assertEqual(events[-1]["status"], "refused")
        self.assertEqual([p for p in harness.paho.snapshot() if p[0].endswith("/on")], [])
        self.assertTrue(harness.thread.is_alive())
        with open(allow_path, "w", encoding="utf-8") as fh:
            json.dump(ALLOW, fh)
        harness.daemon.results.put(UploadResult(REC, "emmc", "ok", "HTTP 200", 1000, -12, (POWER,)))
        self.assertTrue(wait_for(lambda: any(t.endswith("/power/on") for t, _, _ in harness.paho.snapshot())))
        self.assertTrue(harness.thread.is_alive())

    def test_flag_off_does_not_publish(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        daemon = service.Daemon(conf_path=sb.conf, media=sb.media, status_file=sb.status,
                                events_file=sb.events, daemon_lock=sb.lock, mic_lock=sb.mic_lock,
                                mounts_file=sb.mounts, root=sb.root, dev_of=sb.dev_of,
                                asound_dir=sb.asound, check_dependencies=lambda: [])
        link = _Link()
        daemon.link = link
        daemon.conf.voice_commands = False
        daemon.apply_voice(UploadResult(REC, "emmc", "ok", "h: HTTP 200", 1000, -12, (LED,)))
        self.assertEqual(link.sent, [])

    def test_flag_on_publishes_through_the_daemon_once(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        with open(os.path.join(sb.etc_dir, "voice-allow.json"), "w", encoding="utf-8") as fh:
            json.dump(ALLOW, fh)
        daemon = service.Daemon(conf_path=sb.conf, media=sb.media, status_file=sb.status,
                                events_file=sb.events, daemon_lock=sb.lock, mic_lock=sb.mic_lock,
                                mounts_file=sb.mounts, root=sb.root, dev_of=sb.dev_of,
                                asound_dir=sb.asound, check_dependencies=lambda: [])
        daemon.events = service.EventLog(sb.events, lambda: 1)
        link = _Link()
        daemon.link = link
        daemon.conf.voice_commands = True
        result = UploadResult(REC, "emmc", "ok", "h: HTTP 200", 1000, -12, (POWER, FOREIGN))
        daemon.apply_voice(result)
        daemon.apply_voice(result)
        self.assertEqual(link.sent, [("/devices/led-COM3-13/controls/power/on", "1")])
        kinds = [e["event"] for e in daemon.events.events]
        self.assertEqual(kinds, ["voice", "voice"])


class _Link:
    def __init__(self):
        self.connected = True
        self.sent = []

    def publish_command(self, topic, payload):
        self.sent.append((topic, str(payload)))
        return True


if __name__ == "__main__":
    unittest.main()
