"""Pins the conftest host-systemctl fence: the default stub is in place, the
telemetry path that used to reach the host binary now hits the stub, and a
path that bypasses the stub is refused rather than executed.
"""
import importlib.util
import os
import subprocess

import pytest

from tests.conftest import HostSystemctlBlocked

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.environ.get("SA02M_AGENT_PATH") or os.path.join(AGENT_DIR, "sa02m-cloud-agent.py")

_spec = importlib.util.spec_from_file_location("sa02m_cloud_agent_no_host", AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)
REAL_SYSTEMCTL = agent._systemctl


def test_telemetry_services_probe_hits_the_stub(stub_agent_systemctl):
    # The canary defect: collect_telemetry's services_ok probe exec'd the host
    # systemctl. It must land on the stub, and read as "not ok" (stub rc 1).
    t, _ = agent.collect_telemetry()
    assert ("is-active", "--quiet", "nginx") in stub_agent_systemctl.calls
    assert t["services_ok"] is False


def test_real_wrapper_is_refused_not_executed(host_systemctl_guard):
    # The wrapper swallows Exception; the guard's BaseException must get through.
    with pytest.raises(HostSystemctlBlocked):
        REAL_SYSTEMCTL("is-active", "--quiet", "nginx")
    assert len(host_systemctl_guard) == 1
    host_systemctl_guard.clear()  # expected attempt; keep teardown green


@pytest.mark.parametrize("call", [
    lambda: subprocess.run(["systemctl", "restart", "nginx"]),
    lambda: subprocess.run(["/usr/bin/systemctl", "stop", "x"]),
    lambda: subprocess.run("systemctl daemon-reload", shell=True),
    lambda: subprocess.check_output(["systemctl", "show", "x"]),
    lambda: os.system("systemctl enable x"),
])
def test_every_exec_route_is_refused(call, host_systemctl_guard):
    with pytest.raises(HostSystemctlBlocked):
        call()
    assert len(host_systemctl_guard) == 1
    host_systemctl_guard.clear()


def test_other_commands_still_run(host_systemctl_guard):
    # The guard is scoped to systemctl — it must not break the suite's other
    # subprocess use (a real, harmless exec).
    r = subprocess.run(["true"])
    assert r.returncode == 0
    assert host_systemctl_guard == []
