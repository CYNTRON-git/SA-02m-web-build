# -*- coding: utf-8 -*-
"""Contract test for GET /health's `poll_locked` (docs/contracts/flasher-health.md).

WHY THIS EXISTS
The root-side RS-485 port-lease probe (etc/sa02m-web-service-ctl.sh) has no web
session, so it reads the lease from the one route served before _check_auth().
That probe was dead from 2026-07-12 to 2026-08-06 because nothing asserted the
daemon side of the handshake. These are the assertions that were missing.

WHY IT EXECUTES EXTRACTED SOURCE INSTEAD OF IMPORTING service.py
`sa02m_flasher.service` cannot be imported off-Linux at all (`import grp`), and
`import cgi` additionally breaks it on Python >= 3.13 — so no test in this
directory imports it, which is exactly why the py-unit-flasher row is green
today. Importing it here would turn that row red on every dev box. Instead we
extract the SHIPPED function bodies with `ast` and exec them against stubs —
the same "run the shipped code against a sandbox" idiom the bash harnesses in
scripts/dev/ use. The logic under test is the real logic, byte for byte.
"""
from __future__ import annotations

import ast
import os
import re
import tempfile
import types
import unittest
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows dev box — the flock cases skip, the rest run.
    fcntl = None  # type: ignore[assignment]


def _flock_enforced() -> bool:
    """Does flock() on this platform ACTUALLY exclude a second descriptor?

    `import fcntl` succeeding is not the question. Five sibling test modules
    (test_runner_app_line, test_scan_persist, test_fw_info_reg290_signature, …)
    install a NO-OP `fcntl` stub into sys.modules so runner.py can import on
    Windows. Discovery imports them before this module, so here `fcntl` is that
    stub: present, and silently locking nothing. Probing the module's presence
    would run the two flock cases against a lock that cannot exclude anything —
    they would fail on Windows and, worse, could pass vacuously elsewhere.
    So probe the CAPABILITY: take a lock, then try to take it again.
    """
    if fcntl is None:
        return False
    tmp = tempfile.TemporaryDirectory()
    try:
        p = Path(tmp.name) / "probe.lock"
        p.touch()
        a = os.open(str(p), os.O_RDWR)
        b = os.open(str(p), os.O_RDWR)
        try:
            fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False  # second lock granted → not enforced (the stub)
            except OSError:
                return True
        except OSError:
            return False
        finally:
            os.close(a)
            os.close(b)
    finally:
        tmp.cleanup()


_FLOCK = _flock_enforced()

_PKG = Path(__file__).resolve().parent.parent / "sa02m_flasher"
_SERVICE_SRC = (_PKG / "service.py").read_text(encoding="utf-8")
_VERSION = re.search(r'^__version__\s*=\s*"([^"]+)"', (_PKG / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)


def _extract(*names: str) -> types.SimpleNamespace:
    """exec the named top-level functions of service.py in an isolated namespace."""
    tree = ast.parse(_SERVICE_SRC)
    lines = _SERVICE_SRC.splitlines()
    chunks = []
    for want in names:
        node = next(
            (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == want),
            None,
        )
        if node is None:
            raise AssertionError(
                f"service.py no longer defines {want}() — the /health lease contract lost its implementation"
            )
        chunks.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))
    ns: dict = {
        "os": os,
        "Path": Path,
        "__version__": _VERSION,
        "Dict": dict,
        "Any": object,
    }
    # `from __future__ import annotations` so the FlasherConfig/ServiceContext
    # annotations stay strings and need no real class.
    exec("from __future__ import annotations\n" + "\n\n".join(chunks), ns)  # noqa: S102
    return types.SimpleNamespace(**{n: ns[n] for n in names})


_FN = _extract("_poll_lock_held", "health_payload")


class _Jobs:
    def __init__(self, active):
        self._active = active

    def list_active_jobs(self):
        return list(self._active)


def _ctx(lock_dir: Path, active=()):
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(lock_dir=lock_dir),
        jobs=_Jobs(active),
    )


