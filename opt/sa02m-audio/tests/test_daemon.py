"""The daemon as a whole (contract §MQTT, §Носители, §Хранение, §Жизненный
цикл): the real service loop, the real mqttio.Link over an in-memory paho,
the real Capture through the real `timeout` around a fake arecord on PATH,
on an eMMC temp store and on a fake mounted removable medium."""

from __future__ import annotations

import json
import os
import time
import unittest
import wave
from unittest import mock

from sa02m_audio import constants as C
from sa02m_audio import service
from sa02m_audio.capture import MicLease

from . import POSIX, POSIX_REASON
from .fakes import DaemonHarness, Sandbox, wait_for

REC_ON = "/devices/sa02m-audio/controls/record/on"


@unittest.skipUnless(POSIX, POSIX_REASON)
class DaemonTestBase(unittest.TestCase):
    storage = "emmc"

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.sb.write_conf(storage=self.storage)
        self.h = None

    def harness(self, **kwargs) -> DaemonHarness:
        self.h = DaemonHarness(self.sb, **kwargs)
        self.addCleanup(self.h.stop)
        return self.h

    def record(self, payload="2", timeout=15.0):
        before = len(self.h.paho.values("last_status"))
        self.h.command("record", payload)
        ok = wait_for(lambda: len(self.h.paho.values("last_status")) > before, timeout)
        self.assertTrue(ok, "no last_status after record/on %r" % payload)
        return self.h.last("last_status")

    def event_kinds(self):
        return [e["event"] for e in self.h.events()]


