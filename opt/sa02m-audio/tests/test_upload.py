"""Upload to the Operator's URL (contract §Передача): the wire, retries, the
URL policy (sa02m_rules.http_guard) and its absence, the token, the per-clip
sidecar, and the daemon end to end against a local HTTP receiver."""

from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

from sa02m_audio import config
from sa02m_audio import constants as C
from sa02m_audio import store as store_mod
from sa02m_audio import upload

from . import POSIX, POSIX_REASON, REPO_ROOT
from .fakes import DaemonHarness, Sandbox, wait_for

RULES_DIR = os.path.join(REPO_ROOT, "opt", "sa02m-rules")
TOKEN = "s3cr3t-T0KEN.value~+/="


class OpenGuard:
    """A URL policy that allows the loopback test receiver (the real one,
    rightly, refuses loopback); `refuse` names hosts/URL parts it refuses."""

    def __init__(self, refuse=()):
        self.refuse = tuple(refuse)
        self.calls = []

    def check_url(self, url, resolve=True, allow=None):
        self.calls.append((url, resolve))
        return "refused: test policy" if any(r in url for r in self.refuse) else ""


class Receiver:
    """A local HTTP server recording every request; `mode` per test."""

    def __init__(self):
        self.requests = []
        self.mode = "ok"
        self.redirect_to = "/moved"
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                receiver.requests.append((self.path, self.headers, body))  # case-insensitive
                mode = receiver.mode
                if mode == "slow":
                    time.sleep(1.5)
                if mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", receiver.redirect_to)
                    self.end_headers()
                    receiver.mode = "ok"
                    return
                if mode == "302" and self.path == "/up":
                    # Its target would answer 201: following a 302 would read as ok.
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                    return
                code = {"ok": 201, "500": 500, "403": 403, "slow": 200}.get(mode, 200)
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/up" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def job(url, token="", data=b"RIFF0000WAVEdata", rec_id="20261015T120000Z-0a1b2c"):
    return upload.UploadJob(rec_id, "emmc", data, 5000, -12, url, token)


