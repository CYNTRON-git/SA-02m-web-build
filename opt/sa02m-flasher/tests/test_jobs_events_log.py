# -*- coding: utf-8 -*-
"""JobManager: запись SSE-событий в events.log (JSON Lines)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from sa02m_flasher.jobs import JobKind, JobManager


class TestEventsLog(unittest.TestCase):
    def test_emit_appends_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "events.log"
            mgr = JobManager(events_log_path=log_path)

            def run_fn(job, ctx):
                ctx["log"]("hello", "info")

            job = mgr.submit(JobKind.SCAN, "COM1", {}, run_fn)
            # дождаться потока
            for _ in range(50):
                time.sleep(0.05)
                if log_path.is_file() and log_path.stat().st_size > 0:
                    break
            mgr.cancel(job.id)
            for _ in range(50):
                time.sleep(0.05)
                t = mgr.get(job.id)
                if t and t.state.value in ("done", "cancelled", "error"):
                    break

            self.assertTrue(log_path.is_file(), "events.log должен создаваться при первой записи")
            lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertGreaterEqual(len(lines), 1)
            first = json.loads(lines[0])
            self.assertEqual(first["job_id"], job.id)
            self.assertIn("kind", first)
            self.assertIn("message", first)


if __name__ == "__main__":
    unittest.main()
