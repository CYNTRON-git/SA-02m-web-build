# -*- coding: utf-8 -*-
"""Regression net for tools/imaging/capture-image-win.py finalize.

The Windows capture tool must finalize an image exactly like make-image.sh:
verify the raw stream's sha256 on the bytes that get shrunk, PiShrink, then run
tools/imaging/patch-firstboot-image.sh UNCONDITIONALLY and FATALLY (boot.scr,
first-boot wiring and the image-identity wipe + check — площадка 3 of
docs/contracts/image-identity-reset.md), then xz. Before this net the tool ran
a best-effort inline mini-patch instead, so a Windows-made image could ship the
donor's unvalidated boot.scr and skip the identity check.

Stdlib unittest only; paramiko is stubbed when absent so CI needs no extra dep.
WSL is never invoked: subprocess.check_output (wslpath) and check_call are
mocked, and the generated WSL script is asserted on as text. The optional
execution case runs that script under a POSIX bash with PATH stubs and skips
(reported as a skip) where no usable bash exists.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

TOOL = Path(__file__).resolve().parents[1] / "capture-image-win.py"
TEST_IP = "10.20.30.40"  # deliberately not the 192.168.1.136 default
RAW_BYTES = b"SA-02m raw eMMC stand-in\n" * 64
DEFAULT_FLAGS = {"cleanup": True, "zerofill": True, "id_reset": True}


def _load_tool():
    try:
        import paramiko  # noqa: F401
    except ImportError:
        stub = types.ModuleType("paramiko")
        stub.SSHClient = object
        stub.AutoAddPolicy = object
        sys.modules["paramiko"] = stub
    spec = importlib.util.spec_from_file_location("capture_image_win", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_tool()


def _code_only(script: str) -> str:
    """Blank comment lines (keep offsets) so prose cannot satisfy a pin."""
    return "\n".join("" if ln.lstrip().startswith("#") else ln for ln in script.split("\n"))


PATCH_CALL_RE = re.compile(r'\bbash\s+"?\$\{?REPO\}?/tools/imaging/patch-firstboot-image\.sh"?')


class FinalizeBase(unittest.TestCase):
    name = "SA-02m-test"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "out dir"
        self.out.mkdir()
        self.raw = self.out / f"{self.name}.raw.img"
        self.raw.write_bytes(RAW_BYTES)
        self.raw_sha = hashlib.sha256(RAW_BYTES).hexdigest()
        self.raw_sidecar = self.out / f"{self.raw.name}.sha256"
        self.raw_sidecar.write_text(f"{self.raw_sha}  {self.raw.name}\n", encoding="utf-8")
        # The WSL step writes these; the mocked check_call does not.
        self.img_sha = "a" * 64
        self.xz_sha = "b" * 64
        (self.out / f"{self.name}.img.sha256").write_text(f"{self.img_sha}  {self.name}.img\n", encoding="utf-8")
        (self.out / f"{self.name}.img.xz.sha256").write_text(f"{self.xz_sha}  {self.name}.img.xz\n", encoding="utf-8")
        self.wsl_map = {
            str(self.raw): "/mnt/c/o'dir/out dir/" + self.raw.name,
            str(self.out): "/mnt/c/o'dir/out dir",
            str(MOD.REPO): "/mnt/c/repo root",
        }
        p1 = mock.patch.object(MOD.subprocess, "check_output", side_effect=self._wslpath)
        p2 = mock.patch.object(MOD.subprocess, "check_call")
        self.check_output = p1.start()
        self.check_call = p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(self._tmp.cleanup)

    def _wslpath(self, argv, **_kw):
        return self.wsl_map.get(argv[-1], "/mnt/unmapped/" + Path(argv[-1]).name) + "\n"

    def run_finalize(self, ip: str = TEST_IP):
        # Tolerates the pre-fix 3-argument form so a RED run reports each
        # missing property, not one TypeError; test_takes_source_ip pins the
        # signature itself.
        params = inspect.signature(MOD.finalize_wsl).parameters
        if "cleanup" in params:
            return MOD.finalize_wsl(self.raw, self.name, self.out, ip, **DEFAULT_FLAGS)
        if "source_ip" in params:
            return MOD.finalize_wsl(self.raw, self.name, self.out, ip)
        return MOD.finalize_wsl(self.raw, self.name, self.out)

    def wsl_call(self):
        self.assertTrue(self.check_call.called, "finalize never called WSL")
        argv = self.check_call.call_args[0][0]
        i = argv.index("-lc")
        return argv[i + 1], argv[i + 2:]


class FinalizeScriptTests(FinalizeBase):
    def test_takes_source_ip(self):
        self.assertIn("source_ip", inspect.signature(MOD.finalize_wsl).parameters)

    def test_runs_patch_firstboot_through_bash(self):
        self.run_finalize()
        script, _ = self.wsl_call()
        self.assertRegex(_code_only(script), PATCH_CALL_RE)

    def test_invoked_patch_script_exists_in_repo(self):
        # The WSL script names the path literally; a rename of the real script
        # must turn this RED here, not at the WSL preflight on capture day.
        self.assertEqual(MOD.PATCH_SCRIPT_REL, "tools/imaging/patch-firstboot-image.sh")
        self.assertTrue((MOD.REPO / MOD.PATCH_SCRIPT_REL).is_file())
        self.assertIn('"$REPO/' + MOD.PATCH_SCRIPT_REL + '"', _code_only(MOD.FINALIZE_SCRIPT))

    def test_strict_mode(self):
        self.run_finalize()
        script, _ = self.wsl_call()
        self.assertRegex(_code_only(script), r"(?m)^\s*set -euo pipefail\s*$")

    def test_step_order_verify_shrink_patch_xz(self):
        self.run_finalize()
        code = _code_only(self.wsl_call()[0])
        marks = {
            "sha256 verify": code.find("sha256sum -c"),
            "pishrink": code.find("pishrink.sh -a"),
            "patch": (PATCH_CALL_RE.search(code).start() if PATCH_CALL_RE.search(code) else -1),
            "xz": code.find("xz -T0"),
        }
        missing = [k for k, v in marks.items() if v < 0]
        self.assertFalse(missing, f"steps absent from the WSL script: {missing}")
        order = sorted(marks, key=marks.get)
        self.assertEqual(order, ["sha256 verify", "pishrink", "patch", "xz"])

    def test_patch_call_is_bare_and_unconditional(self):
        self.run_finalize()
        lines = _code_only(self.wsl_call()[0]).split("\n")
        hits = [i for i, ln in enumerate(lines) if PATCH_CALL_RE.search(ln)]
        self.assertEqual(len(hits), 1, "expected exactly one patch-firstboot-image.sh invocation")
        line = lines[hits[0]]
        # `|| true` / `|| :` swallow it; `&&` with the patch first or `!` makes
        # set -e ignore its status; an enclosing if/loop makes it conditional.
        self.assertNotRegex(line, r"\|\||&&")
        self.assertNotRegex(line, r"^\s*!")
        self.assertRegex(line, r"^\s*sudo\s+bash\s")
        depth = 0
        for ln in lines[: hits[0]]:
            if re.match(r"\s*(if|while|until|for|case)\b", ln):
                depth += 1
            if re.search(r"(^|;)\s*(fi|done|esac)\b", ln):
                depth -= 1
        self.assertEqual(depth, 0, "patch invocation sits inside a conditional/loop")

    def test_inline_mini_patch_is_gone(self):
        self.run_finalize()
        code = _code_only(self.wsl_call()[0])
        for pat in (r"losetup\s+--partscan", r"losetup\s+-d", r"\bsudo\s+mount\b", r"\bumount\b",
                    r"system\.conf\.d", r"RuntimeWatchdogSec", r"sa02m-rootfs-expand\.done"):
            self.assertNotRegex(code, pat, f"inline mini-patch remnant: {pat}")

    def test_expected_raw_sha_reaches_wsl(self):
        self.run_finalize()
        _, args = self.wsl_call()
        self.assertIn(self.raw_sha, args)

    def test_missing_raw_sidecar_stops_before_wsl(self):
        self.raw_sidecar.unlink()
        with self.assertRaises(SystemExit):
            self.run_finalize()
        self.check_output.assert_not_called()
        self.check_call.assert_not_called()

    def test_malformed_raw_sidecar_stops_before_wsl(self):
        for body in ("not-a-hash  " + self.raw.name + "\n",
                     f"{self.raw_sha}  some-other.raw.img\n",
                     ""):
            with self.subTest(body=body):
                self.raw_sidecar.write_text(body, encoding="utf-8")
                self.check_call.reset_mock()
                self.check_output.reset_mock()
                with self.assertRaises(SystemExit):
                    self.run_finalize()
                self.check_output.assert_not_called()
                self.check_call.assert_not_called()

    def test_wsl_failure_propagates_and_writes_no_manifest(self):
        self.check_call.side_effect = subprocess.CalledProcessError(1, "wsl")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_finalize()
        self.assertFalse((self.out / f"{self.name}.manifest.json").exists())

    def test_manifest_records_real_ip_and_patch(self):
        self.run_finalize(TEST_IP)
        doc = json.loads((self.out / f"{self.name}.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["source_device"]["ip"], TEST_IP)
        self.assertIs(doc["pipeline"].get("patch_firstboot"), True)
        self.assertIs(doc["pipeline"].get("pishrink"), True)
        self.assertEqual(doc["pipeline"].get("patch_script"), "tools/imaging/patch-firstboot-image.sh")
        self.assertEqual(doc.get("raw_sha256"), self.raw_sha)
        self.assertEqual(doc.get("img_name"), f"{self.name}.img")
        self.assertEqual(doc["image_name"], f"{self.name}.img.xz")
        self.assertEqual(doc["image_sha256"], self.xz_sha)
        self.assertEqual(doc["img_sha256"], self.img_sha)

    def test_manifest_records_real_capture_flags(self):
        # The pipeline block used to claim cleanup + id reset whatever flags ran.
        for flags in ({"cleanup": False, "zerofill": False, "id_reset": False},
                      {"cleanup": True, "zerofill": False, "id_reset": True},
                      DEFAULT_FLAGS):
            with self.subTest(**flags):
                MOD.finalize_wsl(self.raw, self.name, self.out, TEST_IP, **flags)
                doc = json.loads((self.out / f"{self.name}.manifest.json").read_text(encoding="utf-8"))
                self.assertIs(doc["pipeline"].get("cleanup"), flags["cleanup"])
                self.assertIs(doc["pipeline"].get("zerofill"), flags["zerofill"])
                self.assertIs(doc["pipeline"].get("id_reset_in_stream"), flags["id_reset"])

    def test_main_forwards_capture_flags(self):
        argv = ["capture-image-win.py", "--ip", TEST_IP, "--name", self.name, "--out-dir", str(self.out),
                "--no-cleanup", "--no-zerofill", "--no-id-reset"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(MOD, "connect", return_value=mock.MagicMock()), \
                mock.patch.object(MOD, "exec_bash", return_value=0), \
                mock.patch.object(MOD, "run_script_file", return_value=0) as cleanup_run, \
                mock.patch.object(MOD, "stream_image") as stream, \
                mock.patch.object(MOD, "finalize_wsl") as fin, \
                mock.patch.object(MOD.time, "sleep"):
            self.assertEqual(MOD.main(), 0)
        cleanup_run.assert_not_called()
        self.assertEqual(stream.call_args[0][3], "--no-zerofill --no-id-reset")
        fin.assert_called_once()
        self.assertEqual(fin.call_args[0][3], TEST_IP)
        self.assertEqual(fin.call_args[1], {"cleanup": False, "zerofill": False, "id_reset": False})


class QuotedNameTests(FinalizeBase):
    """A `'` in --name or a path once broke out of the script's '...' literals."""

    name = "it's-a-name"

    def test_values_are_positional_never_interpolated(self):
        self.run_finalize()
        script, args = self.wsl_call()
        img, out, repo = (self.wsl_map[str(self.raw)], self.wsl_map[str(self.out)],
                          self.wsl_map[str(MOD.REPO)])
        self.assertEqual(args, ["_", img, out, self.name, repo, self.raw_sha])
        for v in (img, out, repo, self.name):
            self.assertNotIn(v, script)


