"""`record/on` and `delete/on` grammar (contract §MQTT): closed, every
refusal named, nothing clamped."""

from __future__ import annotations

import unittest

from sa02m_audio import constants as C
from sa02m_audio import mqttio

from . import AUDIO_ROOT  # noqa: F401  (sys.path)


def rec(payload, default_s=10, max_s=30):
    return mqttio.parse_record(payload, default_s=default_s, max_s=max_s)


class RecordGrammarTest(unittest.TestCase):
    def test_start_words_mean_the_default_duration(self):
        for word in ("1", "on", "true", "ON", "True", " 1 ", "1\n"):
            with self.subTest(word=word):
                self.assertEqual(rec(word, default_s=7), (mqttio.CMD_START, 7))

    def test_integers_in_range_are_seconds(self):
        for n in (2, 3, 10, 29, 30):
            with self.subTest(n=n):
                self.assertEqual(rec(str(n)), (mqttio.CMD_START, n))
        self.assertEqual(rec("05"), (mqttio.CMD_START, 5))

    def test_stop_words(self):
        for word in ("0", "off", "false", "stop", "STOP", "Off"):
            with self.subTest(word=word):
                self.assertEqual(rec(word), (mqttio.CMD_STOP, 0))

    def test_above_the_max_is_refused_never_clamped(self):
        self.assertEqual(rec("31"), (mqttio.CMD_BAD, 0))
        self.assertEqual(rec("60"), (mqttio.CMD_BAD, 0))
        # A lowered conf max refuses what the hard ceiling would allow.
        self.assertEqual(rec("20", max_s=15), (mqttio.CMD_BAD, 0))
        self.assertEqual(rec("15", max_s=15), (mqttio.CMD_START, 15))

    def test_every_other_payload_is_bad_command(self):
        for payload in ("-5", "1.5", "2s", "abc", "{\"seconds\": 5}", "{'seconds': 5}",
                        "1 2", "999999999", "0x10", "ten", "yes", "+5", "٥"):
            with self.subTest(payload=payload):
                self.assertEqual(rec(payload), (mqttio.CMD_BAD, 0))

    def test_empty_payload_is_ignored_not_refused(self):
        # The daemon's own clear of a retained command comes back as "".
        for payload in ("", "   ", "\n"):
            with self.subTest(payload=payload):
                self.assertEqual(rec(payload), (mqttio.CMD_IGNORE, 0))

    def test_one_means_default_not_one_second(self):
        # The rules engine maps True/1/"on" to "1"; 1 s is below MIN_DURATION_S anyway.
        self.assertEqual(C.MIN_DURATION_S, 2)
        self.assertEqual(rec("1"), (mqttio.CMD_START, 10))


class DeleteGrammarTest(unittest.TestCase):
    def test_id_and_all(self):
        self.assertEqual(mqttio.parse_delete("20261015T120000Z-0a1b2c"),
                         (mqttio.CMD_DELETE, "20261015T120000Z-0a1b2c"))
        self.assertEqual(mqttio.parse_delete("all"), (mqttio.CMD_DELETE, "all"))

    def test_everything_else_is_bad(self):
        for payload in ("ALL", "*", "../x", "20261015T120000Z-0A1B2C", "20261015T120000Z-0a1b2",
                        "20261015T120000Z-0a1b2c.wav", "20261015T120000Z-0a1b2c/..", "1"):
            with self.subTest(payload=payload):
                self.assertEqual(mqttio.parse_delete(payload), (mqttio.CMD_BAD, ""))
        self.assertEqual(mqttio.parse_delete(""), (mqttio.CMD_IGNORE, ""))


class TopicsTest(unittest.TestCase):
    def test_command_topics_are_exactly_three(self):
        self.assertEqual(set(mqttio.COMMAND_TOPICS), {
            "/devices/sa02m-audio/controls/record/on",
            "/devices/sa02m-audio/controls/delete/on",
            "/devices/sa02m-audio/controls/light_music/on"})
        # `light_music/on` is subscribed only while light music has a target.
        self.assertEqual(set(mqttio.ALWAYS_TOPICS), {
            "/devices/sa02m-audio/controls/record/on",
            "/devices/sa02m-audio/controls/delete/on"})

    def test_meta_marks_only_record_and_light_music_writable(self):
        meta = dict(mqttio.meta_topics())
        self.assertEqual(meta["/devices/sa02m-audio/meta/driver"], "sa02m-audio")
        for name in ("record", "light_music"):
            self.assertEqual(meta["/devices/sa02m-audio/controls/%s/meta/readonly" % name], "0",
                             name)
        for control in C.CONTROLS:
            if control.name not in ("record", "light_music"):
                self.assertEqual(meta["/devices/sa02m-audio/controls/%s/meta/readonly" % control.name],
                                 "1", control.name)
        self.assertFalse(any(t.endswith("/on") for t in meta))


if __name__ == "__main__":
    unittest.main()