class EmmcFlowTest(DaemonTestBase):
    def test_command_file_retained_state_events_delete(self):
        h = self.harness().start()
        retained = h.paho.retained()
        self.assertEqual(retained["/devices/sa02m-audio/meta/error"], "")
        self.assertEqual(retained["/devices/sa02m-audio/controls/record"], "0")
        self.assertEqual(retained["/devices/sa02m-audio/controls/mic"], "present")
        self.assertEqual(retained["/devices/sa02m-audio/controls/medium"], "emmc")
        self.assertEqual(retained["/devices/sa02m-audio/controls/medium_state"], "ok")
        self.assertEqual(h.paho.will, ("/devices/sa02m-audio/meta/error", "r", 1, True))
        self.assertEqual(sorted(h.paho.subscribed), sorted([
            REC_ON, "/devices/sa02m-audio/controls/delete/on"]))

        self.assertEqual(self.record("2"), "ok")
        self.assertTrue(h.wait_idle_after_record())
        rec_id = h.last("last_file")
        self.assertRegex(rec_id, C.ID_RE)
        self.assertEqual(self.sb.clips(), [rec_id + ".wav"])
        self.assertEqual(os.listdir(self.sb.tmp()), [])
        with wave.open(os.path.join(self.sb.rec(), rec_id + ".wav"), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (16000, 1, 32000))
        self.assertEqual(h.last("last_duration_ms"), "2000")
        self.assertEqual(h.last("last_size"), "64044")
        self.assertEqual(h.last("last_peak_dbfs"), "-6")
        self.assertEqual(h.last("last_upload"), "off")
        self.assertEqual(h.last("count"), "1")
        # every state publish is retained; nothing was ever published on a command topic
        for topic, _payload, retain in h.paho.snapshot():
            self.assertTrue(retain, topic)
            self.assertFalse(topic.endswith("/on"), topic)
        with open(h.argv_log) as fh:
            self.assertIn("-D plughw:CARD=Microphone,DEV=0 -f S16_LE -r 16000 -c 1 -t raw", fh.read())
        kinds = self.event_kinds()
        self.assertEqual(kinds[-2:], ["start", "end"])
        status = h.status()
        self.assertEqual((status["state"], status["count"], status["last_file"]), ("idle", 1, rec_id))
        self.assertEqual(status["media"]["emmc"]["files"], 1)

        h.command("delete", rec_id)
        self.assertTrue(wait_for(lambda: self.sb.clips() == []))
        self.assertTrue(h.wait_last("count", "0"))
        self.assertEqual(self.event_kinds()[-1], "delete")

        self.assertEqual(h.stop(), 0)
        self.assertEqual(h.paho.retained()["/devices/sa02m-audio/meta/error"], "r")
        self.assertEqual(h.status()["state"], "stopped")

    def test_delete_all(self):
        h = self.harness().start()
        for _ in range(2):
            self.assertEqual(self.record("2"), "ok")
            self.assertTrue(h.wait_idle_after_record())
        self.assertEqual(len(self.sb.clips()), 2)
        h.command("delete", "all")
        self.assertTrue(wait_for(lambda: self.sb.clips() == []))

    def test_retained_command_is_ignored_and_cleared(self):
        h = self.harness().start()
        h.command("record", "2", retain=True)
        self.assertTrue(wait_for(lambda: (REC_ON, "", True) in h.paho.snapshot()))
        time.sleep(0.6)
        self.assertNotIn("1", h.paho.values("record"))
        self.assertEqual(self.sb.clips(), [])
        self.assertIn("mqtt", self.event_kinds())
        # The clear comes back as an empty, non-retained message: ignored, not bad_command.
        h.command("record", "")
        time.sleep(0.6)
        self.assertEqual(h.paho.values("last_status"), [])

    def test_bad_commands_record_nothing(self):
        h = self.harness().start()
        for payload in ("60", "31", "abc", "1.5", "{\"s\":5}"):
            with self.subTest(payload=payload):
                self.assertEqual(self.record(payload), "bad_command")
        self.assertNotIn("1", h.paho.values("record"))
        self.assertEqual(self.sb.clips(), [])
        h.command("delete", "../etc")
        self.assertTrue(wait_for(lambda: len(h.paho.values("last_status")) == 6))
        self.assertEqual(h.last("last_status"), "bad_command")

    def test_busy_while_recording(self):
        h = self.harness(realtime=True).start()
        h.command("record", "3")
        self.assertTrue(wait_for(lambda: "1" in h.paho.values("record")))
        self.assertEqual(self.record("2"), "busy")
        self.assertTrue(h.wait_last("last_status", "ok", 15))
        self.assertEqual(len(self.sb.clips()), 1)
        self.assertEqual(h.last("last_duration_ms"), "3000")

    def test_stop_early_keeps_a_clip_of_at_least_one_second(self):
        h = self.harness(realtime=True).start()
        h.command("record", "10")
        self.assertTrue(wait_for(lambda: "1" in h.paho.values("record")))
        time.sleep(1.6)
        h.command("record", "stop")
        self.assertTrue(h.wait_last("last_status", "ok", 10))
        self.assertGreaterEqual(int(h.last("last_duration_ms")), 1000)
        self.assertLess(int(h.last("last_duration_ms")), 10000)

    def test_stop_before_one_second_discards(self):
        h = self.harness(realtime=True).start()
        h.command("record", "10")
        self.assertTrue(wait_for(lambda: "1" in h.paho.values("record")))
        h.command("record", "0")
        self.assertTrue(h.wait_last("last_status", "interrupted", 10))
        self.assertEqual(self.sb.clips(), [])
        self.assertEqual(os.listdir(self.sb.tmp()), [])
        self.assertIn("discarded", self.event_kinds())

    def test_rate_limit(self):
        h = self.harness()
        h.daemon.rate = service.RateLimit(h.daemon.mono, limit=2)
        h.start()
        for _ in range(2):
            self.assertEqual(self.record("2"), "ok")
            self.assertTrue(h.wait_idle_after_record())
        self.assertEqual(self.record("2"), "rate_limited")
        self.assertEqual(len(self.sb.clips()), 2)

    def test_no_mic(self):
        self.sb.set_cards(" 0 [Codec          ]: H3_Audio_Codec - H3 Audio Codec\n", capture=(0,))
        h = self.harness().start()
        self.assertEqual(h.last("mic"), "absent")
        self.assertEqual(self.record("2"), "no_mic")
        self.assertEqual(self.sb.clips(), [])

    def test_mic_lease_held_by_another_opener(self):
        other = MicLease(self.sb.mic_lock)
        self.assertTrue(other.acquire())
        self.addCleanup(other.release)
        h = self.harness().start()
        self.assertEqual(h.last("mic"), "busy")
        self.assertEqual(self.record("2"), "mic_busy")
        self.assertEqual(self.sb.clips(), [])
        other.release()
        self.assertEqual(self.record("2"), "ok")

    def test_device_busy_without_the_lease(self):
        """An opener that ignored the lease: arecord says «busy»."""
        h = self.harness(mode="busy").start()
        self.assertEqual(self.record("2"), "mic_busy")
        self.assertTrue(h.wait_last("mic", "busy"))
        self.assertEqual(self.sb.clips(), [])
        self.assertEqual(os.listdir(self.sb.tmp()), [])

    def test_storage_full(self):
        media = tuple(m._replace(floor_bytes=1 << 62) for m in self.sb.media)
        h = self.harness(media=media).start()
        self.assertEqual(h.last("medium_state"), "full")
        self.assertEqual(self.record("2"), "storage_full")
        self.assertEqual(self.sb.clips(), [])

    def test_startup_part_cleanup_reports_interrupted(self):
        os.makedirs(self.sb.tmp())
        os.makedirs(self.sb.rec())
        part = os.path.join(self.sb.tmp(), "20261015T120000Z-abcdef.wav.part")
        open(part, "wb").close()
        h = self.harness().start()
        self.assertTrue(h.wait_last("last_status", "interrupted"))
        self.assertFalse(os.path.exists(part))
        self.assertIn("interrupted", self.event_kinds())

    def test_shutdown_mid_recording_is_interrupted(self):
        h = self.harness(realtime=True).start()
        h.command("record", "20")
        self.assertTrue(wait_for(lambda: "1" in h.paho.values("record")))
        self.assertEqual(h.stop(), 0)
        self.assertEqual(h.last("record"), "0")
        self.assertEqual(h.last("last_status"), "interrupted")
        self.assertEqual(os.listdir(self.sb.tmp()), [])
        self.assertEqual(self.sb.clips(), [])

    def test_unknown_storage_records_on_emmc_with_an_event(self):
        self.sb.write_conf(storage="nfs")
        h = self.harness().start()
        self.assertEqual(h.last("medium"), "emmc")
        events = [e for e in h.events() if e["event"] == "config"]
        self.assertTrue(events and "'nfs'" in events[0]["detail"], h.events())
        self.assertEqual(self.record("2"), "ok")
        self.assertEqual(len(self.sb.clips()), 1)

    def test_retention_after_each_recording(self):
        self.sb.write_conf(keep_files=1)
        h = self.harness().start()
        for _ in range(3):
            self.assertEqual(self.record("2"), "ok")
            self.assertTrue(h.wait_idle_after_record())
        self.assertEqual(len(self.sb.clips()), 1)
        self.assertEqual(self.sb.clips(), [h.last("last_file") + ".wav"])
        self.assertIn("retention", self.event_kinds())

    def test_events_ring_is_bounded(self):
        log = service.EventLog(self.sb.events, time.time)
        for i in range(C.EVENTS_MAX + 5):
            log.add("refused", status="busy", seconds=i, detail="x" * 300, bogus="dropped")
        with open(self.sb.events, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh.read().splitlines()]
        self.assertEqual(len(lines), C.EVENTS_MAX)
        self.assertEqual(lines[0]["seconds"], 5)
        for line in lines:
            self.assertLessEqual(set(line), set(service.EVENT_KEYS))
            self.assertEqual(len(line["detail"]), 200)
        self.assertEqual(len(service.EventLog(self.sb.events, time.time).events), C.EVENTS_MAX)
        with self.assertRaises(ValueError):
            log.add("made_up")


