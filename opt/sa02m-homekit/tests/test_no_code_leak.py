"""The setup code reaches only setup.json (D5): never stdout/stderr (the
journal), never a log record, never status.json / projection.json.

Two layers: the daemon lifecycle with a fake runner (pure — runs everywhere),
and the pyhap glue (deps-guarded — skips loudly without HAP-python): the
library's `setup_message()` prints the code to stdout, `SafeBridge` must not.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import tempfile
import unittest

from . import _deps
from ._harness import Harness


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def captured():
    """stdout, stderr and every log record (DEBUG and up, all loggers)."""
    root = logging.getLogger()
    handler = _Capture()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield handler, out, err
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _forms(code: str):
    return (code, code.replace("-", ""))


class DaemonLeakTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_lifecycle_never_emits_the_code(self):
        self.h.conf(enabled=True)
        with captured() as (logs, out, err):
            self.h.start()
            self.assertTrue(self.h.wait_for(self.h.setup_exists))
            code = self.h.runners[0].pincode
            self.assertEqual(self.h.setup_json()["code"], code)  # the one sanctioned home
            self.h.runners[0].pair()
            self.assertTrue(self.h.wait_for(lambda: not self.h.setup_exists()))
            self.assertEqual(self.h.finish(), 0)
        self.assertTrue(logs.messages, "no log record captured — the check saw nothing")
        emitted = "\n".join(logs.messages) + out.getvalue() + err.getvalue()
        for path in (self.h.status.status_path, self.h.status.projection_path):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    emitted += fh.read()
        for form in _forms(code):
            self.assertNotIn(form, emitted)


@unittest.skipUnless(_deps.HAVE_PYHAP, _deps.PYHAP_SKIP)
class GlueLeakTests(unittest.TestCase):
    CODE = "482-19-305"

    def setUp(self):
        from sa02m_homekit import bridge

        self.bridge = bridge
        self.dir = tempfile.mkdtemp()
        self.driver = None

    def tearDown(self):
        if self.driver is not None:
            if self.driver.executor is not None:
                self.driver.executor.shutdown(wait=False)
            self.driver.loop.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_setup_message_is_overridden(self):
        from pyhap.accessory import Accessory, Bridge

        self.assertIsNot(self.bridge.SafeBridge.setup_message, Bridge.setup_message)
        self.assertIsNot(self.bridge.SafeBridge.setup_message, Accessory.setup_message)

    def test_building_and_announcing_the_bridge_prints_no_code(self):
        runner = self.bridge.BridgeRunner(
            persist_file=os.path.join(self.dir, "state.json"), address="127.0.0.1", port=21064,
            bridge_name="SA-02m 110002", specs=[], write_cb=lambda *a: None, firmware="1.0.6",
            new_code=self.CODE, new_setup_id="7OSX")
        with captured() as (logs, out, err):
            self.driver = runner._build()
            self.driver.accessory.setup_message()
        self.assertEqual(self.driver.state.pincode, self.CODE.encode("ascii"))
        emitted = "\n".join(logs.messages) + out.getvalue() + err.getvalue()
        for form in _forms(self.CODE):
            self.assertNotIn(form, emitted)


def setUpModule():
    if not _deps.HAVE_PYHAP:
        _deps.announce(_deps.PYHAP_SKIP)


if __name__ == "__main__":
    unittest.main()
