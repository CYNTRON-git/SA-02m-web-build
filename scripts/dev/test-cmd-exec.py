#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes real CGI/auth and response code; no source-line pins.
"""Real console CGI/auth and its bounded binary response reader, without root commands."""
from __future__ import annotations

import contextlib
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("CMD_EXEC_SRC") or ROOT / "www/network_config/cgi-bin/cmd_exec.cgi")
AUTH = ROOT / "www/network_config/cgi-bin/lib_web_auth.sh"
LIMIT = 32768


def stop_fixture_tree(pid):
    """A broken timeout test must not leave its own descendants running."""
    processes = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().split(')', 1)[1].split()
            processes[int(path.parent.name)] = (int(fields[1]), fields[19])
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    owned = {pid}
    while True:
        more = {child for child, (parent, _) in processes.items() if parent in owned}
        if more <= owned: break
        owned |= more
    for child in sorted(owned, reverse=True):
        try:
            current = Path('/proc/%d/stat' % child).read_text().split(')', 1)[1].split()[19]
            if child in processes and current == processes[child][1]:
                os.kill(child, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass


class TailReaderTest(unittest.TestCase):
    def render(self, data, *, rc=0):
        source = SOURCE.read_text(encoding="utf-8")
        match = re.search(r"^python3 - \"\$TMP_OUT\" .*? <<'PY'\n(.*?)^PY$", source, re.M | re.S)
        self.assertIsNotNone(match, "shipped response block missing")
        reads = []

        class BoundedFile(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                if not 0 <= size <= LIMIT:
                    raise AssertionError("response requested an unbounded read: %s" % size)
                return super().read(size)

        stream, output = BoundedFile(data), io.StringIO()
        def open_file(path, *args, **kwargs):
            return io.StringIO(json.dumps({"observed_bytes": len(data), "capture_timed_out": False})) if path == "meta" else stream
        with mock.patch("builtins.open", side_effect=open_file), \
                mock.patch.object(sys, "argv", ["reader", "output", str(rc), "web", "meta", "0"]), \
                contextlib.redirect_stdout(output):
            exec(compile(match[1], str(SOURCE), "exec"), {})
        self.assertTrue(reads)
        return json.loads(output.getvalue())

    def test_boundaries_and_binary_tail(self):
        for data in (b"", b"x" * LIMIT, b"prefix" + b"x" * LIMIT,
                     b"\xe2\x82\xac" + b"x" * (LIMIT - 1), b'bad\xff"\n'):
            with self.subTest(length=len(data)):
                result = self.render(data, rc=7)
                self.assertEqual(result, {"ok": True, "rc": 7, "mode": "web",
                                         "output": data[-LIMIT:].decode("utf-8", "replace"),
                                         "truncated": len(data) > LIMIT, "capture_timed_out": False})

    def test_sparse_large_source_does_not_read_prefix(self):
        # The virtual file represents 10 GiB; only its suffix exists in memory.
        source = SOURCE.read_text(encoding="utf-8")
        match = re.search(r"^python3 - \"\$TMP_OUT\" .*? <<'PY'\n(.*?)^PY$", source, re.M | re.S)
        self.assertIsNotNone(match)
        size, accesses = 10 * 1024**3, []

        class SparseFile:
            position = 0
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def seek(self, offset, whence=0):
                self.position = (size if whence == 2 else self.position if whence == 1 else 0) + offset
                accesses.append(("seek", self.position))
                return self.position
            def tell(self): return self.position
            def read(self, amount=-1):
                accesses.append(("read", amount, self.position))
                if amount != LIMIT or self.position != size - LIMIT:
                    raise AssertionError("read must address only the bounded suffix")
                return b"z" * LIMIT

        output = io.StringIO()
        def open_file(path, *args, **kwargs):
            return io.StringIO(json.dumps({"observed_bytes": size, "capture_timed_out": False})) if path == "meta" else SparseFile()
        with mock.patch("builtins.open", side_effect=open_file), \
                mock.patch.object(sys, "argv", ["reader", "output", "124", "web", "meta", "0"]), \
                contextlib.redirect_stdout(output):
            exec(compile(match[1], str(SOURCE), "exec"), {})
        self.assertEqual(json.loads(output.getvalue()), {
            "ok": True, "rc": 124, "mode": "web", "output": "z" * LIMIT, "truncated": True, "capture_timed_out": False})
        self.assertEqual([a for a in accesses if a[0] == "read"], [("read", LIMIT, size - LIMIT)])


@unittest.skipUnless(os.name == "posix", "CGI execution requires Bash/POSIX")
class ConsoleCGITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cmd-exec-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.token, self.csrf = "a" * 64, "b" * 64
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        (self.sessions / digest).write_text("%d tester\n" % (time.time() + 3600))
        (self.sessions / (digest + ".csrf")).write_text(self.csrf + "\n")
        self.cgi = self.root / "cmd_exec.cgi"
        # Retarget only temp artifacts; all control flow/auth remain shipped code.
        self.cgi.write_text(SOURCE.read_text(encoding="utf-8").replace(
            "/tmp/sa02m-cmd-", str(self.root / "sa02m-cmd-")), encoding="utf-8")
        shutil.copyfile(AUTH, self.root / AUTH.name)
        collector = ROOT / "www/network_config/cgi-bin/lib_cmd_capture.py"
        if collector.exists():
            shutil.copyfile(collector, self.root / collector.name)

    def test_spool_is_bounded_during_large_output(self):
        body = urlencode({"cmd": "python3 -c 'import sys,time;sys.stdout.write(\"z\"*8388608);sys.stdout.flush();time.sleep(1)'", "mode": "web"}).encode()
        env = dict(os.environ, SA02M_SESSION_DIR=str(self.sessions), REQUEST_METHOD="POST",
                   CONTENT_LENGTH=str(len(body)), HTTP_COOKIE="session_token=" + self.token,
                   HTTP_X_SA02M_CSRF=self.csrf)
        process = subprocess.Popen(["bash", str(self.cgi)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
        maximum = 0
        try:
            process.stdin.write(body)
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                for path in self.root.glob("sa02m-cmd-out.*"):
                    try: maximum = max(maximum, path.stat().st_size)
                    except FileNotFoundError: pass
                time.sleep(.005)
            out, err = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
            result = json.loads(out.decode().split("\n\n", 1)[1])
            self.assertEqual(result["output"], "z" * LIMIT)
            self.assertTrue(result["truncated"])
            self.assertLessEqual(maximum, LIMIT + 64, "temporary output grew beyond bounded tail")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

    def request(self, command, *, cookie=True, csrf=True, method="POST", **fields):
        body = urlencode({"cmd": command, "mode": "web", **fields}).encode()
        env = dict(os.environ, SA02M_SESSION_DIR=str(self.sessions), REQUEST_METHOD=method,
                   CONTENT_LENGTH=str(len(body)), HTTP_COOKIE="session_token=" + (self.token if cookie else "c" * 64),
                   HTTP_X_SA02M_CSRF=self.csrf if csrf else "wrong")
        process = subprocess.Popen(["bash", str(self.cgi)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
        try:
            stdout, stderr = process.communicate(body, timeout=25)
        except BaseException:
            stop_fixture_tree(process.pid)
            process.communicate(timeout=3)
            raise
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        headers, raw = stdout.decode("utf-8").split("\n\n", 1)
        self.assertIn("Content-type: application/json", headers)
        self.assertIn("Cache-Control: no-store", headers)
        self.assertEqual(list(self.root.glob("sa02m-cmd-*")), [], "CGI leaked temp artifacts")
        return json.loads(raw)

    def test_large_output_exact_suffix_and_next_command(self):
        data = b"a" * (256 * 1024) + b"FINAL-MARKER\n"
        result = self.request("python3 -c 'import sys; sys.stdout.write(\"a\" * 262144 + \"FINAL-MARKER\\n\")'")
        self.assertEqual(result, {"ok": True, "rc": 0, "mode": "web",
                                 "output": data[-LIMIT:].decode(), "truncated": True, "capture_timed_out": False})
        self.assertEqual(self.request("printf next; exit 7"), {
            "ok": True, "rc": 7, "mode": "web", "output": "next", "truncated": False, "capture_timed_out": False})

    def test_auth_method_and_csrf_prevent_execution(self):
        marker = self.root / "executed"
        for options, error in (({"cookie": False}, "unauthorized"),
                               ({"method": "GET"}, "method_not_allowed"),
                               ({"csrf": False}, "E_CSRF")):
            with self.subTest(error=error):
                result = self.request("touch '%s'" % marker, **options)
                self.assertFalse(result["ok"])
                self.assertEqual(result.get("error_code") or result.get("error"), error)
                self.assertFalse(marker.exists())

    def test_missing_or_failed_collector_and_invalid_metadata(self):
        collector = self.root / "lib_cmd_capture.py"
        original = collector.read_text()
        marker = self.root / "executed"
        collector.unlink()
        self.assertEqual(self.request("touch '%s'" % marker), {"ok": False, "error": "capture_failed"})
        self.assertFalse(marker.exists())
        for source in ("import sys;sys.exit(1)",
                       "import sys;open(sys.argv[2],'w').write('{}')",
                       "import sys;open(sys.argv[2],'w').write('invalid')"):
            with self.subTest(source=source):
                collector.write_text(source)
                self.assertEqual(self.request("printf test"), {"ok": False, "error": "capture_failed"})
        collector.write_text(original)
        self.assertEqual(self.request("printf recovered")["output"], "recovered")

    def test_inherited_pipe_keeps_real_zero_exit(self):
        from shlex import quote

        release = self.root / "release-child"
        os.mkfifo(release)
        # The child holds stdout until the test releases it, even on a slow host.
        command = (f'exec 3< {quote(str(release))}; '
                   'read -r -t 60 <&3 & sleep .5; printf parent-done')
        with release.open("r+b", buffering=0) as gate:
            start = time.monotonic()
            try:
                result = self.request(command)
            finally:
                gate.write(b"\n")
        self.assertGreaterEqual(time.monotonic() - start, 18)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["output"], "parent-done")
        self.assertFalse(result["truncated"])
        self.assertTrue(result["capture_timed_out"])

    def test_term_resistant_command_is_killed_before_nginx_deadline(self):
        # Use the environment's GNU timeout, matching the Noble target, not WSL uutils.
        timeout = shutil.which("timeout")
        version = subprocess.check_output([timeout, "--version"], text=True)
        self.assertIn("GNU coreutils", version, "select the target-compatible GNU timeout in PATH")
        text = self.cgi.read_text()
        text = text.replace('timeout -k 2s "$TIMEOUT_SEC"', '"%s" -k 2s "$TIMEOUT_SEC"' % timeout)
        self.cgi.write_text(text)
        pidfile = self.root / "child.pid"
        command = "trap '' TERM; sleep 60 & echo $! > '%s'; wait" % pidfile
        start = time.monotonic()
        result = self.request(command)
        elapsed = time.monotonic() - start
        self.assertEqual(result["rc"], 137)
        self.assertIn("[command timeout]", result["output"])
        self.assertLess(elapsed, 20)
        self.assertGreaterEqual(elapsed, 16.5)
        child = Path('/proc') / pidfile.read_text().strip() / 'stat'
        if child.exists():
            self.assertEqual(child.read_text().split()[2], 'Z', 'fixture child is still live')

    def test_root_outer_wrapper_contains_a_stale_helper(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        sudo = bindir / 'sudo'
        sudo.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
        sudo.chmod(0o755)
        helper = self.root / 'stale-helper'
        helper.write_text("#!/bin/bash\ntrap '' TERM\nsleep 10 & wait\n")
        helper.chmod(0o755)
        source = self.cgi.read_text()
        self.assertIn('timeout -k 1s 18s sudo', source)
        source = source.replace('timeout -k 1s 18s sudo', 'timeout -k .1s .2s sudo')
        source = source.replace('HELPER=/usr/local/sbin/sa02m-web-root-cmd.sh', 'HELPER=' + str(helper))
        source = source.replace('PATH=/usr/local/sbin:', 'PATH=' + str(bindir) + ':' + str(Path(shutil.which('timeout')).parent) + ':/usr/local/sbin:')
        self.cgi.write_text(source)
        result = self.request('printf unused', mode='root', root_password='synthetic')
        self.assertEqual(result['rc'], 137)
        self.assertIn('[command timeout]', result['output'])
        self.assertNotIn('after 15', result['output'])


class CaptureLoopTest(unittest.TestCase):
    def setUp(self):
        path = ROOT / "www/network_config/cgi-bin/lib_cmd_capture.py"
        spec = importlib.util.spec_from_file_location("cmd_capture_test", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def run_capture(self, chunks, ticks):
        selector = mock.MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [(object(), 1)]
        with mock.patch.object(self.module.selectors, "DefaultSelector", return_value=selector), \
             mock.patch.object(self.module.os, "set_blocking"), \
             mock.patch.object(self.module.os, "close") as close, \
             mock.patch.object(self.module.os, "read", side_effect=chunks) as read, \
             mock.patch.object(self.module.time, "monotonic", side_effect=ticks):
            result = self.module.capture(123)
        close.assert_called_once_with(123)
        self.assertTrue(all(c.args == (123, 65536) for c in read.call_args_list))
        return result

    def test_bounded_chunks_tail_and_observed_count(self):
        tail, metadata = self.run_capture([b'a' * 65536, b'b' * 40000, b''], [0, 0, 1, 2])
        self.assertEqual(tail, b'b' * LIMIT)
        self.assertEqual(metadata, {'observed_bytes': 105536, 'capture_timed_out': False})

    def test_deadline_checked_under_continuously_ready_input(self):
        tail, metadata = self.run_capture([b'a' * 65536, b'b' * 65536], [0, 0, 17, 18])
        self.assertEqual(tail, b'b' * LIMIT)
        self.assertTrue(metadata['capture_timed_out'])
        self.assertEqual(metadata['observed_bytes'], 131072)

    def test_read_failure_closes_fd_and_does_not_succeed(self):
        selector = mock.MagicMock()
        selector.__enter__.return_value = selector
        with mock.patch.object(self.module.selectors, 'DefaultSelector', return_value=selector), \
             mock.patch.object(self.module.os, 'set_blocking'), \
             mock.patch.object(self.module.os, 'close') as close, \
             mock.patch.object(self.module.os, 'read', side_effect=OSError('read failed')):
            with self.assertRaises(OSError): self.module.capture(123)
        close.assert_called_once_with(123)

    def test_main_write_failure_is_nonzero(self):
        with mock.patch.object(self.module, 'capture', return_value=(b'x', {'observed_bytes': 1, 'capture_timed_out': False})), \
             mock.patch('builtins.open', side_effect=OSError('write failed')), \
             mock.patch.object(sys, 'argv', ['capture', 'out', 'meta']), \
             mock.patch.object(sys, 'stdin') as stdin, contextlib.redirect_stderr(io.StringIO()):
            stdin.fileno.return_value = 123
            self.assertEqual(self.module.main(), 1)


@unittest.skipUnless(os.name == "posix", "root verifier requires the target crypt/spwd modules")
class RootVerifierTest(unittest.TestCase):
    def run_verifier(self, shadow, password='correct', command='exit 7', raises=False):
        source = (ROOT / 'etc/sa02m-web-root-cmd.sh').read_text()
        block = re.search(r"<<'PY'\n(.*?)^PY$", source, re.M | re.S).group(1)
        with mock.patch('builtins.open', side_effect=[io.StringIO(password + '\n'), io.StringIO(command + '\n')]), \
             mock.patch.object(sys, 'argv', ['verify', 'pass', 'cmd']), \
             mock.patch('spwd.getspnam', side_effect=KeyError('missing') if raises else None) as get_shadow, \
             mock.patch('crypt.crypt', return_value='hash') as crypt, \
             mock.patch('os.execv') as execute, contextlib.redirect_stderr(io.StringIO()) as err:
            get_shadow.return_value.sp_pwdp = shadow
            rc = 0
            try: exec(compile(block, 'root-helper', 'exec'), {})
            except SystemExit as exc: rc = exc.code
        return rc, execute, crypt, err.getvalue()

    def test_good_password_execs_fixed_shell_and_preserves_command(self):
        rc, execute, crypt, err = self.run_verifier('hash', command='printf "a\\nb"\n')
        self.assertEqual(rc, 0)
        execute.assert_called_once_with('/bin/bash', ['/bin/bash', '-c', 'printf "a\\nb"'])
        crypt.assert_called_once_with('correct', 'hash')

    def test_bad_locked_missing_password_never_executes(self):
        for shadow, raises in [('wrong', False), ('!locked', False), ('*', False), ('', False), ('', True)]:
            with self.subTest(shadow=shadow, raises=raises):
                rc, execute, crypt, err = self.run_verifier(shadow, raises=raises)
                self.assertEqual(rc, 77)
                execute.assert_not_called()
                self.assertEqual(err, 'root authentication failed\n')

    def test_helper_rejects_invalid_paths_before_authentication(self):
        helper = ROOT / 'etc/sa02m-web-root-cmd.sh'
        result = subprocess.run(['sh', str(helper), '/wrong', '/wrong'], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 64)
        self.assertIn(b'invalid password file', result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
