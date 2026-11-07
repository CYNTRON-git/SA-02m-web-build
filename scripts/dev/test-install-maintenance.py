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
import time
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
# The journal directory must belong to m.JOURNAL_OWNER_UID (root on the board).
# Rebind it to this process so the wrapper cases run unprivileged with no skip
# (CI is a non-root runner); JournalDirectory pins the root default and the
# refusal of a foreign owner.
m.JOURNAL_OWNER_UID = os.geteuid()


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
        self.assertEqual(first_calls, ["jobs", "owners:True", "migrate", "jobs", "owners:False", "installer"])
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
        owners = self.root / "owners"
        # install -o/-g needs root; the stub records the requested owner and
        # installs as the running user, so the case runs unprivileged and the
        # root:root request is still asserted below.
        (stage / "scripts/lib.sh").write_text(
            'check_root() { :; }\n'
            'getent() { return 1; }\nid() { return 1; }\n'
            'groupadd() { echo group >> "$COMMANDS"; }\n'
            'useradd() { echo user >> "$COMMANDS"; }\n'
            'install() {\n'
            '  local owner=- group=-; local -a args=()\n'
            '  while [ $# -gt 0 ]; do\n'
            '    case "$1" in -o) owner=$2; shift 2 ;; -g) group=$2; shift 2 ;; *) args+=("$1"); shift ;; esac\n'
            '  done\n'
            '  echo "$owner:$group ${args[*]: -1}" >> "$OWNERS"\n'
            '  command install "${args[@]}"\n}\n'
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
               "TARGET": str(target), "COMMANDS": str(commands), "OWNERS": str(owners)}
        seed = "root:root %s/etc/sa02m/port-coordinator.json" % target
        for expected in ('{"seed":true}', '{"operator":"preserved"}'):
            if expected != '{"seed":true}':
                (target / "etc/sa02m/port-coordinator.json").write_text(expected)
            owners.unlink(missing_ok=True)
            result = subprocess.run(["bash", str(module)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target / "etc/sa02m/port-coordinator.json").read_text(), expected)
            self.assertEqual(commands.read_text().splitlines()[-3:], ["group", "user", "broker"])
            # Every install of the broker's files asks for root:root; the seed
            # is installed only while absent.
            self.assertEqual(owners.read_text().splitlines(), [
                "root:root %s/etc/sa02m" % target,
                *([seed] if expected == '{"seed":true}' else []),
                "root:root %s/etc/systemd/system/sa02m-port-coordinator.service" % target,
            ])