class _Chan:
    def __init__(self, data: bytes, chunk: int = 97):
        self._chunks = [data[i:i + chunk] for i in range(0, len(data), chunk)]

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, _n):
        return b""

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def recv_ready(self):
        return bool(self._chunks)

    def exit_status_ready(self):
        return not self._chunks

    def recv_exit_status(self):
        return 0

    def shutdown_write(self):
        pass


class _Client:
    def __init__(self, data: bytes):
        self.data = data

    def exec_command(self, *_a, **_kw):
        chan = _Chan(self.data)
        stdin = types.SimpleNamespace(write=lambda _b: None, channel=chan)
        stdout = types.SimpleNamespace(channel=chan)
        stderr = types.SimpleNamespace(channel=chan)
        return stdin, stdout, stderr


class StreamSidecarTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.script = d / "stream.sh"
        self.script.write_bytes(b"echo\n")
        self.img = d / "o" / "x.raw.img"
        self.sidecar = d / "o" / "x.raw.img.sha256"

    def test_writes_sha256sum_sidecar_of_streamed_bytes(self):
        with mock.patch.object(MOD, "EMMC_BYTES", len(RAW_BYTES)):
            MOD.stream_image(_Client(RAW_BYTES), self.script, self.img)
        self.assertEqual(self.img.read_bytes(), RAW_BYTES)
        self.assertEqual(self.sidecar.read_text(encoding="utf-8"),
                         f"{hashlib.sha256(RAW_BYTES).hexdigest()}  x.raw.img\n")

    def test_size_mismatch_leaves_no_sidecar(self):
        self.img.parent.mkdir(parents=True)
        self.sidecar.write_text("stale\n", encoding="utf-8")
        with mock.patch.object(MOD, "EMMC_BYTES", len(RAW_BYTES) + 1):
            with self.assertRaises(SystemExit):
                MOD.stream_image(_Client(RAW_BYTES), self.script, self.img)
        self.assertFalse(self.sidecar.exists())


