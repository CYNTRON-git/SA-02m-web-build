#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes production shell/CGI code with fake bus executables and real authentication files.
"""Passive status and variant capability integration, without a physical bus."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
CGI = ROOT / "www/network_config/cgi-bin"
if os.name == "nt":
    print("SKIP: hardware shell composition requires Linux")
    sys.exit(77)

import fcntl


class HardwareCapabilities(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.variant = self.root / "variant.conf"
        self.conf = self.root / "hw.conf"
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.regs = self.root / "regs.json"
        self.ops = self.root / "ops"
        self.seed()
        self.conf.write_text("SA02M_HW_BACKEND=i2c_expander\nSA02M_I2C_RESPECT_OWNER=0\n"
                             f"SA02M_I2C_LOCK_FILE={self.root}/bus.lock\n"
                             "SA02M_I2C_LOCK_WAIT_SEC=0.1\n"
                             f"SA02M_HW_METRICS_CACHE={self.cache}/hw_metrics.snapshot\n")
        tool = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
root=pathlib.Path(os.environ['TEST_ROOT']); p=root/'regs.json'
name=pathlib.Path(sys.argv[0]).name; reg=str(int(sys.argv[4],0))
with (root/'ops').open('a') as f: f.write(name+' '+reg+'\\n')
if os.environ.get('TEST_IO_FAIL'): sys.exit(1)
if os.environ.get('TEST_SLOW'): time.sleep(2)
data=json.loads(p.read_text())
if name=='i2cget': print(hex(data[reg]))
else:
    if not (reg=='3' and os.environ.get('TEST_DIRECTION_STUCK')):
        data[reg]=int(sys.argv[5],0); p.write_text(json.dumps(data))
'''
        for name in ("i2cget", "i2cset"):
            p = self.bin / name
            p.write_text(tool)
            p.chmod(0o755)
        token, csrf = "a" * 64, "b" * 64
        sessions = self.root / "sessions"
        sessions.mkdir()
        digest = hashlib.sha256(token.encode()).hexdigest()
        (sessions / digest).write_text(f"{int(time.time()) + 600} admin\n")
        (sessions / (digest + ".csrf")).write_text(csrf + "\n")
        self.env = {**os.environ, "PATH": str(self.bin) + ":" + os.environ["PATH"],
                    "TEST_ROOT": str(self.root), "SA02M_SESSION_DIR": str(sessions),
                    "HTTP_COOKIE": "session_token=" + token, "HTTP_X_SA02M_CSRF": csrf}
        for name in ("lib_hw.sh", "lib_web_auth.sh", "hw_set.cgi", "status.cgi", "variant.cgi"):
            source = (CGI / name).read_text()
            source = source.replace("/etc/sa02m_hw_variant.conf", str(self.variant))
            source = source.replace("/etc/sa02m_hw.conf", str(self.conf))
            source = source.replace("/tmp/sa02m_status_cache", str(self.cache))
            source = source.replace("/var/log/sa02m_install.log", str(self.root / "success.log"))
            (self.root / name).write_text(source)

    def seed(self, out=0xFD, direction=0xFF, inversion=0x05):
        self.regs.write_text(json.dumps({"1": out, "2": inversion, "3": direction}))
        self.ops.write_text("")

    def shell(self, body, unprivileged=False, **env):
        prefix = ["runuser", "-u", "nobody", "--"] if unprivileged and os.geteuid() == 0 else []
        run = subprocess.run(prefix + ["bash", "-c", f'HW_CONF="{self.conf}"; . "{self.root}/lib_hw.sh";\n' + body],
                             env={**self.env, **env}, text=True, capture_output=True, timeout=8)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def request(self, body="channel=do&value=1", name="hw_set.cgi", **env):
        run = subprocess.run(["bash", str(self.root / name)], input=body, text=True,
                             capture_output=True, timeout=12, env={**self.env,
                                 "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)), **env})
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.split("\n\n", 1)[1])

    def collect(self):
        return json.loads(self.shell('sa02m_hw_collect_metrics; printf \'{"do":%s,"beeper":%s,"led":%s,"pin":%s,"absent":%s,"busy":%s}\' "$HW_DO" "$HW_BEEP" "$HW_LED" "$PIN_DO" "$HW_I2C_EXP_ABS" "$HW_I2C_BUSY"'))

    def test_input_direction_status_is_pure_and_unknown(self):
        original = self.regs.read_text()
        state = self.collect()
        self.assertEqual(self.regs.read_text(), original)
        self.assertNotIn("i2cset", self.ops.read_text())
        self.assertEqual(state, dict(do=-1, beeper=-1, led=-1, pin=1, absent=0, busy=0))

    def test_partial_direction_only_reports_output_channels(self):
        self.seed(direction=0xF4)
        state = self.collect()
        self.assertEqual((state["do"], state["beeper"], state["led"]), (1, -1, 0))
        self.assertNotIn("i2cset", self.ops.read_text())

    def test_two_eth_refuses_actual_authenticated_cgi_without_side_effects(self):
        self.variant.write_text("SA02M_HW_VARIANT=sa02m-2eth\n")
        original = self.regs.read_text()
        self.assertEqual(self.request(), {"ok": False, "error": "channel_unavailable"})
        self.assertEqual(self.regs.read_text(), original)
        self.assertEqual(self.ops.read_text(), "")
        self.assertFalse((self.cache / "hw_metrics.snapshot").exists())
        self.assertFalse((self.root / "success.log").exists())

    def test_input_direction_explicit_command_initializes_safely(self):
        self.assertTrue(self.request()["ok"])
        regs = json.loads(self.regs.read_text())
        self.assertEqual(regs, {"1": 0xFD, "2": 0, "3": 0xF0})
        writes = [line for line in self.ops.read_text().splitlines() if line.startswith("i2cset")]
        self.assertEqual(writes, ["i2cset 2", "i2cset 1", "i2cset 3", "i2cset 1"])

    def test_stuck_direction_cannot_report_success(self):
        self.assertEqual(self.request(TEST_DIRECTION_STUCK="1"), {"ok": False, "error": "write_failed"})
        self.assertFalse((self.root / "success.log").exists())

    def test_initialized_command_preserves_unrelated_latch_bits(self):
        self.seed(out=0xF8, direction=0xF0, inversion=0)
        self.assertTrue(self.request("channel=do&value=0")["ok"])
        self.assertEqual(json.loads(self.regs.read_text())["1"], 0xFA)

    def test_auth_method_csrf_and_input_guards_precede_hardware(self):
        for env, body, error in (({"HTTP_COOKIE": ""}, "channel=do&value=1", "unauthorized"),
                                 ({"REQUEST_METHOD": "GET"}, "channel=do&value=1", "method_not_allowed"),
                                 ({"HTTP_X_SA02M_CSRF": ""}, "channel=do&value=1", "csrf"),
                                 ({}, "channel=unknown&value=1", "bad_channel"),
                                 ({}, "channel=do&value=2", "bad_value")):
            with self.subTest(error=error):
                self.assertEqual(self.request(body, **env)["error"], error)
                self.assertEqual(self.ops.read_text(), "")

    def test_stale_snapshot_is_projected_on_load_and_patch(self):
        self.variant.write_text("SA02M_HW_VARIANT=sa02m-2eth\n")
        (self.cache / "hw_metrics.snapshot").write_text("HW_DO=1\nPIN_DO=1\nHW_BEEP=0\nHW_LED=0\nHW_USB=-1\n")
        for action in ("sa02m_hw_metrics_cache_refresh", "sa02m_hw_metrics_cache_load", "sa02m_hw_metrics_cache_patch_channel beeper 1"):
            with self.subTest(action=action):
                self.assertEqual(self.shell(action + '; printf "%s %s" "$HW_DO" "$PIN_DO"'), "null 0")
        self.assertNotIn("i2cset", self.ops.read_text())

    def test_shared_variant_matrix(self):
        matrix = json.loads((ROOT / "scripts/dev/fixtures/hardware-variants.json").read_text())
        self.root.chmod(0o755)
        for case in matrix:
            with self.subTest(case=case["name"]):
                if self.variant.is_dir():
                    self.variant.rmdir()
                elif self.variant.exists() or self.variant.is_symlink():
                    self.variant.unlink()
                kind = case.get("kind")
                if kind == "directory":
                    self.variant.mkdir()
                elif kind == "symlink":
                    self.variant.symlink_to(self.root / "absent")
                elif kind != "absent":
                    self.variant.write_bytes(case["text"].encode("utf-8") + bytes.fromhex(case.get("suffix_hex", "")))
                    if kind == "unreadable":
                        self.variant.chmod(0)
                self.assertEqual(self.shell("sa02m_hw_variant", unprivileged=kind == "unreadable"), case["variant"])

    def test_invalid_variant_bytes_refuse_authenticated_cgi_before_io(self):
        matrix = json.loads((ROOT / "scripts/dev/fixtures/hardware-variants.json").read_text())
        for case in matrix:
            if case.get("kind") or case["variant"] != "unknown":
                continue
            with self.subTest(case=case["name"]):
                self.seed()
                (self.cache / "hw_metrics.snapshot").unlink(missing_ok=True)
                (self.root / "success.log").unlink(missing_ok=True)
                self.variant.write_bytes(case["text"].encode("utf-8") + bytes.fromhex(case.get("suffix_hex", "")))
                original = self.regs.read_bytes()
                self.assertEqual(self.request(), {"ok": False, "error": "channel_unavailable"})
                self.assertEqual(self.regs.read_bytes(), original)
                self.assertEqual(self.ops.read_text(), "")
                self.assertFalse((self.cache / "hw_metrics.snapshot").exists())
                self.assertFalse((self.root / "success.log").exists())

    def test_variant_parser_needs_no_external_tools(self):
        self.variant.write_bytes("# UTF-8: café 🚦\nSA02M_HW_VARIANT='sa02m-1eth'\n".encode("utf-8"))
        self.assertEqual(self.shell("PATH=/nonexistent; sa02m_hw_variant"), "sa02m-1eth")

    def test_direct_write_paths_and_gpio_do_not_bypass_capability(self):
        self.variant.write_text("SA02M_HW_VARIANT=sa02m-2eth\n")
        for function in ("sa02m_hw_i2c_write_channel", "sa02m_hw_i2c_write_channel_web", "sa02m_hw_i2c_write_channel_locked", "sa02m_hw_gpio_write_channel"):
            with self.subTest(function=function):
                self.assertEqual(self.shell(function + ' do 1; printf "%s" "$?"'), "79")
                self.assertEqual(self.ops.read_text(), "")
        self.assertEqual(self.shell('SA02M_HW_BACKEND=gpio_sysfs; SA02M_GPIO_DO=9; SA02M_GPIO_BEEPER=8; sa02m_hw_gpio_state() { echo "read:$1" >>"$TEST_ROOT/gpio-reads"; echo 1; }; sa02m_hw_collect_metrics; printf "%s %s" "$HW_DO" "$PIN_DO"'), "null 0")
        self.assertIn("read:8", (self.root / "gpio-reads").read_text())
        self.assertNotIn("read:9", (self.root / "gpio-reads").read_text())

    def test_unsearchable_variant_parent_is_not_treated_as_absent(self):
        self.root.chmod(0o755)
        private = self.root / "private"
        private.mkdir()
        target = private / "variant.conf"
        target.write_text("SA02M_HW_VARIANT=sa02m-1eth\n")
        lib = self.root / "lib_hw.sh"
        lib.write_text(lib.read_text().replace(str(self.variant), str(target)))
        private.chmod(0)
        try:
            self.assertEqual(self.shell("sa02m_hw_variant", unprivileged=True), "unknown")
        finally:
            private.chmod(0o700)

    def test_busy_missing_tool_io_and_timeout_are_not_output_writes(self):
        commands = [
            ('sa02m_hw_i2c_owner_active() { return 0; }; ', {}, "1 0"),
            ('', {"TEST_IO_FAIL": "1"}, "0 1"),
            ('SA02M_I2C_TIMEOUT_SEC=0.05; ', {"TEST_SLOW": "1"}, "1 0"),
        ]
        for setup, env, expected in commands:
            with self.subTest(expected=expected, setup=setup):
                self.assertEqual(self.shell(setup + 'sa02m_hw_collect_metrics; printf "%s %s" "$HW_I2C_BUSY" "$HW_I2C_EXP_ABS"', **env), expected)
        (self.bin / "i2cget").unlink()
        self.assertEqual(self.shell('sa02m_hw_i2c_read_output_dec; printf "%s" "$?"'), "78")
        self.assertNotIn("i2cset", self.ops.read_text())

    def test_bus_lock_timeout_is_busy(self):
        with (self.root / "bus.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.collect()["busy"], 1)
        self.assertEqual(self.ops.read_text(), "")

    def test_real_hardware_status_and_http_cache_follow_variant(self):
        self.seed(direction=0xF0)
        first = self.request("", name="status.cgi", REQUEST_METHOD="GET", QUERY_STRING="part=hardware")
        self.assertEqual(first["hw_do"], 1)
        reads = self.ops.read_text()
        cached = self.request("", name="status.cgi", REQUEST_METHOD="GET", QUERY_STRING="part=hardware")
        self.assertEqual(cached["hw_do"], 1)
        self.assertEqual(self.ops.read_text(), reads)
        self.variant.write_text("SA02M_HW_VARIANT=sa02m-2eth\n")
        for disabled in (0, 1):
            blocks = self.root / "blocks.conf"
            blocks.write_text(f"SA02M_STATUS_ENABLE_HARDWARE={1-disabled}\n")
            state = self.request("", name="status.cgi", REQUEST_METHOD="GET", QUERY_STRING="part=hardware", STATUS_BLOCKS_CONF=str(blocks))
            self.assertIsNone(state["hw_do"])
            self.assertEqual(state["hw_pin_do"], 0)
            for path in self.cache.glob("hardware.*.json"):
                path.unlink()
        self.assertNotIn("i2cset", self.ops.read_text())

    def test_main_full_composition_normalizes_live_and_cached_hardware(self):
        source = (self.root / "status.cgi").read_text()
        functions = dict(re.findall(r"^(\w+)\(\) \{\n(.*?)^\}\n(?=\n|$)", source, re.M | re.S))
        definitions = "\n".join(f"{name}() {{\n{body}}}" for name, body in functions.items())
        # Replace unrelated collectors, retaining the real main/full assembly,
        # hardware collector, renderers and status/cache helpers.
        unrelated = [name for name in functions if name.startswith("gather_") and name not in ("gather_hardware_metrics", "gather_main_metrics")]
        stubs = "\n".join(f"{name}() {{ :; }}" for name in unrelated)
        names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", functions["print_full_json"] + functions["print_main_json"]))
        initial = "\n".join(f"{name}=0" for name in names)
        initial += '\nRS485_JSON="[]"; OPTIONAL_SVCS_JSON="[]"; DASH_SVCS_JSON="[]"; build_rs485_array() { echo "[]"; }; HW_VARIANT=$(sa02m_hw_variant)\n'
        self.variant.write_text("SA02M_HW_VARIANT=sa02m-2eth\n")
        for enabled in (0, 1):
            for part in ("main", "full"):
                with self.subTest(enabled=enabled, part=part):
                    (self.cache / "hw_metrics.snapshot").write_text("HW_DO=1\nPIN_DO=1\nHW_BEEP=0\nHW_LED=0\nHW_USB=-1\nHW_I2C_BUSY=0\nHW_I2C_EXP_ABS=0\n")
                    output = self.shell(definitions + "\n" + stubs + "\n" + initial + f"\nSA02M_STATUS_ENABLE_HARDWARE={enabled}; build_{part}_json")
                    state = json.loads(output)
                    self.assertIsNone(state["hw_do"])
                    self.assertEqual(state["hw_pin_do"], 0)
        self.assertNotIn("i2cset", self.ops.read_text())

    def test_variant_success_invalidates_data_but_preserves_lock_inodes(self):
        helper = self.root / "apply-variant"
        helper.write_text('#!/bin/sh\necho \'{"ok":true}\'\nexit "${TEST_VARIANT_EXIT:-0}"\n')
        helper.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text('#!/bin/sh\nexec "$@"\n')
        sudo.chmod(0o755)
        path = self.root / "variant.cgi"
        path.write_text(path.read_text().replace("/usr/local/sbin/sa02m-apply-variant.sh", str(helper)))
        for result in (1, 0):
            files = [self.cache / name for name in ("hardware.json", "hardware.sa02m-1eth.json", "hardware.sa02m-2eth.json", "hardware.unknown.json", "hw_metrics.snapshot", "system.json", "main.json")]
            for file in files:
                file.write_text("cached")
                file.with_suffix(file.suffix + ".lock").touch()
            locks = {p: p.stat().st_ino for p in self.cache.glob("*.lock")}
            self.request("variant=sa02m-2eth", name="variant.cgi", TEST_VARIANT_EXIT=str(result))
            self.assertEqual([p.exists() for p in files], [bool(result)] * len(files))
            self.assertEqual({p: p.stat().st_ino for p in self.cache.glob("*.lock")}, locks)

    def test_boot_initializer_remains_the_explicit_service_target(self):
        unit = (ROOT / "etc/systemd/sa02m-pre-start.service").read_text().splitlines()
        self.assertIn("ExecStart=/usr/local/sbin/sa02m-pre-start.sh", [line.strip() for line in unit if not line.lstrip().startswith("#")])
        boot = (ROOT / "etc/sa02m-pre-start.sh").read_text()
        self.assertRegex(boot, r"(?m)^\s+sa02m_pca9536_boot_indication\s+\|\|")


if __name__ == "__main__":
    unittest.main(verbosity=2)
