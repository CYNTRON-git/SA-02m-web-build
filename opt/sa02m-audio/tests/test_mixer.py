"""Capture gain of the USB mic (contract §6 «Усиление захвата», §8
`capture_gain`): the mixer half (`sa02m_audio.mixer`) against a fake amixer,
and the daemon half (`service.Daemon` gain sync) with `subprocess.run`
replaced, so nothing here needs ALSA, POSIX or a board."""

from __future__ import annotations

import subprocess
import types
import unittest
from unittest import mock

from sa02m_audio import config
from sa02m_audio import constants as C
from sa02m_audio import mixer
from sa02m_audio import mqttio
from sa02m_audio import service

from .fakes import Sandbox

# The Fifine bench mic: one capture-only control.
FIFINE = ({"name": "Mic", "caps": "cvolume cvolume-joined cswitch cswitch-joined",
           "lo": 0, "hi": 16, "values": [12]},)


def _db(value: int) -> str:
    return "%.2f" % (value * 1.5)


class FakeAmixer:
    """`amixer -c <card> -- scontents | sset <name>,<idx> capture <raw>` over an
    in-memory control list. A `runner` for mixer.* and, via `subprocess_run`,
    a stand-in for subprocess.run that also records every call's argv and
    kwargs. `sticky` = sset answers but the value does not move; `fail` = every
    call exits non-zero; `timeout` = every call raises TimeoutExpired."""

    def __init__(self, controls, sticky=False, fail=False, timeout=False):
        self.controls = [dict(c, index=c.get("index", 0), values=list(c["values"]))
                         for c in controls]
        self.sticky, self.fail, self.timeout = sticky, fail, timeout
        self.calls = []  # (argv, kwargs)

    def verbs(self):
        return [argv[argv.index("--") + 1] for argv, _kw in self.calls]

    def render(self, ctls) -> str:
        out = []
        for c in ctls:
            out.append("Simple mixer control '%s',%d" % (c["name"], c["index"]))
            out.append("  Capabilities: %s" % c["caps"])
            if "cvolume" in c["caps"].split():
                out.append("  Capture channels: Mono")
                out.append("  Limits: Capture %d - %d" % (c["lo"], c["hi"]))
                for v in c["values"]:
                    out.append("  Mono: Capture %d [50%%] [%sdB] [on]" % (v, _db(v)))
            else:
                out.append("  Playback channels: Front Left - Front Right")
                out.append("  Limits: Playback %d - %d" % (c["lo"], c["hi"]))
                out.append("  Mono:")
                for v in c["values"]:
                    out.append("  Front Left: Playback %d [50%%] [%sdB] [on]" % (v, _db(v)))
        return "\n".join(out) + "\n"

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append((argv, {}))
        return self._answer(argv)

    def _answer(self, argv):
        if self.fail:
            return None
        verb = argv[argv.index("--") + 1]
        if verb == "scontents":
            return self.render(self.controls)
        name, index = argv[argv.index("--") + 2].rsplit(",", 1)
        for c in self.controls:
            if (c["name"], c["index"]) == (name, int(index)):
                if not self.sticky:
                    c["values"] = [int(argv[-1])] * len(c["values"])
                return self.render([c])
        return None

    def subprocess_run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        text = self._answer(argv)
        if text is None:
            return types.SimpleNamespace(returncode=1, stdout=b"")
        return types.SimpleNamespace(returncode=0, stdout=text.encode("utf-8"))


BOUNDED_PREFIX = [C.TIMEOUT, "-k", str(C.AMIXER_KILL_AFTER_S), str(C.AMIXER_TIMEOUT_S), C.AMIXER]


