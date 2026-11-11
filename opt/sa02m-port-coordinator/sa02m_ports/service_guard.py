"""Privileged helper reexec with an inherited, broker-issued mutation guard."""

import json
import os
from pathlib import Path
import subprocess
import sys

from .broker import System
from .client import LOCK_DIR, CoordinatorUnavailable, ServiceMutation
from .locks import verify_descriptor

# The wrapper (etc/sa02m-web-service-ctl.sh) relays this JSON to the panel.
# Two codes only: an outage the Operator must repair is not a busy port.
CODE_UNAVAILABLE = "coordinator_unavailable"
CODE_BUSY = "port_busy"
# The re-executed helper overran its budget (A4-10): reported as such, not as
# a busy port the Operator would wait out.
CODE_TIMEOUT = "guard_timeout"

# start/stop is bounded inside the panel's 90 s operation budget several times
# over; install/uninstall (CODESYS, Node-RED) legitimately runs for minutes and
# is bounded by the panel's 600 s budget instead, so it gets no timer here.
START_STOP_TIMEOUT_S = 300


def error_code(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return CODE_TIMEOUT
    return CODE_UNAVAILABLE if isinstance(exc, CoordinatorUnavailable) else CODE_BUSY


def verify(unit, directory=LOCK_DIR, system=None):
    if os.geteuid() != 0:
        raise PermissionError("Service mutations require root")
    canonical = (system or System()).canonical(unit)
    if os.environ.get("SA02M_SERVICE_GUARD_UNIT") != canonical:
        raise PermissionError("Wrong service mutation guard")
    fd = int(os.environ["SA02M_SERVICE_GUARD_FD"])
    verify_descriptor(fd, Path(directory) / ("service-" + canonical + ".lock"))
    return fd


def run(helper, action, identity, unit):
    if os.geteuid() != 0 or action not in {"start", "stop", "install", "uninstall"}:
        raise PermissionError("Invalid service mutation")
    if identity not in {"mplc4", "codesys", "vplc", "mqtt-bridge"}:
        raise PermissionError("Invalid UART service")
    # The installed shell entrypoint supplies its own path and validated unit.
    # All children inherit the fd: killing just the wrapper cannot unlock a
    # systemctl/init.d command still changing the service — which is why each
    # of those carries its own bound in the helper (initd_run, sc_run_slow) and
    # this timeout only stops the wrapper from being waited on forever.
    timeout = START_STOP_TIMEOUT_S if action in {"start", "stop"} else None
    with ServiceMutation(unit) as guard:
        env = {**os.environ, "SA02M_SERVICE_GUARD_FD": str(guard.fd),
               "SA02M_SERVICE_GUARD_UNIT": guard.unit}
        return subprocess.run(["/bin/sh", helper, action, identity], env=env,
                              pass_fds=(guard.fd,), check=False,
                              timeout=timeout).returncode


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--verify":
            verify(sys.argv[2])
            return 0
        if len(sys.argv) == 6 and sys.argv[1] == "--run":
            return run(*sys.argv[2:])
        raise ValueError("Invalid guard invocation")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": error_code(exc), "detail": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