class LifecycleTest(DaemonTestBase):
    def test_disabled_exits_zero_and_says_so(self):
        self.sb.write_conf(enabled=False)
        h = self.harness().start(wait_ready=False)
        h.thread.join(10)
        self.assertEqual(h.exit_code, 0)
        self.assertEqual(h.status()["state"], "disabled")
        self.assertEqual(h.paho.snapshot(), [])

    def test_conf_unreadable_records_nothing(self):
        os.unlink(self.sb.conf)
        os.mkdir(self.sb.conf)
        h = self.harness().start(wait_ready=False)
        h.thread.join(10)
        self.assertEqual(h.exit_code, 0)
        self.assertEqual(h.status()["state"], "conf_unreadable")
        self.assertEqual(h.paho.snapshot(), [])

    def test_missing_paho_is_a_standby_exit(self):
        h = self.harness(check_dependencies=lambda: ["paho.mqtt.client"]).start(wait_ready=False)
        h.thread.join(10)
        self.assertEqual(h.exit_code, 0)
        self.assertEqual(h.status()["state"], "missing_deps")

    def test_second_copy_exits_75(self):
        first = self.harness().start()
        second = DaemonHarness(self.sb)
        self.assertEqual(second.daemon.run(), C.EXIT_LOCKED)
        self.assertEqual(second.paho.snapshot(), [])
        self.assertEqual(first.last("record"), "0")

    def test_disable_while_running_exits(self):
        h = self.harness().start()
        self.sb.write_conf(enabled=False)
        h.thread.join(C.CONF_POLL_S * 3 + 5)
        self.assertFalse(h.thread.is_alive())
        self.assertEqual(h.exit_code, 0)
        self.assertEqual(h.status()["state"], "disabled")

    def test_status_keys_are_the_declared_set(self):
        h = self.harness().start()
        status = h.status()
        self.assertEqual(set(status), set(service.STATUS_KEYS))
        self.assertEqual(set(status["media"]), set(C.MEDIUM_NAMES))
        for verdict in status["media"].values():
            self.assertEqual(set(verdict), set(service.MEDIA_KEYS))


