#!/usr/bin/env python3
# comment-mutation-proof-exempt: Fixed native acceptance launcher. Real prerequisite checks precede any sudo or test dispatch; failure never becomes a skip. Child behavioral suites own their unit/file assertions.
"""Mandatory ship acceptance in an explicitly provisioned disposable PID1 container."""

import os
from pathlib import Path
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TESTS = {"system-actions": ("python3", "scripts/dev/test-system-actions-systemd.py"),
         "mplc": ("python3", "scripts/dev/test-mplc-systemd.py"),
         "journal": ("python3", "scripts/dev/test-journal-completion-systemd.py"),
         "service-worker": ("python3", "scripts/dev/test-service-worker.py"),
         "runtime-transition": ("python3", "scripts/dev/test-runtime-transition-systemd.py"),
         "runtime-image": ("python3", "scripts/dev/test-runtime-image-seal.py"),
         "old-online": ("python3", "scripts/dev/test-runtime-old-online.py"),
         "rootfs-loop": ("bash", "scripts/dev/test-rootfs-policy-loop.sh")}


def prerequisite():
    if os.name != "posix" or os.environ.get("SA02M_DISPOSABLE_SYSTEMD_TEST") != "1":
        return False
    try:
        marker = Path("/run/.containerenv")
        info = marker.lstat()
        return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                and Path("/run/systemd/container").read_text().strip() == "podman"
                and Path("/proc/1/comm").read_text().strip() == "systemd"
                and os.stat(ROOT).st_dev == os.stat("/").st_dev
                and not os.path.lexists("/etc/armbian-release") and not os.path.lexists("/dev/watchdog"))
    except OSError:
        return False


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in TESTS:
        print("invalid_disposable_test", file=sys.stderr)
        return 1
    if not prerequisite():
        print("missing_disposable_pid1: require explicit opt-in, Podman PID1, copied source and no board environment", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        command = ["sudo", "-n", "/usr/bin/env", "SA02M_DISPOSABLE_SYSTEMD_TEST=1"]
        if sys.argv[1] == "old-online" and "SA02M_RELEASED_UPDATER_BLOBS" in os.environ:
            command.append("SA02M_RELEASED_UPDATER_BLOBS=" + os.environ["SA02M_RELEASED_UPDATER_BLOBS"])
        command += ["/usr/bin/python3", "-I", str(Path(__file__).resolve()), sys.argv[1]]
    else:
        executable, relative = TESTS[sys.argv[1]]
        command = ["/usr/bin/" + executable, str(ROOT / relative)]
    environment = {**os.environ, "SA02M_JOURNAL_PID1_TEST": "disposable-no-host-mounts"}
    try:
        result = subprocess.call(command, cwd=ROOT, env=environment)
    except OSError as exc:
        print("disposable_test_unavailable: " + type(exc).__name__, file=sys.stderr)
        return 1
    if result:
        print("disposable_test_failed: " + sys.argv[1] + " status=" + str(result), file=sys.stderr)
    return 0 if result == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())  # exit-status: invalid prerequisites, sudo/child failures and skips normalize to failure.
