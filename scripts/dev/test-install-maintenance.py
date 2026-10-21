#!/usr/bin/env python3
# comment-mutation-proof-exempt: behavioral fault injection of the installer guard with simulated systemd state and real temporary drop-in/journal files; no source-text acceptance pins.
"""Installer guards preserve operator decisions on success, failure and recovery."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch

try:
    import fcntl  # noqa: F401 - the wrapper's lock idiom; Windows CPython has none
except ImportError:
    # run.mjs SKIP_EXIT: a skip is reported as a skip, never as a pass.
    print("SKIP  install-maintenance: fcntl unavailable (Windows CPython) - the wrapper is Linux-only; run under WSL / Linux CI")
    sys.exit(77)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("maintenance", ROOT / "scripts/install-maintenance.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class System:
    def __init__(self, run):
        self.run = run
        self.states = {
            "nginx.service": {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"},
            "sa02m-flasher.service": {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "disabled"},
            "sa02m-serial-gateway.service": {"LoadState": "masked", "ActiveState": "inactive", "UnitFileState": "masked"},
        }
        self.calls = []

    def ctl(self, verb, *args):
        self.calls.append((verb, args))
        if verb == "daemon-reload":
            return ""
        unit = args[0] if verb == "show" else args[-1]
        state = self.states.setdefault(unit, {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": ""})
        if verb == "show":
            if "--value" in args:
                return state[args[args.index("-p") + 1]]
            return "\n".join(k + "=" + v for k, v in state.items())
        if verb == "stop":
            state["ActiveState"] = "inactive"
        elif verb == "start":
            if not list((self.run / (unit + ".d")).glob("sa02m-install-*.conf")):
                state["ActiveState"] = "active"
        elif verb == "unmask":
            if state["UnitFileState"].startswith("masked"):
                state["UnitFileState"] = "disabled"
        elif verb in {"mask", "enable", "disable"}:
            state["UnitFileState"] = {"mask": "masked", "enable": "enabled", "disable": "disabled"}[verb]
            if "--runtime" in args:
                state["UnitFileState"] += "-runtime"
        return ""


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.system = System(self.root)
        self.mock = patch.object(m, "ctl", self.system.ctl)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def guard(self):
        return m.Guard(tuple(self.system.states), run=self.root, journal=self.root / "recovery.json")

    def test_guard_survives_infra_start_and_restores_active_disabled_and_mask(self):
        initial = json.loads(json.dumps(self.system.states))
        guard = self.guard()
        guard.enter()
        guard.stop(guard.units)
        for unit in guard.units:
            self.system.ctl("unmask", unit)
            self.system.ctl("enable", unit)
            self.system.ctl("start", unit)
            self.assertEqual(self.system.states[unit]["ActiveState"], "inactive")
        guard.close()
        for unit, state in initial.items():
            self.assertEqual(self.system.states[unit]["UnitFileState"], state["UnitFileState"])
            self.assertEqual(self.system.states[unit]["ActiveState"], state["ActiveState"])
        self.assertFalse(guard.journal.exists())
        self.assertFalse(list(self.root.glob("*.d/*.conf")))

    def test_exception_and_keyboard_interrupt_restore(self):
        for failure in (RuntimeError, KeyboardInterrupt):
            with self.subTest(failure=failure):
                guard = self.guard()
                with self.assertRaises(failure):
                    with contextlib.ExitStack() as stack:
                        stack.callback(guard.close)
                        guard.enter()
                        guard.stop(guard.units)
                        raise failure("installer interrupted")
                self.assertEqual(self.system.states["sa02m-flasher.service"]["ActiveState"], "active")
                self.assertEqual(self.system.states["sa02m-flasher.service"]["UnitFileState"], "disabled")
                self.assertFalse(guard.journal.exists())

    def test_sigkill_recovery_uses_persisted_state(self):
        guard = self.guard()
        guard.enter()
        guard.stop(guard.units)
        # No finally: a new process recovers solely from the journal.
        replacement = self.guard()
        replacement.recover()
        self.assertEqual(self.system.states["sa02m-flasher.service"]["ActiveState"], "active")
        self.assertEqual(self.system.states["sa02m-serial-gateway.service"]["UnitFileState"], "masked")
        self.assertFalse(replacement.journal.exists())

    def test_installed_infra_does_not_resurrect_retired_fcgi_socket(self):
        self.system.states["fcgiwrap.socket"] = {
            "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
        guard = self.guard()
        guard.enter()
        guard.stop(guard.units)
        guard.use_installed_infra()
        self.system.ctl("mask", "fcgiwrap.socket")
        # Recovery after a killed installer preserves the same infra decision.
        self.guard().recover()
        self.assertEqual(self.system.states["fcgiwrap.socket"]["UnitFileState"], "masked")
        self.assertEqual(self.system.states["fcgiwrap.socket"]["ActiveState"], "inactive")
        self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "active")
        self.assertEqual(self.system.states["sa02m-flasher.service"]["ActiveState"], "active")
        self.assertEqual(self.system.states["sa02m-flasher.service"]["UnitFileState"], "disabled")

    def test_restore_failure_retains_recovery_journal(self):
        guard = self.guard()
        guard.enter()
        guard.stop(guard.units)
        def fail_start(verb, *args):
            if verb == "start":
                raise RuntimeError("start failed")
            return self.system.ctl(verb, *args)
        with patch.object(m, "ctl", fail_start), self.assertRaisesRegex(RuntimeError, "start failed"):
            guard.close()
        self.assertTrue(guard.journal.exists())
        self.guard().recover()
        self.assertFalse(guard.journal.exists())

    def test_installer_may_turn_a_generated_unit_into_a_real_one(self):
        # static/generated/indirect/alias are not operator decisions (nothing to
        # enable or disable); an installer that lands a real unit file over one
        # makes it enabled/disabled. The guard restores activity and accepts the
        # new autostart state - the old code raised, kept the journal, and
        # --recover raised the same forever (review A9).
        self.system.states["sa02m-test-stand.service"] = {
            "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "generated"}
        guard = self.guard()
        guard.enter()
        guard.stop(guard.units)
        self.system.states["sa02m-test-stand.service"]["UnitFileState"] = "enabled"
        guard.close()
        self.assertEqual(self.system.states["sa02m-test-stand.service"]["ActiveState"], "active")
        self.assertEqual(self.system.states["sa02m-test-stand.service"]["UnitFileState"], "enabled")
        self.assertFalse(guard.journal.exists())

    def test_transitional_state_refuses_before_guard_files(self):
        self.system.states["nginx.service"]["ActiveState"] = "activating"
        with self.assertRaisesRegex(RuntimeError, "unstable"):
            self.guard().enter()
        self.assertFalse(list(self.root.glob("*.d/*.conf")))

    def test_recovery_rejects_unexpected_units(self):
        guard = self.guard()
        guard.enter()
        data = json.loads(guard.journal.read_text())
        data["saved"]["unexpected.service"] = {}
        guard.journal.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "invalid guard recovery units"):
            self.guard().recover()

    def test_maintenance_flock_refuses_second_holder(self):
        with contextlib.ExitStack() as first, contextlib.ExitStack() as second:
            m.locked_file(self.root / "maintenance.lock", first)
            with self.assertRaises(BlockingIOError):
                m.locked_file(self.root / "maintenance.lock", second)

    def test_flasher_idle_reads_unauthenticated_health_and_fails_closed(self):
        # The daemon gates every route but /health behind the panel session, which
        # root has none of (docs/contracts/flasher-health.md); a probe of /jobs
        # answered 401 and refused the refresh on every board with an active
        # flasher (review B1, bench 136). poll_locked is the daemon's own union of
        # the on-disk flock and its active jobs - the whole "line busy" fact.
        import http.server
        import socketserver
        import threading
        replies = {"body": b'{"ok":true,"version":"t","poll_locked":false}', "status": 200}
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path != "/health":
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(replies["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(replies["body"])))
                self.end_headers()
                self.wfile.write(replies["body"])
            def log_message(self, *_args):
                pass
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        server = Server(str(self.root / "flasher.sock"), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.object(m, "FLASHER_SOCKET", str(self.root / "flasher.sock")):
            m.flasher_idle()  # idle line: the refresh proceeds
            self.assertEqual(requests, ["/health"], "only the unauthenticated route may be probed")
            for body, status in ((b'{"ok":true,"version":"t","poll_locked":true}', 200),
                                 (b'{"ok":true,"version":"t"}', 200),  # older daemon: no field
                                 (b'not json', 200), (b'{"ok":false}', 500)):
                replies.update(body=body, status=status)
                with self.subTest(body=body, status=status), self.assertRaises(RuntimeError):
                    m.flasher_idle()
            self.assertTrue(all(path == "/health" for path in requests), requests)
            # No daemon => no lease => no probe (the contract's "absence is not unknown").
            self.system.states["sa02m-flasher.service"]["ActiveState"] = "inactive"
            probes = len(requests)
            m.flasher_idle()
            self.assertEqual(len(requests), probes)

    def test_web_health_checks_custom_port_and_cgi_without_auth(self):
        self.system.states["fcgiwrap.service"] = {
            "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
        class Reply:
            status = 200
            def __init__(self, body):
                self.body = body
            def read(self, limit):
                return self.body
        with patch.object(m.subprocess, "run") as nginx, patch.object(
            m.subprocess, "check_output", return_value="8080\n"
        ), patch.object(m.http.client, "HTTPConnection") as http:
            connection = http.return_value
            connection.getresponse.side_effect = [Reply(b"<html>login</html>"), Reply(b'{"error":"unauthorized"}')]
            m.web_health()
            http.assert_called_once_with("127.0.0.1", 8080, timeout=10)
            self.assertEqual(connection.request.call_args_list[-1].args, ("GET", "/cgi-bin/status.cgi?part=priority"))
            nginx.assert_called_once()
            connection.close.assert_called_once()
            self.system.states["fcgiwrap.socket"] = {
                "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
            with self.assertRaisesRegex(RuntimeError, "obsolete socket"):
                m.web_health()

    def test_web_health_rejects_broken_cgi_response(self):
        self.system.states["fcgiwrap.service"] = {
            "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
        with patch.object(m.subprocess, "run"), patch.object(
            m.subprocess, "check_output", return_value="9999\n"
        ), patch.object(m.http.client, "HTTPConnection") as http:
            response = http.return_value.getresponse.return_value
            response.status = 200
            response.read.side_effect = [b"<html>login</html>", b'{"error":"backend_failed"}']
            with self.assertRaisesRegex(RuntimeError, "CGI guard unavailable"):
                m.web_health()

    def test_partial_entry_refuses_missing_coordinator_before_writes(self):
        with patch.object(m.os, "geteuid", return_value=0), patch.object(
            Path, "is_dir", return_value=False
        ), patch.object(m.subprocess, "Popen") as child, patch.object(m.Guard, "enter") as enter:
            with self.assertRaisesRegex(RuntimeError, "E_BOOTSTRAP_REQUIRED"):
                m.run_installer(["--require-coordinator", "update-www-only.sh"])
            child.assert_not_called()
            enter.assert_not_called()

    def test_full_wrapper_orders_quiescence_and_restores_after_failed_installer(self):
        real_guard = m.Guard
        real_popen = subprocess.Popen
        (self.root / "var/lib").mkdir(parents=True)
        states = json.loads(json.dumps(self.system.states))
        calls = []
        def path(value):
            value = str(value)
            if value.startswith(("/run/", "/var/", "/opt/")):
                return self.root / value.lstrip("/")
            return Path(value)
        def guarded(units, **kwargs):
            return real_guard(units, run=self.root, **kwargs)
        def run(args, **kwargs):
            calls.append("migrate" if "sa02m-update-state-migrate.sh" in str(args) else "idle")
            self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "inactive")
            self.assertEqual(self.system.states["sa02m-flasher.service"]["ActiveState"], "active")
        class Child:
            pid = 999999
            def wait(self, **kwargs):
                return 42
        def launch(args, **kwargs):
            calls.append("installer")
            self.assertEqual(self.system.states["sa02m-flasher.service"]["ActiveState"], "inactive")
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            for fd in kwargs["pass_fds"]:
                os.fstat(fd)
            return Child()
        with patch.object(m, "Path", side_effect=path), patch.object(m, "Guard", side_effect=guarded), \
             patch.object(m.os, "geteuid", return_value=0), patch.object(m, "legacy_idle"), \
             patch.object(m, "flasher_idle", side_effect=lambda: calls.append("jobs")), \
             patch.object(m, "uart_owners", side_effect=lambda allowed: calls.append("owners:" + str(allowed))), \
             patch.object(m.subprocess, "run", side_effect=run), \
             patch.object(m.subprocess, "Popen", side_effect=launch):
            self.assertEqual(m.run_installer(["install.sh", "--refresh"]), 42)
            first_calls = list(calls)
            calls.clear()
            stale = self.root / "var/lib/sa02m-port-coordinator/state.json"
            stale.parent.mkdir()
            stale.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "journal exists without installed broker"):
                m.run_installer(["install.sh", "--refresh"])
            self.assertNotIn("installer", calls)
            stale.unlink()
            children = []
            def interrupted_child(args, **kwargs):
                child = real_popen(["bash", "-c", "sleep 60"], start_new_session=True,
                                   pass_fds=kwargs["pass_fds"])
                children.append(child)
                wait = child.wait
                first = [True]
                def interrupted_wait(**options):
                    if first[0]:
                        first[0] = False
                        raise KeyboardInterrupt("test interruption")
                    return wait(**options)
                child.wait = interrupted_wait
                return child
            with patch.object(m.subprocess, "Popen", side_effect=interrupted_child):
                with self.assertRaises(KeyboardInterrupt):
                    m.run_installer(["install.sh", "--refresh"])
            self.assertIsNotNone(children[0].poll(), "interrupted installer child survived cleanup")
        self.assertEqual(first_calls, ["jobs", "owners:True", "migrate", "owners:False", "installer"])
        for unit, state in states.items():
            self.assertEqual(self.system.states[unit]["ActiveState"], state["ActiveState"])
            self.assertEqual(self.system.states[unit]["UnitFileState"], state["UnitFileState"])

    def test_shell_entrypoints_exec_guard_before_any_module(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "python3"
        output = self.root / "args"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_OUTPUT"\nexit 73\n')
        fake.chmod(0o755)
        env = {**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"], "TEST_OUTPUT": str(output)}
        env.pop("SA02M_INSTALL_GUARDED", None)
        for script in ("install.sh", "scripts/03-webserver.sh", "scripts/03a-port-coordinator.sh", "scripts/update-www-only.sh"):
            result = subprocess.run(["bash", str(ROOT / script), "--refresh"], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 73, result.stderr)
            args = output.read_text().splitlines()
            self.assertEqual(args[0], str(ROOT / "scripts/install-maintenance.py"))
            self.assertEqual(args[-2:], [str(ROOT / script), "--refresh"])
            self.assertEqual("--require-coordinator" in args, script != "install.sh")

    def test_coordinator_seed_preserved_and_dependency_before_start(self):
        stage = self.root / "repo"
        (stage / "scripts").mkdir(parents=True)
        (stage / "etc/sa02m").mkdir(parents=True)
        (stage / "etc/systemd").mkdir(parents=True)
        (stage / "opt/sa02m-port-coordinator").mkdir(parents=True)
        (stage / "etc/sa02m/port-coordinator.json").write_text('{"seed":true}')
        (stage / "etc/systemd/sa02m-port-coordinator.service").write_text("[Service]\nUser=root\n")
        (stage / "opt/sa02m-port-coordinator/package.py").write_text("value=1\n")
        target = self.root / "target"
        (target / "etc/systemd/system").mkdir(parents=True)
        commands = self.root / "commands"
        (stage / "scripts/lib.sh").write_text(
            'check_root() { :; }\n'
            'getent() { return 1; }\nid() { return 1; }\n'
            'groupadd() { echo group >> "$COMMANDS"; }\n'
            'useradd() { echo user >> "$COMMANDS"; }\n'
            'chown() { :; }\nsystemctl() { :; }\n'
            'sa02m_atomic_install() { install "$@"; }\n'
            'sa02m_svc_apply() {\n'
            '  test -f "$TARGET/opt/sa02m-port-coordinator/package.py" || return 1\n'
            '  test -f "$TARGET/etc/sa02m/port-coordinator.json" || return 1\n'
            '  test -f "$TARGET/etc/systemd/system/sa02m-port-coordinator.service" || return 1\n'
            '  echo broker >> "$COMMANDS"\n}\n')
        source = (ROOT / "scripts/03a-port-coordinator.sh").read_text()
        # Relocate only absolute destinations; repository sources keep BASE_DIR.
        source = re.sub(r"(?<![}$\w])/(etc|opt)/", lambda match: str(target) + match.group(0), source)
        module = stage / "scripts/03a-port-coordinator.sh"
        module.write_text(source)
        env = {**os.environ, "SA02M_ROOTFS_BUILD": "1", "SA02M_INSTALL_GUARDED": "1",
               "TARGET": str(target), "COMMANDS": str(commands)}
        for expected in ('{"seed":true}', '{"operator":"preserved"}'):
            if expected != '{"seed":true}':
                (target / "etc/sa02m/port-coordinator.json").write_text(expected)
            result = subprocess.run(["bash", str(module)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target / "etc/sa02m/port-coordinator.json").read_text(), expected)
            self.assertEqual(commands.read_text().splitlines()[-3:], ["group", "user", "broker"])


if __name__ == "__main__":
    unittest.main()