class ParseAndPickTest(unittest.TestCase):
    def test_bench_mic_parses_with_its_live_range_and_db(self):
        text = ("Simple mixer control 'Mic',0\n"
                "  Capabilities: cvolume cvolume-joined cswitch cswitch-joined\n"
                "  Capture channels: Mono\n"
                "  Limits: Capture 0 - 16\n"
                "  Mono: Capture 12 [75%] [18.75dB] [on]\n")
        ctl = mixer.pick_capture_volume(mixer.parse_scontents(text))
        self.assertEqual((ctl.name, ctl.index, ctl.min, ctl.max, ctl.values, ctl.db),
                         ("Mic", 0, 0, 16, (12,), "18.75"))

    def test_control_is_found_by_capture_volume_type_not_by_name(self):
        # A playback-only «Mic» (a headset's sidetone) comes first; the capture
        # volume lives under a name that says nothing about a microphone.
        amixer = FakeAmixer([
            {"name": "Mic", "caps": "pvolume pswitch", "lo": 0, "hi": 31, "values": [20, 20]},
            {"name": "Line", "caps": "cvolume cswitch", "lo": 0, "hi": 100, "values": [40]},
        ])
        answered, ctl = mixer.read("1", amixer)
        self.assertTrue(answered)
        self.assertEqual((ctl.name, ctl.min, ctl.max), ("Line", 0, 100))
        result = mixer.apply("1", 70, amixer)
        self.assertEqual(result.state, C.GAIN_APPLIED)
        self.assertEqual(amixer.calls[-1][0][-4:], ["sset", "Line,0", "capture", "70"])

    def test_card_from_an_explicit_device_name(self):
        for device, card in (("plughw:CARD=Microphone,DEV=0", "Microphone"),
                             ("hw:CARD=Mic_2", "Mic_2"), ("hw:1,0", "1"), ("plughw:2", "2"),
                             ("hw:01", "1"), ("default", None), ("sysdefault", None),
                             ("hw:CARD=", None), ("dsnoop:1", None)):
            with self.subTest(device=device):
                self.assertEqual(mixer.card_from_device(device), card)

    def test_no_capture_volume_is_no_control(self):
        amixer = FakeAmixer([
            {"name": "Mic", "caps": "pvolume pswitch", "lo": 0, "hi": 31, "values": [20]},
            {"name": "Capture", "caps": "cswitch", "lo": 0, "hi": 1, "values": []},
        ])
        self.assertEqual(mixer.read("1", amixer), (True, None))

    def test_a_capture_control_outside_the_name_grammar_is_no_control(self):
        # The name is one argv word for amixer: never a guess at the next one.
        amixer = FakeAmixer([
            {"name": "Mic;rm", "caps": "cvolume", "lo": 0, "hi": 16, "values": [1]},
            {"name": "Mic", "caps": "cvolume", "lo": 0, "hi": 16, "values": [1]},
        ])
        self.assertEqual(mixer.read("1", amixer), (True, None))


class ApplyTest(unittest.TestCase):
    def test_bounds_come_from_the_live_control_not_a_constant(self):
        cases = (
            # (control range, raw, expected state)
            ((0, 16), 16, C.GAIN_APPLIED),
            ((0, 16), 17, C.GAIN_OUT_OF_RANGE),
            ((0, 16), -1, C.GAIN_OUT_OF_RANGE),
            ((0, 100), 50, C.GAIN_APPLIED),
            ((0, 100), 101, C.GAIN_OUT_OF_RANGE),
            ((-20, 30), -5, C.GAIN_APPLIED),
            ((-20, 30), -21, C.GAIN_OUT_OF_RANGE),
        )
        for (lo, hi), raw, state in cases:
            with self.subTest(lo=lo, hi=hi, raw=raw):
                amixer = FakeAmixer([{"name": "Mic", "caps": "cvolume", "lo": lo, "hi": hi,
                                      "values": [lo]}])
                result = mixer.apply("1", raw, amixer)
                self.assertEqual(result.state, state, result.detail)
                self.assertEqual((result.control.min, result.control.max), (lo, hi))
                if state == C.GAIN_OUT_OF_RANGE:
                    self.assertEqual(amixer.verbs(), ["scontents"])  # nothing written
                    self.assertEqual(result.detail, "capture_gain %d out of %d..%d, not applied"
                                     % (raw, lo, hi))
                else:
                    self.assertEqual(amixer.verbs(), ["scontents", "sset"])
                    self.assertEqual(result.control.values, (raw,))

    def test_a_negative_value_is_passed_after_the_option_terminator(self):
        amixer = FakeAmixer([{"name": "Mic", "caps": "cvolume", "lo": -20, "hi": 30,
                              "values": [0]}])
        mixer.apply("Microphone", -5, amixer)
        argv = amixer.calls[-1][0]
        self.assertLess(argv.index("--"), argv.index("-5"))
        self.assertEqual(argv[argv.index("--"):], ["--", "sset", "Mic,0", "capture", "-5"])

    def test_missing_control_does_nothing(self):
        amixer = FakeAmixer([{"name": "Mic", "caps": "pvolume", "lo": 0, "hi": 31,
                              "values": [20]}])
        result = mixer.apply("1", 8, amixer)
        self.assertEqual((result.state, result.control), (C.GAIN_NO_CONTROL, None))
        self.assertEqual(amixer.verbs(), ["scontents"])

    def test_amixer_failure_and_a_value_that_does_not_land(self):
        failed = FakeAmixer(FIFINE, fail=True)
        self.assertEqual(mixer.apply("1", 8, failed).state, C.GAIN_FAILED)
        self.assertEqual(failed.verbs(), ["scontents"])
        sticky = FakeAmixer(FIFINE, sticky=True)
        result = mixer.apply("1", 8, sticky)
        self.assertEqual(result.state, C.GAIN_FAILED)
        self.assertIn("did not land", result.detail)


