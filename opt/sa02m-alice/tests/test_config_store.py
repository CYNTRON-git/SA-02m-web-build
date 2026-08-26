"""Unit tests: conf writes must not strand ownership/mode or drop settings.

Root cause pinned here (1.0.6.14): the client conf is written by BOTH root
(web-service-ctl Пуск/Стоп sync, config API service) and www-data (the Alice
card CGI). `_atomic_write` used to install the temp file writer-owned with a
fixed 0640 — a root-side write left the conf root:root 0640 and the www-data
web layer could no longer read or write it. It now preserves the existing
file's mode (and owner, best-effort) across the replace. set_client_enabled
must also keep operator-set keys (it loads the existing conf over defaults).
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock


def _load_config_store(etc_dir: str):
    """(Re)import the module tree with SA02M_ALICE_ETC pointing at etc_dir."""
    import importlib
    import sa02m_alice.common.constants as constants
    import sa02m_alice.common.config_store as config_store

    with mock.patch.dict(os.environ, {"SA02M_ALICE_ETC": etc_dir}):
        importlib.reload(constants)
        importlib.reload(config_store)
    return constants, config_store


@unittest.skipUnless(os.name == "posix", "POSIX file modes (target is Linux)")
class AtomicWritePreservesMode(unittest.TestCase):
    def test_existing_mode_survives_replace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, cs = _load_config_store(td)
            path = os.path.join(td, "sa02m-alice-client.conf")
            cs._atomic_write(path, "[client]\nclient_enabled = false\n")
            os.chmod(path, 0o660)  # the installer's provisioned mode
            cs._atomic_write(path, "[client]\nclient_enabled = true\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o660)

    def test_fresh_file_gets_default_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, cs = _load_config_store(td)
            path = os.path.join(td, "fresh.conf")
            cs._atomic_write(path, "data")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)


class SetClientEnabledKeepsSettings(unittest.TestCase):
    def test_operator_keys_survive_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            constants, cs = _load_config_store(td)
            with open(constants.CLIENT_CONF, "w", encoding="utf-8") as fh:
                fh.write(
                    "[client]\nclient_enabled = false\n"
                    "log_level = DEBUG\ncustom_key = keep-me\n"
                )
            cs.set_client_enabled(True)
            cfg = cs.default_client_cfg()
            self.assertTrue(cfg.getboolean("client", "client_enabled"))
            self.assertEqual(cfg.get("client", "log_level"), "DEBUG")
            self.assertEqual(cfg.get("client", "custom_key"), "keep-me")
            cs.set_client_enabled(False)
            cfg = cs.default_client_cfg()
            self.assertFalse(cfg.getboolean("client", "client_enabled"))
            self.assertEqual(cfg.get("client", "custom_key"), "keep-me")


if __name__ == "__main__":
    unittest.main()
