"""budget.py: per-UTC-day accounting, persistence, the 800/1000 policy, 429."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import tempfile
import unittest

from sa02m_homeconnect import constants as C
from sa02m_homeconnect.budget import Budget, BudgetExceeded, next_utc_midnight

NOON = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc).timestamp()


class Clock:
    def __init__(self, t: float = NOON) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class BudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "budget.json")
        self.clock = Clock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def budget(self) -> Budget:
        return Budget(self.path, clock=self.clock)

    def test_every_charge_persists_and_survives_restart(self) -> None:
        b = self.budget()
        for _ in range(3):
            b.charge(C.KIND_API)
        b.charge(C.KIND_SSE)
        b.charge(C.KIND_TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        again = self.budget()
        self.assertEqual(again.used, 5)
        self.assertEqual(again.by_kind, {"api": 3, "sse": 1, "token": 1})

    def test_local_budget_stops_api_but_not_stream_or_token(self) -> None:
        b = self.budget()
        b.used = C.LOCAL_BUDGET
        with self.assertRaises(BudgetExceeded) as ctx:
            b.charge(C.KIND_API)
        self.assertEqual(ctx.exception.reason, C.REASON_BUDGET_LOCAL)
        self.assertEqual(ctx.exception.until, next_utc_midnight(NOON))
        self.assertEqual(b.used, C.LOCAL_BUDGET)  # a refused call is not counted
        b.charge(C.KIND_SSE)
        b.charge(C.KIND_TOKEN)
        self.assertEqual(b.used, C.LOCAL_BUDGET + 2)

    def test_daily_limit_stops_everything(self) -> None:
        b = self.budget()
        b.used = C.DAILY_LIMIT
        for kind in C.KINDS:
            with self.assertRaises(BudgetExceeded) as ctx:
                b.charge(kind)
            self.assertEqual(ctx.exception.reason, C.REASON_DAILY_LIMIT)

    def test_retry_after_blocks_every_kind_and_persists(self) -> None:
        b = self.budget()
        until = b.block_for(120)
        self.assertEqual(until, int(NOON) + 120)
        for kind in C.KINDS:
            with self.assertRaises(BudgetExceeded) as ctx:
                self.budget().check(kind)
            self.assertEqual(ctx.exception.reason, C.REASON_RETRY_AFTER)
        self.clock.t += 121
        self.budget().check(C.KIND_API)

    def test_utc_day_rollover_resets(self) -> None:
        b = self.budget()
        b.used = C.DAILY_LIMIT
        b._persist()
        self.clock.t = next_utc_midnight(NOON) + 1
        again = self.budget()
        self.assertEqual(again.used, 0)
        again.charge(C.KIND_API)

    def test_untrusted_file_counts_as_local_budget(self) -> None:
        with open(self.path, "w") as fh:
            fh.write("{broken")
        os.chmod(self.path, 0o600)
        b = self.budget()
        self.assertTrue(b.untrusted)
        self.assertEqual(b.used, C.LOCAL_BUDGET)
        with self.assertRaises(BudgetExceeded):
            b.charge(C.KIND_API)

    def test_foreign_mode_file_is_untrusted(self) -> None:
        with open(self.path, "w") as fh:
            json.dump({"day": "2026-09-28", "used": 0, "by_kind": {}}, fh)
        os.chmod(self.path, 0o666)
        self.assertEqual(self.budget().used, C.LOCAL_BUDGET)

    def test_snapshot_fields(self) -> None:
        b = self.budget()
        b.charge(C.KIND_API)
        snap = b.snapshot()
        self.assertEqual(snap["used"], 1)
        self.assertEqual(snap["remaining"], C.DAILY_LIMIT - 1)
        self.assertEqual(snap["limit"], 1000)
        self.assertEqual(snap["local"], 800)
        self.assertEqual(snap["day"], "2026-09-28")


if __name__ == "__main__":
    unittest.main()
