"""Suite-wide host-service-control fence for the cloud-agent tests.

A unit test must never reach the host's real `systemctl`: the quality row runs
on a developer machine and in CI, where even a read-only `is-active` is a probe
of someone else's services, and the next code path to reach the wrapper may be
`restart` / `stop` / `disable`. Found by a PATH canary: `collect_telemetry`
(`services_ok`) reached the real binary twice per run with every test green.

Two layers, both autouse so a test module added later is covered without
opting in:

1. STUB — the test module's loaded agent (every module here binds it as
   `agent`) gets `_systemctl` replaced by a recorder answering 1, the value the
   real wrapper returns when systemctl is unavailable, so nothing pretends a
   unit is healthy. A test that needs other answers still monkeypatches
   `agent._systemctl` itself; that overrides this default.
2. GUARD — `subprocess.Popen` (under `run` / `call` / `check_output`) and
   `os.system` refuse a `systemctl` argv by raising `HostSystemctlBlocked`, a
   BaseException so the wrapper's own `except Exception` cannot swallow it; the
   attempt is also recorded and fails the test at teardown, so code that
   catches everything cannot hide it either. This is what catches a path that
   bypasses the stub (a direct `subprocess.run(["systemctl", ...])`, a module
   that binds the agent under another name, a test restoring the real wrapper).
"""
import os
import shlex
import subprocess

import pytest


class HostSystemctlBlocked(BaseException):
    """A test tried to exec the host's real systemctl."""


def _argv0(args, kwargs):
    exe = kwargs.get("executable")
    if exe:
        return os.path.basename(str(exe))
    if isinstance(args, (bytes, str)):
        text = args.decode() if isinstance(args, bytes) else args
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        return os.path.basename(parts[0]) if parts else ""
    try:
        first = list(args)[0]
    except (TypeError, IndexError):
        return ""
    if isinstance(first, bytes):
        first = first.decode()
    return os.path.basename(str(first))


class _SystemctlStub:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return 1


@pytest.fixture(autouse=True)
def host_systemctl_guard(monkeypatch):
    """Yields the list of blocked attempts; teardown fails on a non-empty one."""
    attempts = []
    real_popen = subprocess.Popen
    real_system = os.system

    class GuardedPopen(real_popen):
        def __init__(self, args, *a, **kw):
            if _argv0(args, kw) == "systemctl":
                attempts.append(args)
                raise HostSystemctlBlocked(f"test reached host systemctl: {args!r}")
            super().__init__(args, *a, **kw)

    def guarded_system(command):
        if _argv0(command, {}) == "systemctl":
            attempts.append(command)
            raise HostSystemctlBlocked(f"test reached host systemctl: {command!r}")
        return real_system(command)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(os, "system", guarded_system)
    yield attempts
    if attempts:
        pytest.fail(f"host systemctl invoked by the test (blocked): {attempts!r}")


@pytest.fixture(autouse=True)
def stub_agent_systemctl(request, monkeypatch, host_systemctl_guard):
    """Default `_systemctl` stub on the test module's agent; yields the stub
    (None when the module binds no agent)."""
    agent = getattr(request.module, "agent", None)
    if agent is None or not callable(getattr(agent, "_systemctl", None)):
        yield None
        return
    stub = _SystemctlStub()
    monkeypatch.setattr(agent, "_systemctl", stub)
    yield stub