class ManagedGateway(unittest.TestCase):
    """The installed broker handles real peer credentials, PTYs, SCM_RIGHTS and flocks."""

    def setUp(self):
        import multiprocessing
        import pty
        sys.path.insert(0, str(ROOT / "opt/sa02m-port-coordinator"))
        from sa02m_ports import Lease, PortError, broker, locks
        self.Lease, self.PortError = Lease, PortError
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.unit = "sa02m-serial-gateway.service"
        self.group = "/system.slice/" + self.unit
        self.system = System(self.root)
        self.system.states[self.unit].update(LoadState="loaded", ActiveState="active", UnitFileState="disabled")
        self.runtime = self.root / "run/sa02m-port-coordinator"
        self.private = self.root / "var/lib/sa02m-port-coordinator"
        self.sock = str(self.runtime / "control.sock")
        self.dev = self.root / "dev"
        self.dev.mkdir()
        self.ports = ["/dev/COM1", "/dev/COM4"]
        for port in self.ports:
            master, slave = pty.openpty()
            self.stack.callback(os.close, master)
            (self.dev / Path(port).name).symlink_to(os.ttyname(slave))
            os.close(slave)
        self.cgroup = self.root / "cgroup"
        self.cgroup.write_text("0::" + self.group + "\n")
        self.install = self.root / "opt/sa02m-port-coordinator"
        self.install.parent.mkdir()
        self.install.symlink_to(ROOT / "opt/sa02m-port-coordinator")
        self.stack.enter_context(patch.object(locks, "OWNER_UID", os.getuid()))
        self.stack.enter_context(patch.object(locks, "OWNER_GID", os.getgid()))
        self.stack.enter_context(patch.object(broker, "OCCUPANT_SCAN_ROOT", os.getpid()))
        class Units:
            def mqtt_pause(self, port):
                return None
            def canonical(self, unit):
                return unit
        def server():
            # Relocate the kernel devices while retaining real COM names on the wire.
            for name in ("device_key", "occupants", "lock_device"):
                original = getattr(broker, name)
                setattr(broker, name, lambda port, *a, fn=original: fn(self.dev / Path(port).name, *a))
            instance = broker.Broker({"ports": {p: [] for p in self.ports}}, self.private, self.runtime, Units())
            broker.serve(instance, allowed_uids={os.getuid()}, admin_uids={os.getuid()}, group=os.getgid())
        daemon = multiprocessing.get_context("fork").Process(target=server)
        self.daemon = daemon
        daemon.start()
        self.stack.callback(lambda: (daemon.kill(), daemon.join(5)))
        until = time.monotonic() + 5
        while not Path(self.sock).exists() and daemon.is_alive() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue(Path(self.sock).exists(), "broker did not listen")
        self.handles = {}
        self.stack.callback(self.release)
        self.gateway_refused = []
        self.started = 0
        self.refuse_ports = False
        self.keep_port_on_stop = False
        self.child_code = 0
        def path(name):
            name = str(name)
            if name == "/":
                return self.root
            if name == "/proc/%d/cgroup" % os.getpid():
                return self.cgroup
            if name.split("/")[1:2] in (["dev"], ["run"], ["opt"], ["var"]):
                return self.root / name[1:]
            return Path(name)
        real_guard, real_run = m.Guard, subprocess.run
        def run(args, **kwargs):
            if args[0] == "bash":
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == [sys.executable, "-I", "-c"] and len(args) == 4:
                # The .32 one-line probe hardcodes its installed package and socket.
                args = [*args[:3], args[3].replace("'/opt/sa02m-port-coordinator'", repr(str(self.install)))
                        .replace("from sa02m_ports import assert_idle", "from sa02m_ports import client; "
                                 + "client.SOCKET=" + repr(self.sock) + "; from sa02m_ports import assert_idle")]
            return real_run(args, **kwargs)
        def launch(args, **kwargs):
            self.started += 1
            self.assertFalse(self.handles)
            journal = self.root / "var/lib/sa02m-install-maintenance/uart.json"
            self.assertEqual(set(json.loads(journal.read_text())["gateway_ports"]), set(self.ports))
            for port in self.ports:
                self.assertEqual(broker.occupants(self.dev / Path(port).name), [])
                with self.assertRaisesRegex(PortError, "maintenance"):
                    with Lease(port, "scan", self.sock):
                        pass
            class Child:
                def wait(_self):
                    return self.child_code
            return Child()
        # Keep subprocess.run's own Popen intact for the isolated installed-client RPC.
        import types
        process = types.SimpleNamespace(**vars(subprocess))
        process.Popen, process.run = launch, run
        for name, value in (("Path", path), ("subprocess", process), ("ctl", self.ctl),
                            ("Guard", lambda units, **kw: real_guard(units, run=self.root, **kw)),
                            ("legacy_idle", lambda: None), ("flasher_idle", lambda: None),
                            ("web_health", lambda: None)):
            self.stack.enter_context(patch.object(m, name, value))
        self.stack.enter_context(patch.object(m.os, "geteuid", return_value=0))
        self.acquire()

    def acquire(self):
        for port in self.ports:
            if port not in self.handles and not self.refuse_ports:
                lease = self.Lease(port, "gateway", self.sock).__enter__()
                self.handles[port] = lease, os.open(self.dev / Path(port).name, os.O_RDWR | os.O_NOCTTY)

    def release(self):
        while self.handles:
            _, (lease, fd) = self.handles.popitem()
            os.close(fd)
            lease.close()

    def ctl(self, verb, *args):
        if verb == "show" and args == (self.unit, "-p", "ControlGroup", "--value"):
            return self.group
        if verb == "show" and args[0] == self.unit and "MainPID" in args:
            return "ActiveState=active\nMainPID=%d\nControlGroup=%s" % (os.getpid(), self.group)
        result = self.system.ctl(verb, *args)
        if verb in {"start", "stop"} and args[-1] == self.unit:
            if verb == "stop":
                if not self.keep_port_on_stop:
                    self.release()
            else:
                try:
                    self.acquire()
                except self.PortError as exc:
                    self.gateway_refused.append(str(exc))
        return result

    def test_full_refresh_admits_only_managed_gateway_and_restores_working_line(self):
        for code in (0, 42):
            self.child_code = code
            self.assertEqual(m.run_installer(["install.sh", "--refresh"]), code)
            self.assertEqual(set(self.handles), set(self.ports))
            self.assertEqual(self.system.states[self.unit]["UnitFileState"], "disabled")
        self.assertEqual(self.started, 2)
        self.assertEqual(self.gateway_refused, [])

    def refusal(self, error=Exception):
        with self.assertRaises(error):
            m.run_installer(["install.sh", "--refresh"])
        self.assertEqual(self.started, 0)
        self.assertFalse(any(call == ("stop", (self.unit,)) for call in self.system.calls))
        self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "active")

    def test_live_other_lease_and_service_mutation_never_stop_the_gateway(self):
        from sa02m_ports import ServiceMutation
        port = self.ports[-1]
        lease, fd = self.handles.pop(port)
        os.close(fd)
        lease.close()
        for purpose in ("flasher", "operation", "scan"):
            with self.subTest(purpose=purpose), self.Lease(port, purpose, self.sock):
                self.refusal()
        with ServiceMutation("mplc4.service", self.sock):
            self.refusal()
        self.assertIn(self.ports[0], self.handles)

    def test_spoofed_or_recovering_ledger_cannot_interrupt_a_gateway(self):
        from sa02m_ports.broker import atomic_json
        ledger = self.private / "state.json"
        original = json.loads(ledger.read_bytes())
        self.stack.callback(atomic_json, ledger, original)
        for change in ({"pid": os.getppid()}, {"purpose": "scan"}, {"phase": "recovering"}, {"id": "x"},
                       {"key": "1-1"}, {"error": "unresolved"}, {"unknown": True}):
            state = json.loads(json.dumps(original))
            next(iter(state["leases"].values())).update(change)
            atomic_json(ledger, state)
            with self.subTest(change=change):
                self.refusal()
        for leases in ({}, original["leases"]):
            atomic_json(ledger, {"leases": leases, "services": {"mplc4.service": {
                "restore": True, "phase": "restore_failed", "error": "failed"}}})
            self.refusal()
        atomic_json(ledger, original)
        self.cgroup.write_text("0::/user.slice/" + self.unit + "\n")
        self.refusal()
        self.cgroup.write_text("0::" + self.group + "\n")
        # Same real process holds the physical UART but has lost its transferred
        # lock descriptor, as a recycled same-unit PID could after a broker crash.
        lease, _ = self.handles[self.ports[0]]
        os.close(lease.fd)
        lease.fd = None
        self.refusal()

    def test_foreign_uart_fd_and_unsafe_ledger_refuse_before_stop(self):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        ready_r, ready_w = ctx.Pipe(duplex=False)
        def foreign():
            fd = os.open(self.dev / Path(self.ports[0]).name, os.O_RDWR | os.O_NOCTTY)
            ready_w.send(True)
            time.sleep(30)
            os.close(fd)
        owner = ctx.Process(target=foreign)
        owner.start()
        try:
            self.assertTrue(ready_r.poll(5) and ready_r.recv())
            self.refusal()
        finally:
            owner.kill()
            owner.join(5)
            ready_r.close()
            ready_w.close()
        ledger = self.private / "state.json"
        ledger.chmod(0o644)
        self.refusal()
        ledger.chmod(0o600)
        moved = self.private / "renamed"
        ledger.rename(moved)
        ledger.symlink_to(moved)
        self.refusal()
        ledger.unlink()
        moved.rename(ledger)
        self.private.chmod(0o755)
        self.refusal()
        self.private.chmod(0o700)

    def test_failed_reopen_preserves_both_journals_and_explicit_recovery_repairs_it(self):
        self.refuse_ports = True
        self.system.states[self.unit]["UnitFileState"] = "enabled"
        with self.assertRaisesRegex(RuntimeError, "restore working UART"):
            m.run_installer(["install.sh", "--refresh"])
        self.assertEqual(self.started, 1)
        self.assertEqual(self.handles, {})
        journal = self.root / "var/lib/sa02m-install-maintenance"
        self.assertEqual(set(json.loads((journal / "uart.json").read_text())["gateway_ports"]), set(self.ports))
        self.assertTrue((journal / "web.json").is_file())
        self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "inactive")
        self.refuse_ports = False
        self.assertEqual(m.run_installer(["--recover"]), 0)
        self.assertEqual(set(self.handles), set(self.ports))
        self.assertEqual(self.system.states[self.unit]["UnitFileState"], "enabled")
        self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "active")
        self.assertEqual(list(journal.iterdir()), [])

    def test_refusal_after_stop_restores_the_original_gateway_and_keeps_installer_out(self):
        self.keep_port_on_stop = True
        with patch.object(m, "HANDOFF_SECONDS", .75), self.assertRaisesRegex(RuntimeError, "did not release"):
            m.run_installer(["install.sh", "--refresh"])
        self.assertEqual(self.started, 0)
        self.assertEqual(set(self.handles), set(self.ports))
        self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "active")

    def test_interrupted_child_releases_gate_before_restoration(self):
        def launch(args, **kwargs):
            child = subprocess.Popen(["sleep", "30"], start_new_session=True, pass_fds=kwargs["pass_fds"])
            wait = child.wait
            def interrupt(**kwargs):
                child.wait = wait
                raise KeyboardInterrupt
            child.wait = interrupt
            return child
        with patch.object(m.subprocess, "Popen", side_effect=launch), self.assertRaises(KeyboardInterrupt):
            m.run_installer(["install.sh", "--refresh"])
        self.assertEqual(set(self.handles), set(self.ports))
        self.assertEqual(self.gateway_refused, [])
        self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "active")

    def test_surviving_child_descriptor_blocks_recovery_and_reopen(self):
        inherited = []
        wrapped = m.subprocess.Popen
        def launch(args, **kwargs):
            inherited.extend(map(os.dup, kwargs["pass_fds"]))
            return wrapped(args, **kwargs)
        try:
            with patch.object(m.subprocess, "Popen", side_effect=launch), self.assertRaises(BlockingIOError):
                m.run_installer(["install.sh", "--refresh"])
            with self.assertRaises(BlockingIOError):
                m.run_installer(["--recover"])
            self.assertFalse(self.handles)
            self.assertEqual(self.system.states["nginx.service"]["ActiveState"], "inactive")
        finally:
            for fd in inherited:
                os.close(fd)
        self.assertEqual(m.run_installer(["--recover"]), 0)
        self.assertEqual(set(self.handles), set(self.ports))

    def test_gateway_is_left_running_if_broker_dies(self):
        self.daemon.kill()
        self.daemon.join(5)
        self.refusal()
        self.assertEqual(set(self.handles), set(self.ports))
        for lease, _ in self.handles.values():
            lease.sock.close()
            lease.sock = None

    def test_old_journal_recovery_releases_gate_before_gateway_starts(self):
        self.release()
        journal = self.root / "var/lib/sa02m-install-maintenance"
        journal.mkdir(mode=0o700)
        guard = m.Guard(m.UART + m.UPDATE, journal=journal / "uart.json")
        guard.enter()
        guard.stop(m.UART)
        data = json.loads(guard.journal.read_text())
        data.pop("gateway_ports", None)
        guard.journal.write_text(json.dumps(data))
        self.assertEqual(m.run_installer(["--recover"]), 0)
        self.assertEqual(self.gateway_refused, [])
        self.assertEqual(set(self.handles), set(self.ports))