class TimeoutTest(unittest.TestCase):
    def test_every_amixer_argv_is_bounded_by_timeout(self):
        ctl = mixer.Control("Mic", 0, ("cvolume",), 0, 16, (1,), "")
        for argv in (mixer.argv_get("1"), mixer.argv_set("1", ctl, 8)):
            with self.subTest(argv=argv):
                self.assertEqual(argv[:len(BOUNDED_PREFIX)], BOUNDED_PREFIX)
        self.assertLess(C.AMIXER_TIMEOUT_S, C.AMIXER_RUN_TIMEOUT_S)

    def test_run_carries_a_subprocess_timeout_and_a_hang_is_a_failure(self):
        amixer = FakeAmixer(FIFINE)
        with mock.patch.object(mixer.subprocess, "run", amixer.subprocess_run):
            self.assertIn("Simple mixer control 'Mic',0", mixer.run(mixer.argv_get("1")))
        self.assertEqual(amixer.calls[0][1]["timeout"], C.AMIXER_RUN_TIMEOUT_S)
        hung = FakeAmixer(FIFINE, timeout=True)
        with mock.patch.object(mixer.subprocess, "run", hung.subprocess_run):
            self.assertIsNone(mixer.run(mixer.argv_get("1")))


class _Events:
    def __init__(self):
        self.items = []

    def add(self, kind, **fields):
        if kind not in C.EVENT_KINDS:
            raise ValueError("unknown event %r" % kind)
        self.items.append(dict(fields, event=kind))

    def gain(self):
        return [e for e in self.items if e["event"] == "gain"]


class _Lease:
    def acquire(self):
        return True

    def release(self):
        pass


