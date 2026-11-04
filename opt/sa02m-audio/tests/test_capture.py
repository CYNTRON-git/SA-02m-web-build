"""Capture: WAV header from the real sample count, peak dBFS, the outcome
table, the mic lease (contract §Формат, §Аренда микрофона)."""

from __future__ import annotations

import os
import queue
import struct
import unittest
import wave

from sa02m_audio import capture
from sa02m_audio import constants as C
from sa02m_audio import store as store_mod

from . import POSIX, POSIX_REASON
from .fakes import Sandbox, fake_arecord_dir


class PeakTest(unittest.TestCase):
    def test_silence_is_the_floor(self):
        self.assertEqual(capture.peak_of(b"\0\0" * 1000), 0)
        self.assertEqual(capture.dbfs(0), C.PEAK_FLOOR_DBFS)
        self.assertEqual(capture.dbfs(capture.peak_of(b"")), -90)

    def test_full_scale_is_zero(self):
        full = struct.pack("<4h", 0, 32767, -32768, 100)
        self.assertEqual(capture.peak_of(full), 32768)
        self.assertEqual(capture.dbfs(32768), 0)
        self.assertEqual(capture.dbfs(32767), 0)

    def test_half_scale_is_minus_six(self):
        self.assertEqual(capture.dbfs(capture.peak_of(struct.pack("<2h", 16384, -100))), -6)

    def test_one_lsb_is_clamped_to_the_floor(self):
        self.assertEqual(capture.dbfs(1), -90)


class ClassifyTest(unittest.TestCase):
    T = 64000

    def out(self, stderr="", got=64000, stop=False, interrupted=False, write_error=""):
        return capture.classify(stderr, data_bytes=got, target=self.T, stop_requested=stop,
                                interrupted=interrupted, write_error=write_error)

    def test_table(self):
        self.assertEqual(self.out(), capture.OUT_OK)
        self.assertEqual(self.out(got=40000, stop=True), capture.OUT_OK)       # ≥ 1 s kept
        self.assertEqual(self.out(got=31998, stop=True), capture.OUT_SHORT)    # < 1 s discarded
        self.assertEqual(self.out(got=0, stderr="audio open error: Device or resource busy"),
                         capture.OUT_MIC_BUSY)
        self.assertEqual(self.out(got=0, stderr="audio open error: No such file or directory"),
                         capture.OUT_NO_MIC)
        self.assertEqual(self.out(got=100), capture.OUT_ERROR)                 # ended early
        self.assertEqual(self.out(interrupted=True), capture.OUT_INTERRUPTED)
        self.assertEqual(self.out(write_error="No space left on device"), capture.OUT_ERROR)

    def test_argv_bounds_arecord_with_timeout(self):
        argv = capture.arecord_argv("plughw:CARD=Microphone,DEV=0", 10)
        self.assertEqual(argv[:4], ["timeout", "-k", "2", "15"])
        self.assertEqual(argv[4:], ["arecord", "-q", "-D", "plughw:CARD=Microphone,DEV=0", "-f",
                                    "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"])


@unittest.skipUnless(POSIX, POSIX_REASON)
class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def test_exclusive_and_released(self):
        a = capture.MicLease(self.sb.mic_lock)
        b = capture.MicLease(self.sb.mic_lock)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertFalse(b.held)
        a.release()
        self.assertTrue(b.acquire())
        b.release()
        self.assertEqual(oct(os.stat(self.sb.mic_lock).st_mode & 0o777), "0o644")

    def test_lease_is_a_flock_other_processes_see(self):
        import subprocess
        import sys

        a = capture.MicLease(self.sb.mic_lock)
        self.assertTrue(a.acquire())
        code = ("import fcntl,os,sys; fd=os.open(sys.argv[1], os.O_RDONLY)\n"
                "try:\n fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); print('got')\n"
                "except OSError: print('busy')\n")
        out = subprocess.run([sys.executable, "-c", code, self.sb.mic_lock],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        self.assertEqual(out, "busy")
        a.release()
        out = subprocess.run([sys.executable, "-c", code, self.sb.mic_lock],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        self.assertEqual(out, "got")


@unittest.skipUnless(POSIX, POSIX_REASON)
class CaptureRunTest(unittest.TestCase):
    """The real Capture against the fake arecord through the real `timeout`."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        bindir = fake_arecord_dir(self.sb.root)
        saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_ARECORD_MODE", "FAKE_ARECORD_REALTIME")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
        self.store = store_mod.Store(self.sb.media)

    def run_capture(self, mode, seconds=2, stop_after=None, realtime=False):
        os.environ["FAKE_ARECORD_MODE"] = mode
        os.environ["FAKE_ARECORD_REALTIME"] = "1" if realtime else "0"
        rec_id = store_mod.new_id()
        part = self.store.open_part(C.MEDIUM_EMMC, rec_id)
        results = queue.Queue()
        cap = capture.Capture("plughw:CARD=Microphone,DEV=0", seconds, part, results.put)
        cap.start()
        if stop_after is not None:
            import time
            time.sleep(stop_after)
            cap.request_stop()
        result = results.get(timeout=20)
        return result, part

    def test_wav_header_is_read_back_by_wave(self):
        result, part = self.run_capture("tone", seconds=2)
        self.assertEqual(result.outcome, capture.OUT_OK)
        self.assertEqual(result.data_bytes, 64000)
        self.assertEqual(result.duration_ms, 2000)
        self.assertEqual(result.peak_dbfs, -6)
        path = os.path.join(self.sb.tmp(), part.name)
        self.assertEqual(os.path.getsize(path), 64044)
        with wave.open(path, "rb") as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                             (1, 2, 16000, 32000))
        part.close()

    def test_silent_mic_reports_the_floor(self):
        result, part = self.run_capture("silence", seconds=2)
        self.assertEqual((result.outcome, result.peak_dbfs), (capture.OUT_OK, -90))
        part.close()

    def test_stop_keeps_what_is_there(self):
        result, part = self.run_capture("tone", seconds=10, stop_after=1.6, realtime=True)
        self.assertEqual(result.outcome, capture.OUT_OK)
        self.assertGreaterEqual(result.duration_ms, 1000)
        self.assertLess(result.duration_ms, 10000)
        with wave.open(os.path.join(self.sb.tmp(), part.name), "rb") as w:
            self.assertEqual(w.getnframes() * 1000 // 16000, result.duration_ms)
        part.close()

    def test_busy_device(self):
        result, part = self.run_capture("busy")
        self.assertEqual(result.outcome, capture.OUT_MIC_BUSY)
        self.assertIn("busy", result.detail.lower())
        part.close()

    def test_child_dying_early_is_an_error(self):
        result, part = self.run_capture("die", seconds=5)
        self.assertEqual(result.outcome, capture.OUT_ERROR)
        self.assertEqual(result.data_bytes, 64000)
        part.close()

    def test_overrun_is_flagged(self):
        result, part = self.run_capture("xrun")
        self.assertEqual(result.outcome, capture.OUT_OK)
        self.assertTrue(result.xrun)
        part.close()


if __name__ == "__main__":
    unittest.main()