class JournalDirectory(unittest.TestCase):
    """The recovery journal lives in a root-only directory on the board."""

    def test_owner_default_is_root(self):
        # A fresh interpreter: the module-level rebinding above must not mask a
        # product default that drifted away from root.
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import importlib.util as u, sys; s = u.spec_from_file_location('x', sys.argv[1]); "
             "mod = u.module_from_spec(s); s.loader.exec_module(mod); print(mod.JOURNAL_OWNER_UID)",
             str(ROOT / "scripts/install-maintenance.py")],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_unsafe_directory_refused_before_any_guard(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        journal = root / "var/lib/sa02m-install-maintenance"
        elsewhere = root / "elsewhere"
        journal.parent.mkdir(parents=True)

        def path(value):
            value = str(value)
            return root / value.lstrip("/") if value.startswith("/var/") else Path(value)

        def foreign_owner():
            journal.mkdir()
            journal.chmod(0o700)
            return {"JOURNAL_OWNER_UID": os.geteuid() + 1}

        def group_readable():
            journal.mkdir()
            journal.chmod(0o750)
            return {}

        def symlinked():
            # On Linux a symlink's own lstat mode is 0777, so the mode clause
            # refuses it too; this case pins the refusal, not the is_symlink() term.
            elsewhere.mkdir()
            elsewhere.chmod(0o700)
            journal.symlink_to(elsewhere, target_is_directory=True)
            return {}

        for case in (foreign_owner, group_readable, symlinked):
            with self.subTest(case=case.__name__):
                for leftover in (journal, elsewhere):
                    if leftover.is_symlink():
                        leftover.unlink()
                    elif leftover.exists():
                        leftover.rmdir()
                overrides = case()
                with contextlib.ExitStack() as stack:
                    for name, value in overrides.items():
                        stack.enter_context(patch.object(m, name, value))
                    stack.enter_context(patch.object(m, "Path", side_effect=path))
                    stack.enter_context(patch.object(m.os, "geteuid", return_value=0))
                    guard = stack.enter_context(patch.object(m, "Guard"))
                    child = stack.enter_context(patch.object(m.subprocess, "Popen"))
                    with self.assertRaisesRegex(RuntimeError, "unsafe install-maintenance journal directory"):
                        m.run_installer(["install.sh", "--refresh"])
                    guard.assert_not_called()
                    child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