class RemovableFlowTest(DaemonTestBase):
    storage = "usb"

    def test_records_onto_the_mounted_medium_only(self):
        self.sb.mount(self.sb.usb)
        h = self.harness().start()
        self.assertEqual(h.last("medium"), "usb")
        self.assertEqual(h.last("medium_state"), "ok")
        self.assertEqual(self.record("2"), "ok")
        rec_id = h.last("last_file")
        self.assertEqual(self.sb.clips("usb"), [rec_id + ".wav"])
        self.assertEqual(self.sb.clips("emmc"), [])
        self.assertEqual(h.status()["media"]["usb"]["files"], 1)
        h.command("delete", "all")
        self.assertTrue(wait_for(lambda: self.sb.clips("usb") == []))

    def test_phantom_dir_refuses_and_writes_nowhere(self):
        os.chmod(self.sb.usb, 0o777)
        h = self.harness().start()
        self.assertEqual(h.last("medium_state"), "absent")
        self.assertEqual(self.record("2"), "medium_absent")
        self.assertEqual(os.listdir(self.sb.usb), [])
        self.assertEqual(self.sb.clips("emmc"), [])
        self.assertFalse(os.path.exists(self.sb.tmp("emmc")) and os.listdir(self.sb.tmp("emmc")))

    def test_readonly_medium(self):
        self.sb.mount(self.sb.usb, options="ro,relatime")
        h = self.harness().start()
        self.assertEqual(h.last("medium_state"), "readonly")
        self.assertEqual(self.record("2"), "medium_readonly")
        self.assertEqual(os.listdir(self.sb.usb), [])

    def test_medium_lost_between_capture_and_rename(self):
        self.sb.mount(self.sb.usb)
        h = self.harness(realtime=True).start()
        h.command("record", "3")
        self.assertTrue(wait_for(lambda: "1" in h.paho.values("record")))
        self.sb.unmount(self.sb.usb)  # lazily unmounted: the held fds still reach it
        self.assertTrue(h.wait_last("last_status", "medium_lost", 15))
        self.assertEqual(self.sb.clips("usb"), [])
        self.assertEqual(self.sb.clips("emmc"), [])
        self.assertEqual(h.last("record"), "0")

    def test_hot_insert_is_noticed_and_old_clips_age_out(self):
        patcher = mock.patch.object(C, "MEDIA_CHECK_S", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)
        h = self.harness()
        h.start()
        self.assertEqual(self.record("2"), "medium_absent")
        # a stick carrying an old clip and a torn part from another board
        os.makedirs(self.sb.rec("usb"))
        os.makedirs(self.sb.tmp("usb"))
        old = os.path.join(self.sb.rec("usb"), "20200101T000000Z-aaaaaa.wav")
        with open(old, "wb") as fh:
            fh.write(b"RIFF")
        t = time.time() - 48 * 3600
        os.utime(old, (t, t))
        open(os.path.join(self.sb.tmp("usb"), "20200101T000001Z-bbbbbb.wav.part"), "wb").close()
        self.sb.mount(self.sb.usb)  # noticed by the periodic check (shortened to 0.3 s)
        self.assertTrue(h.wait_last("medium_state", "ok"))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(os.listdir(self.sb.tmp("usb")), [])
        self.assertEqual(h.last("last_status"), "interrupted")
        self.assertEqual(self.record("2"), "ok")

    def test_retention_sweeps_a_present_medium_that_is_not_selected(self):
        self.sb.write_conf(storage="emmc")
        self.sb.mount(self.sb.usb)
        os.makedirs(self.sb.rec("usb"))
        old = os.path.join(self.sb.rec("usb"), "20200101T000000Z-cccccc.wav")
        with open(old, "wb") as fh:
            fh.write(b"RIFF")
        t = time.time() - 48 * 3600
        os.utime(old, (t, t))
        self.harness().start()
        self.assertTrue(wait_for(lambda: not os.path.exists(old)))


if __name__ == "__main__":
    unittest.main()