class PostOnceTest(unittest.TestCase):
    def setUp(self):
        self.rx = Receiver()
        self.addCleanup(self.rx.close)
        self.opener = upload.default_opener()

    def test_wire_headers_body_bearer(self):
        state, detail = upload.post_once(job(self.rx.url, TOKEN), OpenGuard(), self.opener, 5)
        self.assertEqual(state, "ok")
        path, headers, body = self.rx.requests[0]
        self.assertEqual(body, b"RIFF0000WAVEdata")
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(headers["X-SA02M-Recording-Id"], "20261015T120000Z-0a1b2c")
        self.assertEqual(headers["X-SA02M-Duration-Ms"], "5000")
        self.assertEqual(headers["X-SA02M-Peak-Dbfs"], "-12")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, detail)
        self.assertIn("127.0.0.1", detail)

    def test_no_token_no_authorization_header(self):
        upload.post_once(job(self.rx.url), OpenGuard(), self.opener, 5)
        self.assertNotIn("Authorization", self.rx.requests[0][1])

    def test_non_2xx_is_a_retry(self):
        for mode in ("500", "403", "302"):
            with self.subTest(mode=mode):
                self.rx.mode = mode
                self.assertEqual(upload.post_once(job(self.rx.url), OpenGuard(), self.opener, 5)[0], "retry")

    def test_307_is_followed_and_rejudged(self):
        self.rx.mode = "redirect"
        guard = OpenGuard()
        self.assertEqual(upload.post_once(job(self.rx.url), guard, self.opener, 5)[0], "ok")
        self.assertEqual([r[0] for r in self.rx.requests], ["/up", "/moved"])
        self.assertEqual(self.rx.requests[1][2], b"RIFF0000WAVEdata")  # the body re-sent
        self.assertEqual(len(guard.calls), 2)  # every hop judged
        self.rx.requests.clear()
        self.rx.mode = "redirect"
        state, _ = upload.post_once(job(self.rx.url), OpenGuard(refuse=("/moved",)), self.opener, 5)
        self.assertEqual(state, "refused")
        self.assertEqual(len(self.rx.requests), 1)  # the refused hop never sent

    def test_dial_host_must_be_the_host_that_was_checked(self):
        # urlsplit does not percent-decode; urllib would dial 127.0.0.1.
        state, detail = upload.post_once(job("http://127%2e0%2e0%2e1/x", TOKEN),
                                          OpenGuard(), self.opener, 2)
        self.assertEqual(state, "refused")
        self.assertNotIn(TOKEN, detail)
        self.assertEqual(self.rx.requests, [])
        self.rx.mode = "redirect"
        self.rx.redirect_to = "http://127%2e0%2e0%2e1/secret"
        state, detail = upload.post_once(job(self.rx.url, TOKEN), OpenGuard(), self.opener, 2)
        self.assertEqual(state, "refused")
        self.assertEqual([r[0] for r in self.rx.requests], ["/up"])  # the retargeted hop was not sent
        self.assertNotIn(TOKEN, detail)

    def test_refused_url_sends_nothing(self):
        state, _ = upload.post_once(job(self.rx.url), OpenGuard(refuse=("127.0.0.1",)), self.opener, 5)
        self.assertEqual((state, self.rx.requests), ("refused", []))

    def test_timeout_is_a_retry(self):
        self.rx.mode = "slow"
        t0 = time.monotonic()
        state, _ = upload.post_once(job(self.rx.url), OpenGuard(), self.opener, 0.5)
        self.assertEqual(state, "retry")
        self.assertLess(time.monotonic() - t0, 1.4)

    def test_connection_refused_is_a_retry(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(upload.post_once(job("http://127.0.0.1:%d/" % port), OpenGuard(),
                                          self.opener, 2)[0], "retry")


class RealGuardTest(unittest.TestCase):
    """The real sa02m_rules.http_guard, imported the way the daemon does."""

    def test_loaded_from_the_rules_dir(self):
        guard = upload.load_guard(RULES_DIR)
        self.assertIsNotNone(guard)
        self.assertTrue(guard.check_url("http://127.0.0.1:9/x", resolve=False))
        self.assertTrue(guard.check_url("http://user:pw@192.168.1.10/x", resolve=False))
        self.assertTrue(guard.check_url("ftp://192.168.1.10/x", resolve=False))
        self.assertEqual(guard.check_url("https://192.168.1.10/x", resolve=False), "")

    def test_loopback_receiver_is_refused_and_never_reached(self):
        rx = Receiver()
        self.addCleanup(rx.close)
        guard = upload.load_guard(RULES_DIR)
        state, _ = upload.post_once(job(rx.url), guard, upload.default_opener(), 5)
        self.assertEqual((state, rx.requests), ("refused", []))

    def test_missing_rules_is_none(self):
        with mock.patch.dict("sys.modules", {"sa02m_rules": None, "sa02m_rules.http_guard": None}):
            self.assertIsNone(upload.load_guard("/nonexistent-rules-dir"))


class UploaderTest(unittest.TestCase):
    def setUp(self):
        self.rx = Receiver()
        self.addCleanup(self.rx.close)
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)
        self.results = []

    def uploader(self, guard=None, delays=(0, 0, 0)):
        return upload.Uploader(self.results.append, self.stop, guard=guard or OpenGuard(),
                               delays=delays, timeout=2)

    def test_three_attempts_then_failed(self):
        self.rx.mode = "500"
        up = self.uploader()
        self.assertEqual(up.submit(job(self.rx.url)), "pending")
        self.assertTrue(wait_for(lambda: self.results, 10))
        self.assertEqual(self.results[0].state, "failed")
        self.assertEqual(len(self.rx.requests), 3)
        self.assertIn("after 3 attempts", self.results[0].detail)

    def test_second_attempt_succeeds(self):
        self.rx.mode = "500"
        up = self.uploader(delays=(0, 1, 2))
        up.submit(job(self.rx.url))
        self.assertTrue(wait_for(lambda: len(self.rx.requests) >= 1, 5))
        self.rx.mode = "ok"
        self.assertTrue(wait_for(lambda: self.results, 10))
        self.assertEqual(self.results[0].state, "ok")

    def test_refused_is_not_retried(self):
        up = self.uploader(guard=OpenGuard(refuse=("127.0.0.1",)))
        up.submit(job(self.rx.url))
        self.assertTrue(wait_for(lambda: self.results, 5))
        self.assertEqual((self.results[0].state, self.rx.requests), ("refused", []))

    def test_queue_full_is_failed_at_once(self):
        gate = threading.Event()
        entered = threading.Event()

        class Blocking(OpenGuard):
            def check_url(self, url, resolve=True, allow=None):
                entered.set()
                gate.wait(5)
                return ""
        up = self.uploader(guard=Blocking())
        states = [up.submit(job(self.rx.url, rec_id="20261015T120000Z-0a1b2c"))]
        self.assertTrue(entered.wait(5))  # the worker holds the first job
        states += [up.submit(job(self.rx.url, rec_id="20261015T12000%dZ-0a1b2c" % i)) for i in range(1, 7)]
        gate.set()
        self.assertEqual(states.count("failed"), 1)
        self.assertEqual(states.count("pending"), 6)  # one in flight + UPLOAD_QUEUE_MAX queued

    def test_backoff_offsets_and_stop(self):
        self.rx.mode = "500"
        up = self.uploader(delays=(0, 30, 90))
        up.submit(job(self.rx.url))
        self.assertTrue(wait_for(lambda: len(self.rx.requests) == 1, 5))
        time.sleep(0.5)
        self.assertEqual(len(self.rx.requests), 1)  # the second attempt waits its offset
        self.stop.set()
        self.assertTrue(wait_for(lambda: self.results, 5))
        self.assertEqual((self.results[0].state, self.results[0].detail), ("failed", "daemon stopped"))

    def test_default_schedule_is_the_contract(self):
        self.assertEqual(C.UPLOAD_DELAYS_S, (0, 10, 60))
        self.assertEqual(C.UPLOAD_TIMEOUT_S, 10)
        self.assertEqual(C.UPLOAD_QUEUE_MAX, 5)


