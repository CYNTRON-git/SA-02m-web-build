# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from sa02m_flasher import mplc_lease


class TestMplcLease(unittest.TestCase):
    def test_stop_service_uses_process_path_for_mplc(self) -> None:
        with (
            patch.object(mplc_lease, "resolve_service_name", return_value="mplc.service"),
            patch.object(mplc_lease, "_pkill_names", return_value=True) as pkill_mock,
            patch.object(mplc_lease, "_proc_is_running", return_value=False) as proc_mock,
            patch.object(mplc_lease, "_run") as run_mock,
        ):
            ok = mplc_lease.stop_service("mplc.service")

        self.assertTrue(ok)
        pkill_mock.assert_called_once_with(["mplc", "mplc4", "mplc_monitor"])
        self.assertGreaterEqual(proc_mock.call_count, 1)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
