"""The panel dispatch (contract §15): states, the clip list from present media
only, the mutation grammar and its follow-up line, the removable-media
acknowledgement, the download path."""

from __future__ import annotations

import io
import json
import os
import time
import unittest
from unittest import mock

from sa02m_audio import api
from sa02m_audio import config
from sa02m_audio import constants as C
from sa02m_audio import media
from sa02m_audio import store as store_mod

from . import POSIX, POSIX_REASON
from .fakes import Sandbox

ID1 = "20261015T120000Z-0a1b2c"
ID2 = "20261015T120500Z-0a1b2d"


@unittest.skipUnless(POSIX, POSIX_REASON)
class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        patches = [
            mock.patch.object(C, "CONF_FILE", self.sb.conf),
            mock.patch.object(C, "STATUS_FILE", self.sb.status),
            mock.patch.object(C, "EVENTS_FILE", self.sb.events),
            mock.patch.object(api, "MEDIA", self.sb.media),
            mock.patch.object(api, "footprint_installed", lambda: True),
            mock.patch.object(api, "_probe", lambda: media.MediaProbe(
                self.sb.media, mounts_file=self.sb.mounts, root=self.sb.root, dev_of=self.sb.dev_of)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sb.write_conf(enabled=True)
        self.store = store_mod.Store(self.sb.media)

    def status(self, state="idle", age=0, **extra):
        body = {"state": state, "ts": int(time.time()) - age, "mic": "present",
                "medium_state": "ok", "media": {}, "recording": None, "last_file": "",
                "last_status": "ok"}
        body.update(extra)
        with open(self.sb.status, "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def clip(self, medium, rec_id, body=b"RIFF....WAVE"):
        part = self.store.open_part(medium, rec_id)
        os.write(part.fd, body)
        os.close(part.fd)
        part.commit()
        part.close()

    def post(self, **body):
        return api.dispatch("POST", json.dumps(body))


class StateTest(ApiTestBase):
    def test_not_installed(self):
        with mock.patch.object(api, "footprint_installed", lambda: False):
            view = api.status_view()
        self.assertEqual((view["state"], view["installed"], view["live"]), ("not_installed", False, False))
        self.assertNotIn("clips", view)

    def test_disabled_wins_over_a_live_looking_status(self):
        self.sb.write_conf(enabled=False)
        self.status("idle")
        view = api.status_view()
        self.assertEqual((view["state"], view["live"]), ("disabled", False))

    def test_live_idle_and_recording(self):
        self.status("idle")
        self.assertEqual((api.status_view()["state"], api.status_view()["live"]), ("idle", True))
        self.status("recording", recording={"id": ID1, "medium": "emmc", "seconds": 10, "started_ts": 1})
        view = api.status_view()
        self.assertEqual(view["state"], "recording")
        self.assertEqual(view["recording"]["id"], ID1)

    def test_stale_status_is_not_running(self):
        self.status("idle", age=C.STATUS_STALE_S + 5)
        view = api.status_view()
        self.assertEqual((view["state"], view["live"]), ("not_running", False))

    def test_no_status_while_enabled_is_not_running(self):
        self.assertEqual(api.status_view()["state"], "not_running")

    def test_conf_unreadable(self):
        os.unlink(self.sb.conf)
        os.mkdir(self.sb.conf)
        self.assertEqual(api.status_view()["state"], "conf_unreadable")

    def test_daemon_standby_states_pass_through(self):
        for st in ("missing_deps", "conf_unreadable", "error"):
            with self.subTest(st=st):
                self.status(st)
                self.assertEqual(api.status_view()["state"], st)

    def test_untrusted_status_values_are_dropped(self):
        self.status("idle", mic="<b>", medium_state="maybe", last_status="pwned",
                    last_file="../../etc/passwd", media={"emmc": {"state": "x", "files": "9"}})
        view = api.status_view()
        self.assertEqual((view["mic"], view["medium_state"], view["last_status"], view["last_file"]),
                         ("", "", "", ""))
        self.assertEqual(view["media"]["emmc"], {"state": "absent", "free_bytes": 0, "files": 0, "bytes": 0})

    def test_bounds_and_conf_for_the_card(self):
        view = api.status_view()
        self.assertEqual(view["bounds"]["keep_mb"], {"emmc": [1, 256], "usb": [1, 4096], "sdcard": [1, 4096]})
        self.assertEqual(view["bounds"]["max_duration_s"], [2, 30])
        self.assertEqual(set(view["conf"]), set(config.KEYS))
        self.assertEqual(view["removable"], ["usb", "sdcard"])


class ClipsTest(ApiTestBase):
    def test_emmc_and_a_mounted_stick_are_listed_newest_first(self):
        self.sb.mount(self.sb.usb)
        self.clip("emmc", ID1)
        self.clip("usb", ID2)
        old = os.path.join(self.sb.rec("emmc"), ID1 + ".wav")
        os.utime(old, (time.time() - 100, time.time() - 100))
        view = api.status_view()
        self.assertEqual([(c["id"], c["medium"]) for c in view["clips"]], [(ID2, "usb"), (ID1, "emmc")])
        self.assertEqual(set(view["present"]), {"emmc", "usb"})

    def test_the_phantom_dir_is_never_listed(self):
        """A clip planted in the rootfs dir /media/usb leaves behind is not a
        clip of a present medium."""
        os.makedirs(self.sb.rec("usb"))
        with open(os.path.join(self.sb.rec("usb"), ID2 + ".wav"), "wb") as fh:
            fh.write(b"RIFF")
        view = api.status_view()
        self.assertEqual(view["clips"], [])
        self.assertNotIn("usb", view["present"])

    def test_events_tail_is_bounded_and_sanitised(self):
        lines = [json.dumps({"ts": i, "event": "start", "id": ID1, "detail": "x" * 400, "junk": 1})
                 for i in range(30)]
        lines.append(json.dumps({"ts": 99, "event": "made_up"}))
        lines.append("not json")
        with open(self.sb.events, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        events = api.status_view()["events"]
        self.assertLessEqual(len(events), C.EVENTS_TAIL)
        self.assertTrue(all(e["event"] == "start" and "junk" not in e and len(e["detail"]) == 200
                            for e in events))


class MutationTest(ApiTestBase):
    def test_record_needs_a_live_daemon(self):
        self.assertEqual(self.post(action="record", seconds=5)[0]["error"], "not_running")
        self.sb.write_conf(enabled=False)
        self.assertEqual(self.post(action="record", seconds=5)[0]["error"], "disabled")

    def test_record_grammar(self):
        self.status("idle")
        self.assertEqual(self.post(action="record", seconds=10), ({"ok": True, "action": "record"},
                                                                  "pub record 10"))
        for bad in (1, 31, 0, -5, "10", True, 2.5, None):
            with self.subTest(bad=bad):
                resp, follow = self.post(action="record", seconds=bad)
                self.assertEqual((resp["error"], follow), ("bad_seconds", ""))
        self.sb.write_conf(enabled=True, max_duration_s=15)
        self.assertEqual(self.post(action="record", seconds=20)[0]["error"], "bad_seconds")

    def test_stop_and_delete(self):
        self.status("idle")
        self.assertEqual(self.post(action="stop")[1], "pub record stop")
        self.assertEqual(self.post(action="delete", id=ID1)[1], "pub delete " + ID1)
        self.assertEqual(self.post(action="delete", id="all")[1], "pub delete all")
        for bad in ("ALL", "../x", ID1 + " ", 5, None, ID1.upper()):
            with self.subTest(bad=bad):
                self.assertEqual(self.post(action="delete", id=bad), ({"ok": False, "error": "bad_id"}, ""))

    def test_enable_disable_write_the_conf_and_name_the_verb(self):
        self.sb.write_conf(enabled=False)
        resp, follow = self.post(action="enable")
        self.assertEqual((resp["ok"], follow), (True, "sudo enable"))
        self.assertTrue(config.load(self.sb.conf).enabled)
        resp, follow = self.post(action="disable")
        self.assertEqual(follow, "sudo disable")
        self.assertFalse(config.load(self.sb.conf).enabled)

    def test_restart_needs_enabled(self):
        self.assertEqual(self.post(action="restart")[1], "sudo restart")
        self.sb.write_conf(enabled=False)
        self.assertEqual(self.post(action="restart"), ({"ok": False, "error": "not_enabled"}, ""))

    def test_save_removable_needs_the_acknowledgement(self):
        resp, follow = self.post(action="save", storage="usb")
        self.assertEqual((resp, follow), ({"ok": False, "error": "ack_required"}, ""))
        self.assertEqual(config.load(self.sb.conf).storage, "emmc")  # nothing saved
        resp, _ = self.post(action="save", storage="usb", ack_removable="true")
        self.assertEqual(resp["error"], "ack_required")  # only the JSON true counts
        resp, follow = self.post(action="save", storage="usb", ack_removable=True, keep_mb=2000)
        self.assertTrue(resp["ok"])
        self.assertEqual(follow, "")
        conf = config.load(self.sb.conf)
        self.assertEqual((conf.storage, conf.keep_mb), ("usb", 2000))
        # A later save that keeps the removable medium needs it again.
        self.assertEqual(self.post(action="save", keep_hours=48)[0]["error"], "ack_required")
        self.assertTrue(self.post(action="save", storage="emmc", keep_mb=100)[0]["ok"])

    def test_save_never_takes_the_alsa_device(self):
        # The device name is not a panel setting (api.SAVE_KEYS): a value with
        # a line ending is refused as a field, before any grammar.
        for value in ("hw:1\n", "hw:1\r\n", "hw:1"):
            with self.subTest(value=value):
                self.assertEqual(self.post(action="save", device=value)[0],
                                 {"ok": False, "error": "unknown_field", "field": "device"})
        self.assertEqual(config.load(self.sb.conf).device, C.DEVICE_AUTO)

    def test_save_values_are_typed_and_bounded(self):
        for body in ({"keep_mb": 300}, {"keep_mb": "10"}, {"keep_files": True}, {"keep_hours": 0},
                     {"storage": "nfs"}, {"max_duration_s": 31}, {"default_duration_s": 1},
                     {"delete_after_upload": "yes"}, {"storage": 5}):
            with self.subTest(body=body):
                resp, follow = self.post(action="save", **body)
                self.assertEqual((resp["ok"], resp["error"], follow), (False, "invalid_value", ""))
        self.assertEqual(self.post(action="save", device="hw:0")[0],
                         {"ok": False, "error": "unknown_field", "field": "device"})
        self.assertEqual(self.post(action="save", enabled=True)[0]["error"], "unknown_field")

    def test_save_answers_with_the_status(self):
        resp, _ = self.post(action="save", keep_files=10)
        self.assertEqual(resp["status"]["conf"]["keep_files"], 10)

    def test_unknown_action_bad_json_unreadable_conf(self):
        self.assertEqual(self.post(action="reboot")[0]["error"], "not_found")
        self.assertEqual(api.dispatch("POST", "{nope")[0]["error"], "invalid_json")
        self.assertEqual(api.dispatch("POST", "[1]")[0]["error"], "invalid_json")
        self.assertEqual(api.dispatch("POST", "x" * (C.MAX_BODY_BYTES + 1))[0]["error"], "payload_too_large")
        self.assertEqual(api.dispatch("PUT", "")[0]["error"], "method_not_allowed")
        os.unlink(self.sb.conf)
        os.mkdir(self.sb.conf)
        self.assertEqual(self.post(action="enable"), ({"ok": False, "error": "conf_unreadable"}, ""))

    def test_not_installed_mutation_names_no_follow_up(self):
        with mock.patch.object(api, "footprint_installed", lambda: False):
            self.assertEqual(self.post(action="enable"),
                             ({"ok": False, "error": "not_installed", "state": "not_installed"}, ""))

    def test_follow_up_grammar_is_closed(self):
        """Every follow-up the dispatch can name matches the CGI's case."""
        import re
        self.status("idle")
        seen = set()
        for body in ({"action": "record", "seconds": 2}, {"action": "record", "seconds": 30},
                     {"action": "stop"}, {"action": "delete", "id": ID1}, {"action": "delete", "id": "all"},
                     {"action": "enable"}, {"action": "disable"}, {"action": "restart"}):
            self.sb.write_conf(enabled=True)
            seen.add(api.dispatch("POST", json.dumps(body))[1])
        grammar = re.compile(r"^(pub record ([1-9][0-9]?|stop)|pub delete (all|%s)|sudo (%s))$"
                             % (C.ID_RE[1:-1], "|".join(C.TRIGGER_VERBS)))
        self.assertTrue(all(grammar.match(f) for f in seen), seen)
        self.assertEqual(len(seen), 8)


class DownloadTest(ApiTestBase):
    def fetch(self, query):
        out = io.BytesIO()
        api.download(query, out)
        raw = out.getvalue()
        head, _, body = raw.partition(b"\r\n\r\n")
        return head.decode("ascii", "replace"), body

    def test_wav_is_streamed_with_safe_headers(self):
        self.clip("emmc", ID1, b"RIFF" + b"\0" * 100)
        head, body = self.fetch("action=download&id=" + ID1)
        self.assertIn("Content-Type: audio/wav", head)
        self.assertIn("X-Content-Type-Options: nosniff", head)
        self.assertIn('Content-Disposition: attachment; filename="%s.wav"' % ID1, head)
        self.assertIn("Content-Length: 104", head)
        self.assertEqual(body, b"RIFF" + b"\0" * 100)
        head, _ = self.fetch("action=download&id=%s&inline=1" % ID1)
        self.assertIn("Content-Disposition: inline", head)

    def json_error(self, query):
        head, body = self.fetch(query)
        self.assertIn("application/json", head)
        return json.loads(body)["error"]

    def test_bad_ids(self):
        for q in ("action=download&id=../../etc/passwd", "action=download", "action=download&id=",
                  "action=download&id=%s&id=%s" % (ID1, ID2), "action=download&id=" + "x" * 600):
            with self.subTest(q=q[:60]):
                self.assertIn(self.json_error(q), ("bad_id", "bad_request"))

    def test_planted_symlink_hardlink_and_big_file_are_refused(self):
        rec = self.sb.rec("emmc")
        os.makedirs(rec, exist_ok=True)
        secret = os.path.join(self.sb.root, "secret")
        with open(secret, "wb") as fh:
            fh.write(b"SECRET")
        os.symlink(secret, os.path.join(rec, ID1 + ".wav"))
        self.assertEqual(self.json_error("action=download&id=" + ID1), "not_found")
        os.link(secret, os.path.join(rec, ID2 + ".wav"))
        self.assertEqual(self.json_error("action=download&id=" + ID2), "not_found")
        big = "20261015T121000Z-0a1b2e"
        with open(os.path.join(rec, big + ".wav"), "wb") as fh:
            fh.truncate(C.DOWNLOAD_MAX_BYTES + 1)
        self.assertEqual(self.json_error("action=download&id=" + big), "not_found")

    def test_phantom_medium_is_never_read(self):
        os.makedirs(self.sb.rec("usb"))
        with open(os.path.join(self.sb.rec("usb"), ID2 + ".wav"), "wb") as fh:
            fh.write(b"RIFF planted")
        self.assertEqual(self.json_error("action=download&id=" + ID2), "not_found")
        self.sb.mount(self.sb.usb)
        head, body = self.fetch("action=download&id=" + ID2)
        self.assertEqual(body, b"RIFF planted")


class CliTest(ApiTestBase):
    def test_two_line_protocol(self):
        self.status("idle")
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO('{"action":"record","seconds":3}')), \
                mock.patch("sys.stdout", out):
            api.main(["POST"])
        lines = out.getvalue().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0]), {"ok": True, "action": "record"})
        self.assertEqual(lines[1], "pub record 3")


if __name__ == "__main__":
    unittest.main()