class UrlAndTokenTest(unittest.TestCase):
    def test_url_grammar(self):
        for good in ("", "http://192.168.1.10/up", "https://example.com:8443/a/b?x=1"):
            self.assertTrue(config.valid_upload_url(good), good)
        for bad in ("ftp://x/y", "http://", "http://u:p@h/x", "http://h/a b", "x" * 10,
                    "https://h/" + "a" * 500, "http://h/é", None, 5, "http://[::1"):
            self.assertFalse(config.valid_upload_url(bad), bad)

    def test_conf_never_echoes_a_refused_url(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.conf")
            with open(p, "w") as fh:
                fh.write("[recorder]\nupload_url = http://admin:hunter2@nas/x\n")
            conf = config.load(p)
            self.assertEqual(conf.upload_url, "")
            self.assertFalse(any("hunter2" in w for w in conf.warnings))


@unittest.skipUnless(POSIX, POSIX_REASON)
class TokenFileTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.path = os.path.join(self.sb.etc_dir, "upload.token")

    def test_round_trip_mode_and_clear(self):
        self.assertEqual(config.read_token(self.path), "")
        config.save_token(TOKEN, self.path)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o640")
        self.assertEqual(config.read_token(self.path), TOKEN)
        self.assertTrue(config.token_set(self.path))
        config.save_token("", self.path)
        self.assertFalse(os.path.exists(self.path))
        with self.assertRaises(ValueError):
            config.save_token("bad token\n", self.path)

    def test_untrusted_token_file_is_none(self):
        target = os.path.join(self.sb.root, "elsewhere")
        with open(target, "w") as fh:
            fh.write(TOKEN)
        os.symlink(target, self.path)
        self.assertIsNone(config.read_token(self.path))
        os.unlink(self.path)
        with open(self.path, "w") as fh:
            fh.write("not a token\n")
        self.assertIsNone(config.read_token(self.path))
        os.unlink(self.path)
        os.mkfifo(self.path)
        self.assertIsNone(config.read_token(self.path))

    def test_present_but_empty_or_blank_is_refused(self):
        for text in ("", "\n", "\r\n", "   \n", "token \n"):
            with self.subTest(text=text):
                with open(self.path, "w", newline="") as fh:
                    fh.write(text)
                self.assertIsNone(config.read_token(self.path))


@unittest.skipUnless(POSIX, POSIX_REASON)
class SidecarTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.store = store_mod.Store(self.sb.media)
        self.id = "20261015T120000Z-0a1b2c"
        part = self.store.open_part("emmc", self.id)
        os.write(part.fd, b"RIFF")
        os.close(part.fd)
        part.commit()
        part.close()

    def test_write_read_and_clean(self):
        self.assertTrue(self.store.write_meta("emmc", self.id, {"duration_ms": 5000, "peak_dbfs": -12,
                                                                "upload": "pending", "junk": "<x>"}))
        self.assertEqual(self.store.read_meta("emmc", self.id),
                         {"duration_ms": 5000, "peak_dbfs": -12, "upload": "pending"})
        self.assertEqual(os.listdir(self.sb.tmp()), [])  # the temp was renamed away

    def test_planted_values_and_shapes_are_dropped(self):
        meta = os.path.join(self.sb.rec(), self.id + ".json")
        with open(meta, "w") as fh:
            json.dump({"duration_ms": "5", "peak_dbfs": 7, "upload": "hacked"}, fh)
        self.assertEqual(self.store.read_meta("emmc", self.id), {})
        os.unlink(meta)
        secret = os.path.join(self.sb.root, "secret.json")
        with open(secret, "w") as fh:
            json.dump({"upload": "ok"}, fh)
        os.symlink(secret, meta)
        self.assertEqual(self.store.read_meta("emmc", self.id), {})
        os.unlink(meta)
        with open(meta, "w") as fh:
            fh.write(json.dumps({"upload": "ok"}) + " " * 2000)
        self.assertEqual(self.store.read_meta("emmc", self.id), {})

    def test_no_sidecar_without_its_clip_and_none_after_it(self):
        self.assertFalse(self.store.write_meta("emmc", "20261015T130000Z-0a1b2c", {"upload": "ok"}))
        self.store.write_meta("emmc", self.id, {"upload": "ok"})
        self.assertEqual(self.store.delete(["emmc"], self.id), [self.id])
        self.assertEqual(os.listdir(self.sb.rec()), [])

    def test_orphan_sidecar_is_swept_by_retention(self):
        orphan = os.path.join(self.sb.rec(), "20261015T130000Z-0a1b2c.json")
        with open(orphan, "w") as fh:
            fh.write("{}")
        self.store.retention("emmc", keep_files=50, keep_mb=32, keep_hours=24)
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(os.path.join(self.sb.rec(), self.id + ".wav")))

    def test_upload_reads_the_bytes_written_not_a_replacement(self):
        rec_id = "20261015T130000Z-0a1b2c"
        part = self.store.open_part("emmc", rec_id)
        os.write(part.fd, b"REAL-BYTES")
        os.fsync(part.fd)
        part.remember_written(part.fd)
        os.close(part.fd)
        part.commit()
        path = os.path.join(self.sb.rec(), rec_id + ".wav")
        os.remove(path)
        with open(path, "wb") as fh:
            fh.write(b"PLANTED-FILE")
        self.assertEqual(part.read_clip(1024), b"REAL-BYTES")
        part.close()