class TestHealthPayload(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── the response shape the shell probe and README both depend on ────────
    def test_shape_is_exactly_three_keys(self) -> None:
        """An UNAUTHENTICATED route must expose the boolean and nothing else.

        /status may list active jobs (ports, firmware names, addresses) because
        it sits behind auth. /health does not, so a future edit that widens it
        into "just return the status payload" would publish job detail to every
        caller of a 0660 socket. Pinned as an exact key set, not a subset.
        """
        payload = _FN.health_payload(_ctx(self.lock_dir))
        self.assertEqual(set(payload), {"ok", "version", "poll_locked"})

    def test_ok_and_version_preserved(self) -> None:
        """The pre-existing contract (README §HTTP API) is additive-only."""
        payload = _FN.health_payload(_ctx(self.lock_dir))
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["version"], _VERSION)

    def test_idle_daemon_reports_not_locked(self) -> None:
        payload = _FN.health_payload(_ctx(self.lock_dir))
        self.assertIs(payload["poll_locked"], False)

    def test_active_job_reports_locked(self) -> None:
        """Second half of the union: a job accepted but not yet flock'd."""
        payload = _FN.health_payload(_ctx(self.lock_dir, active=[{"id": "job-1"}]))
        self.assertIs(payload["poll_locked"], True)

    def test_poll_locked_is_a_bool_not_a_truthy_list(self) -> None:
        """The shell probe substring-matches `true`/`false`; a JSON list here
        would serialise as `[...]` and land in the fail-closed branch forever."""
        payload = _FN.health_payload(_ctx(self.lock_dir, active=[{"id": "job-1"}]))
        self.assertIsInstance(payload["poll_locked"], bool)

    @unittest.skipUnless(_FLOCK, "flock is not enforced here (absent, or the sibling tests' no-op stub)")
    def test_held_flock_reports_locked_with_no_active_job(self) -> None:
        """First half of the union — and the reason /health carries poll_locked
        rather than the narrower `busy`. A port can be flock'd with no job in
        the active list (the inter-device settle window inside a batch), and
        `busy` alone would answer "free" while the line is held."""
        lock = self.lock_dir / "sa02m-flasher-COM1.lock"
        lock.touch()
        fd = os.open(str(lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            payload = _FN.health_payload(_ctx(self.lock_dir))
            self.assertIs(payload["poll_locked"], True)
        finally:
            os.close(fd)

    @unittest.skipUnless(_FLOCK, "flock is not enforced here (absent, or the sibling tests' no-op stub)")
    def test_stale_unheld_lock_file_is_not_locked(self) -> None:
        """A leftover lock file is not a held lease — otherwise every board
        would grey out its Пуск buttons permanently after one flash."""
        (self.lock_dir / "sa02m-flasher-COM1.lock").touch()
        payload = _FN.health_payload(_ctx(self.lock_dir))
        self.assertIs(payload["poll_locked"], False)

    def test_missing_lock_dir_is_not_locked(self) -> None:
        payload = _FN.health_payload(_ctx(self.lock_dir / "nope"))
        self.assertIs(payload["poll_locked"], False)


class TestDispatchOrdering(unittest.TestCase):
    """Source-level pins. The whole point of /health is that it answers BEFORE
    the auth check; an edit that reorders the dispatch would re-kill the gate
    silently, and no behavioural test of the payload can see it."""

    def _line_of(self, needle: str) -> int:
        for i, line in enumerate(_SERVICE_SRC.splitlines(), 1):
            if needle in line:
                return i
        raise AssertionError(f"service.py no longer contains {needle!r}")

    def test_health_is_dispatched_before_check_auth(self) -> None:
        health = self._line_of('p == "/health"')
        auth = self._line_of("if not self._check_auth()")
        self.assertLess(
            health,
            auth,
            "/health moved below the auth check — the root-side port-lease probe has no session "
            "and would get 401 forever, exactly the 2026-07-12 regression",
        )

    def test_health_route_uses_health_payload(self) -> None:
        """A hand-inlined dict here would drift from the /status union."""
        idx = self._line_of('p == "/health"')
        following = "\n".join(_SERVICE_SRC.splitlines()[idx : idx + 3])
        self.assertIn("health_payload(ctx)", following)

    def test_one_owner_of_the_lease_fact(self) -> None:
        """health_payload and _handle_status must compute poll_locked the same
        way — two divergent definitions of "is the line held" is the defect
        class this whole change exists to remove."""
        expr = "_poll_lock_held(ctx.cfg) or bool("
        self.assertEqual(
            _SERVICE_SRC.count(expr),
            2,
            "the poll_locked union no longer appears in exactly its two known homes "
            "(_handle_status and health_payload) — check they did not diverge",
        )


if __name__ == "__main__":
    unittest.main()