def _posix_bash(probe_dir: Path):
    """A bash that sees this process's paths and has /var/tmp, or (None, why)."""
    bash = shutil.which("bash")
    if not bash:
        return None, "no bash on PATH"
    probe = ('test -d "$1" || exit 3; test -d /var/tmp -a -w /var/tmp || exit 4; '
             'for t in sha256sum mktemp cp; do command -v "$t" >/dev/null || exit 5; done')
    try:
        rc = subprocess.run([bash, "-c", probe, "_", probe_dir.as_posix()],
                            capture_output=True, timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"bash probe failed: {exc}"
    why = {3: "bash cannot see this process's paths (WSL bash.exe?)",
           4: "bash has no writable /var/tmp", 5: "bash lacks sha256sum/mktemp/cp"}
    return (bash, "") if rc == 0 else (None, why.get(rc, f"bash probe rc={rc}"))


class FinalizeScriptExecutionTests(FinalizeBase):
    """Runs the generated WSL script for real with stubbed tools."""

    def setUp(self):
        super().setUp()
        root = Path(self._tmp.name) / "exec"
        self.bin = root / "bin"
        self.repo = root / "repo"
        self.xout = root / "xout"
        for d in (self.bin, self.repo / "tools" / "imaging", self.xout):
            d.mkdir(parents=True)
        self.bash, why = _posix_bash(root)
        if not self.bash:
            self.skipTest(why)
        self.log = root / "calls.log"
        stubs = {
            "sudo": 'exec "$@"\n',
            "pishrink.sh": f'echo "pishrink $*" >> "{self.log.as_posix()}"\n',
            "xz": 'for a; do last=$a; done; cat "$last"\n',
            "losetup": "exit 0\n",
            "python3": "exit 0\n",
        }
        for name, body in stubs.items():
            p = self.bin / name
            p.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
            p.chmod(0o755)
        self.patch_script = self.repo / "tools" / "imaging" / "patch-firstboot-image.sh"
        self.run_finalize()
        self.script, _ = self.wsl_call()

    def _exec(self, patch_body: str, sha: str):
        self.patch_script.write_text(
            f'echo "patch $*" >> "{self.log.as_posix()}"\n{patch_body}', encoding="utf-8", newline="\n")
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        args = [self.raw.as_posix(), self.xout.as_posix(), self.name, self.repo.as_posix(), sha]
        return subprocess.run([self.bash, "-c", self.script, "_", *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def _calls(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_patch_failure_aborts_before_xz(self):
        r = self._exec("exit 1\n", self.raw_sha)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("patch ", self._calls())
        self.assertFalse((self.xout / f"{self.name}.img.xz").exists())

    def test_sha_mismatch_aborts_before_pishrink(self):
        r = self._exec("exit 0\n", "0" * 64)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("pishrink", self._calls())

    def test_clean_run_publishes_img_xz_and_sums(self):
        r = self._exec("exit 0\n", self.raw_sha)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self._calls()
        self.assertLess(calls.index("pishrink"), calls.index("patch "))
        for f in (f"{self.name}.img", f"{self.name}.img.xz",
                  f"{self.name}.img.sha256", f"{self.name}.img.xz.sha256"):
            self.assertTrue((self.xout / f).is_file(), f)
        self.assertEqual((self.xout / f"{self.name}.img").read_bytes(), RAW_BYTES)


if __name__ == "__main__":
    unittest.main()