@unittest.skipUnless(POSIX, POSIX_REASON)
class DaemonUploadTest(unittest.TestCase):
    """The real daemon loop + fake arecord, uploading to a local receiver."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.rx = Receiver()
        self.addCleanup(self.rx.close)
        self.token_path = os.path.join(self.sb.etc_dir, "upload.token")

    def start(self, guard=None, delays=(0, 0, 0), **conf):
        conf.setdefault("upload_url", self.rx.url)
        self.sb.write_conf(**conf)
        self.h = DaemonHarness(self.sb, token_path=self.token_path,
                               guard_loader=(lambda: guard) if guard is not False else (lambda: None),
                               upload_delays=delays)
        self.addCleanup(self.h.stop)
        self.h.start()
        return self.h

    def record(self):
        before = len(self.h.paho.values("last_status"))
        self.h.command("record", "2")
        self.assertTrue(wait_for(lambda: len(self.h.paho.values("last_status")) > before, 15))
        return self.h.last("last_file")

    def test_clip_uploaded_then_deleted(self):
        config.save_token(TOKEN, self.token_path)
        h = self.start(guard=OpenGuard())
        rec_id = self.record()
        self.assertTrue(h.wait_last("last_upload", "ok", 15))
        self.assertIn("pending", h.paho.values("last_upload"))
        path, headers, body = self.rx.requests[0]
        self.assertEqual(headers["X-SA02M-Recording-Id"], rec_id)
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(len(body), 64044)
        self.assertTrue(wait_for(lambda: self.sb.clips() == [], 5))  # delete_after_upload
        self.assertEqual(self.sb.sidecars(), [])
        kinds = [e["event"] for e in h.events()]
        self.assertIn("upload", kinds)
        for blob in (json.dumps(h.events()), open(self.sb.status).read(), json.dumps(h.paho.snapshot())):
            self.assertNotIn(TOKEN, blob)

    def test_token_never_in_the_log(self):
        config.save_token(TOKEN, self.token_path)
        with self.assertLogs("sa02m_audio", level="DEBUG") as cm:
            h = self.start(guard=OpenGuard())
            self.record()
            self.assertTrue(h.wait_last("last_upload", "ok", 15))
            h.stop()
        text = "\n".join(cm.output)
        self.assertIn("127.0.0.1", text)  # the host is logged
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(self.rx.url, text)  # the URL is not: only its host

    def test_kept_when_delete_after_upload_is_off_and_sidecar_says_ok(self):
        h = self.start(guard=OpenGuard(), delete_after_upload=False)
        rec_id = self.record()
        self.assertTrue(h.wait_last("last_upload", "ok", 15))
        self.assertNotIn("Authorization", self.rx.requests[0][1])
        self.assertEqual(self.sb.clips(), [rec_id + ".wav"])
        meta = store_mod.Store(self.sb.media).read_meta("emmc", rec_id)
        self.assertTrue(wait_for(lambda: store_mod.Store(self.sb.media).read_meta("emmc", rec_id).get("upload") == "ok", 5))
        self.assertEqual(meta.get("duration_ms"), 2000)

    def test_failed_keeps_the_file(self):
        self.rx.mode = "500"
        h = self.start(guard=OpenGuard())
        rec_id = self.record()
        self.assertTrue(h.wait_last("last_upload", "failed", 15))
        self.assertEqual(len(self.rx.requests), 3)
        self.assertEqual(self.sb.clips(), [rec_id + ".wav"])

    def test_no_url_policy_is_unavailable_nothing_sent(self):
        h = self.start(guard=False)
        rec_id = self.record()
        self.assertEqual(h.last("last_upload"), "unavailable")
        self.assertEqual(self.rx.requests, [])
        self.assertEqual(self.sb.clips(), [rec_id + ".wav"])

    def test_refused_url_with_the_real_policy(self):
        h = self.start(guard=upload.load_guard(RULES_DIR))
        rec_id = self.record()
        self.assertTrue(h.wait_last("last_upload", "refused", 15))
        self.assertEqual(self.rx.requests, [])
        self.assertEqual(self.sb.clips(), [rec_id + ".wav"])

    def test_unreadable_token_fails_closed(self):
        os.mkfifo(self.token_path)
        h = self.start(guard=OpenGuard())
        self.record()
        self.assertEqual(h.last("last_upload"), "failed")
        time.sleep(0.5)
        self.assertEqual(self.rx.requests, [])

    def test_no_url_is_off(self):
        h = self.start(guard=OpenGuard(), upload_url="")
        self.record()
        self.assertEqual(h.last("last_upload"), "off")
        self.assertEqual(self.rx.requests, [])

    def test_a_planted_listed_file_is_never_uploaded(self):
        os.makedirs(self.sb.rec(), exist_ok=True)
        planted = "20200101T000000Z-aaaaaa"
        with open(os.path.join(self.sb.rec(), planted + ".wav"), "wb") as fh:
            fh.write(b"RIFF planted")
        h = self.start(guard=OpenGuard(), delete_after_upload=False)
        rec_id = self.record()
        self.assertTrue(h.wait_last("last_upload", "ok", 15))
        ids = [r[1]["X-SA02M-Recording-Id"] for r in self.rx.requests]
        self.assertEqual(ids, [rec_id])
        self.assertFalse(any(b"planted" in r[2] for r in self.rx.requests))


@unittest.skipUnless(POSIX, POSIX_REASON)
class SidecarRewriteTest(unittest.TestCase):
    """Completion rewrites the sidecar from the recording, not from the file."""

    def test_a_planted_sidecar_is_not_adopted(self):
        from sa02m_audio import service

        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        daemon = service.Daemon(conf_path=sb.conf, media=sb.media, status_file=sb.status,
                                events_file=sb.events, daemon_lock=sb.lock, mic_lock=sb.mic_lock,
                                mounts_file=sb.mounts, root=sb.root, dev_of=sb.dev_of,
                                asound_dir=sb.asound, check_dependencies=lambda: [])
        daemon.conf.delete_after_upload = False
        rec_id = "20261015T120000Z-0a1b2c"
        part = daemon.store.open_part("emmc", rec_id)
        os.write(part.fd, b"RIFF")
        os.close(part.fd)
        part.commit()
        part.close()
        daemon.store.write_meta("emmc", rec_id, {"duration_ms": 30000, "peak_dbfs": 0, "upload": "ok"})
        daemon.last["last_file"] = rec_id
        daemon.on_upload(upload.UploadResult(rec_id, "emmc", "failed", "127.0.0.1: HTTP 500",
                                             2000, -12))
        self.assertEqual(daemon.store.read_meta("emmc", rec_id),
                         {"duration_ms": 2000, "peak_dbfs": -12, "upload": "failed"})
        self.assertEqual(daemon.last["last_upload"], "failed")


if __name__ == "__main__":
    unittest.main()