class DaemonGainTest(unittest.TestCase):
    """service.Daemon over a Sandbox (the bench card 1 «Microphone»), with
    every amixer fork going through FakeAmixer.subprocess_run."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.d = service.Daemon(conf_path=self.sb.conf, media=self.sb.media,
                                mounts_file=self.sb.mounts, root=self.sb.root,
                                dev_of=self.sb.dev_of, asound_dir=self.sb.asound,
                                mic_lock=self.sb.mic_lock, status_file=self.sb.status,
                                events_file=self.sb.events, daemon_lock=self.sb.lock,
                                check_dependencies=lambda: [])
        self.d.events = _Events()
        self.d.lease = _Lease()
        self.status_writes = []
        p = mock.patch.object(service, "atomic_write",
                              lambda path, data, **kw: self.status_writes.append(data))
        p.start()
        self.addCleanup(p.stop)

    def use(self, amixer):
        self.amixer = amixer
        p = mock.patch.object(mixer.subprocess, "run", amixer.subprocess_run)
        p.start()
        self.addCleanup(p.stop)
        return amixer

    def conf(self, **values):
        self.sb.write_conf(**values)
        self.assertTrue(self.d.reload_conf())

    def assert_all_bounded(self):
        for argv, kwargs in self.amixer.calls:
            self.assertEqual(argv[:len(BOUNDED_PREFIX)], BOUNDED_PREFIX, argv)
            self.assertEqual(kwargs.get("timeout"), C.AMIXER_RUN_TIMEOUT_S, argv)

    def test_key_absent_never_touches_the_mixer(self):
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(keep_files=10)
        for _ in range(3):
            self.d.check_mic()
        status = self.d.write_status(C.STATE_IDLE)
        self.assertEqual(amixer.calls, [])
        self.assertEqual(status["mic_gain"]["state"], C.GAIN_OFF)
        self.assertEqual(self.d.events.gain(), [])
        self.assertEqual(amixer.controls[0]["values"], [12])  # the kernel default stays

    def test_key_removed_stops_managing_without_a_restore(self):
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(capture_gain=8)
        self.assertEqual(self.d.gain["state"], C.GAIN_APPLIED)
        amixer.calls.clear()
        self.conf(keep_files=10)
        self.d.check_mic()
        self.assertEqual(amixer.calls, [])
        self.assertEqual(self.d.write_status(C.STATE_IDLE)["mic_gain"]["state"], C.GAIN_OFF)
        self.assertEqual(amixer.controls[0]["values"], [8])

    def test_a_value_in_range_is_applied_and_reported(self):
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(capture_gain=8)
        self.assertEqual(amixer.verbs(), ["scontents", "sset"])
        self.assertEqual(amixer.calls[1][0][-4:], ["sset", "Mic,0", "capture", "8"])
        status = self.d.write_status(C.STATE_IDLE)
        self.assertEqual(status["mic_gain"], {"state": C.GAIN_APPLIED, "want": 8, "applied": 8,
                                              "min": 0, "max": 16, "db": "12.00",
                                              "control": "Mic"})
        self.assertEqual(status["conf_warnings"], [])
        self.assertEqual([e["status"] for e in self.d.events.gain()], [C.GAIN_APPLIED])
        # One confirmation read on the next mic check, then no fork while the
        # card identity and the value hold.
        self.d.check_mic()
        self.d.check_mic()
        self.assertEqual(amixer.verbs(), ["scontents", "sset", "scontents"])
        self.assert_all_bounded()

    def test_out_of_range_is_a_conf_warning_and_not_applied(self):
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(capture_gain=50)
        self.assertEqual(amixer.verbs(), ["scontents"])
        self.assertEqual(amixer.controls[0]["values"], [12])
        status = self.d.write_status(C.STATE_IDLE)
        warning = "capture_gain 50 out of 0..16, not applied"
        self.assertIn(warning, status["conf_warnings"])
        self.assertEqual((status["mic_gain"]["state"], status["mic_gain"]["applied"],
                          status["mic_gain"]["min"], status["mic_gain"]["max"]),
                         (C.GAIN_OUT_OF_RANGE, None, 0, 16))
        self.assertEqual([(e["status"], e["detail"]) for e in self.d.events.gain()],
                         [(C.GAIN_OUT_OF_RANGE, warning)])
        # Back in range: applied, the warning gone.
        self.conf(capture_gain=4)
        status = self.d.write_status(C.STATE_IDLE)
        self.assertEqual(status["mic_gain"]["state"], C.GAIN_APPLIED)
        self.assertNotIn(warning, status["conf_warnings"])
        self.assert_all_bounded()

    def test_missing_control_is_an_event_and_nothing_is_done(self):
        amixer = self.use(FakeAmixer([{"name": "Mic", "caps": "pvolume pswitch", "lo": 0,
                                       "hi": 31, "values": [20]}]))
        self.conf(capture_gain=8)
        self.assertEqual(amixer.verbs(), ["scontents"])
        self.assertEqual(amixer.controls[0]["values"], [20])
        self.assertEqual([e["status"] for e in self.d.events.gain()], [C.GAIN_NO_CONTROL])
        self.assertEqual(self.d.write_status(C.STATE_IDLE)["mic_gain"]["state"],
                         C.GAIN_NO_CONTROL)
        # Not retried on every mic check: only a replug or a conf change does.
        self.d.check_mic()
        self.d.check_mic()
        self.assertEqual(amixer.verbs(), ["scontents"])
        self.assertEqual(len(self.d.events.gain()), 1)

    def test_a_hung_amixer_is_a_failure_not_a_wait(self):
        amixer = self.use(FakeAmixer(FIFINE, timeout=True))
        self.conf(capture_gain=8)
        self.assertEqual(self.d.gain["state"], C.GAIN_FAILED)
        self.assertEqual([e["status"] for e in self.d.events.gain()], [C.GAIN_FAILED])
        self.assert_all_bounded()

    def test_an_overwrite_after_apply_is_reapplied_once(self):
        # udev `alsactl restore` racing the hot-plug writes the old value back.
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(capture_gain=8)
        amixer.controls[0]["values"] = [12]
        self.d.check_mic()
        self.assertEqual(amixer.verbs(), ["scontents", "sset", "scontents", "scontents", "sset"])
        self.assertEqual(amixer.controls[0]["values"], [8])
        self.assertEqual([e["status"] for e in self.d.events.gain()],
                         [C.GAIN_APPLIED, C.GAIN_OVERRIDDEN])
        # Overwritten again: never a loop.
        amixer.controls[0]["values"] = [12]
        self.d.check_mic()
        self.d.check_mic()
        self.assertEqual(len(amixer.calls), 5)
        self.assert_all_bounded()

    def test_a_replug_is_a_new_identity_and_reapplies(self):
        usbbus = self.sb.asound + "/card1/usbbus"
        with open(usbbus, "w", encoding="ascii") as fh:
            fh.write("001/002\n")
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(capture_gain=8)
        self.d.check_mic()  # the confirmation read
        self.assertEqual(amixer.verbs(), ["scontents", "sset", "scontents"])
        # Same card index and id, a new USB device number: the kernel default
        # is back and the daemon applies again.
        amixer.controls[0]["values"] = [12]
        with open(usbbus, "w", encoding="ascii") as fh:
            fh.write("001/003\n")
        self.d.check_mic()
        self.assertEqual(amixer.verbs()[3:], ["scontents", "sset"])
        self.assertEqual(amixer.controls[0]["values"], [8])

    def test_an_explicit_device_targets_the_card_its_name_names(self):
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(capture_gain=8, device="plughw:CARD=Microphone,DEV=0")
        self.assertEqual(amixer.calls[0][0][len(BOUNDED_PREFIX):],
                         ["-c", "Microphone", "--", "scontents"])
        self.assertEqual(self.d.gain["state"], C.GAIN_APPLIED)

    def test_an_explicit_device_without_a_derivable_card_forks_nothing(self):
        amixer = self.use(FakeAmixer(FIFINE))
        self.conf(capture_gain=8, device="default")
        self.d.check_mic()
        self.assertEqual(amixer.calls, [])
        self.assertEqual(self.d.gain["state"], C.GAIN_NO_CARD)
        self.assertEqual([e["status"] for e in self.d.events.gain()], [C.GAIN_NO_CARD])

    def test_no_mic_forks_nothing(self):
        amixer = self.use(FakeAmixer(FIFINE))
        self.sb.set_cards(" 0 [Codec          ]: H3_Audio_Codec - H3 Audio Codec\n", capture=())
        self.conf(capture_gain=8)
        self.d.check_mic()
        self.assertEqual(amixer.calls, [])
        self.assertEqual(self.d.gain["state"], C.GAIN_NO_MIC)


class NoMqttControlTest(unittest.TestCase):
    """Operator decision 1.0.7.10: the gain has no MQTT control — it is a
    panel/conf setting only."""

    def test_no_gain_control_or_command_topic(self):
        names = [c.name for c in C.CONTROLS] + list(C.COMMAND_CONTROLS)
        self.assertFalse([n for n in names if "gain" in n], names)
        self.assertFalse([t for t in mqttio.COMMAND_TOPICS if "gain" in t])
        self.assertFalse([t for t, _p in mqttio.meta_topics() if "gain" in t])

    def test_capture_gain_is_a_conf_key(self):
        self.assertIn("capture_gain", config.KEYS)


if __name__ == "__main__":
    unittest.main()
