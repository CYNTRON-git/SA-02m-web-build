#!/usr/bin/env python3
# comment-mutation-proof-exempt: runs real CGI and helper publication against temporary files; assertions inspect correlated responses, not source pins.
"""Operation identity, legacy compatibility and atomic result publication."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if os.name == "nt":
    print("SKIP: service shell/CGI composition requires Linux")
    sys.exit(77)
if os.geteuid() == 0:
    # Root imports and spawned children must not contaminate the caller's checkout.
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


class ServiceOperations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.result = self.root / "results"
        self.result.mkdir()
        self.helper = self.root / "helper.sh"
        self.helper.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"' + str(self.root / "args") + '"\n')
        self.helper.chmod(0o755)
        (self.root / "lib_web_auth.sh").write_text('web_session_check_cookie() { [ "${TEST_AUTH:-yes}" = yes ]; }\nweb_csrf_require() { [ "${TEST_CSRF:-yes}" = yes ] || { echo \'Content-type: application/json\'; echo; echo \'{"ok":false,"error":"E_CSRF"}\'; exit 0; }; }\n')
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "sudo").write_text('#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
        (bindir / "sudo").chmod(0o755)
        self.env = {**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"]}
        cgi = (ROOT / "www/network_config/cgi-bin/services_ctrl.cgi").read_text()
        cgi = cgi.replace("CTL=/usr/local/sbin/sa02m-web-service-ctl.sh", "CTL=" + str(self.helper))
        cgi = cgi.replace("RESULT_DIR=/var/run/sa02m-svcctl", "RESULT_DIR=" + str(self.result))
        cgi = cgi.replace("/var/log/sa02m_install.log", str(self.root / "log"))
        self.cgi = self.root / "services_ctrl.cgi"
        self.cgi.write_text(cgi)

    def request(self, query="", data=None, **env):
        body = "" if data is None else json.dumps(data)
        run = subprocess.run(["bash", str(self.cgi)], input=body, text=True, capture_output=True, timeout=5,
            env={**self.env, "REQUEST_METHOD": "GET" if data is None else "POST", "QUERY_STRING": query,
                 "CONTENT_LENGTH": str(len(body)), **env})
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.split("\n\n", 1)[1])

    def test_previous_success_and_error_cannot_complete_new_action(self):
        op = self.request(data={"id": "vplc", "action": "stop"})
        self.assertRegex(op["op_id"], "^[a-f0-9]{32}$")
        other = self.request(data={"id": "vplc", "action": "stop"})
        self.assertNotEqual(other["op_id"], op["op_id"])
        query = "result=1&id=vplc&op_id=" + op["op_id"]
        for old in ({"ok": True}, {"ok": False, "error": "old_failure"}):
            payload = {**old, "id": "vplc", "action": "stop", "op_id": "a" * 32}
            (self.result / "vplc.json").write_text(json.dumps(payload))
            self.assertTrue(self.request(query)["pending"])
            self.assertEqual(self.request("result=1&id=vplc"), payload)
        (self.result / "vplc.json").write_text(json.dumps(op))
        self.assertTrue(self.request(query)["pending"])
        final = {**op, "pending": False}
        (self.result / "vplc.json").write_text(json.dumps(final))
        self.assertEqual(self.request(query), final)

    def test_identifier_and_auth_refusals(self):
        for query in ("result=1&id=../vplc", "result=1&id=vplc/../x", "result=1&id=vplc&op_id=../x", "result=1&id=vplc&op_id=" + "a" * 33):
            self.assertFalse(self.request(query)["ok"], query)
        self.assertEqual(self.request("result=1&id=vplc", TEST_AUTH="no")["error"], "unauthorized")
        self.assertEqual(self.request(data={"id": "vplc", "action": "stop"}, TEST_CSRF="no")["error"], "E_CSRF")

    def test_unknown_helper_identity_never_publishes_a_file(self):
        source = (ROOT / "etc/sa02m-web-service-ctl.sh").read_text()
        source = source.replace("RESULT_DIR=/var/run/sa02m-svcctl", "RESULT_DIR=" + str(self.result))
        source = source.replace("SA02M_STACKS_LIB=/usr/local/lib/sa02m-stacks-policy.sh", "SA02M_STACKS_LIB=/nonexistent")
        self.helper.write_text(source)
        for identity, operation, error in (("unknown-service", "e" * 32, "unknown_service"), ("../vplc", "e" * 32, "invalid_id"), ("vplc", "../x", "invalid_operation")):
            run = subprocess.run(["sh", str(self.helper), "stop", identity, operation], text=True, capture_output=True, timeout=5)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(json.loads(run.stdout)["error"], error)
            self.assertEqual(list(self.result.iterdir()), [])

    def test_helper_publishes_whole_json_and_correlates_error(self):
        source = (ROOT / "etc/sa02m-web-service-ctl.sh").read_text().split("\nACTION=", 1)[0]
        source = source.replace("RESULT_DIR=/var/run/sa02m-svcctl", "RESULT_DIR=" + str(self.result))
        source = source.replace("SA02M_STACKS_LIB=/usr/local/lib/sa02m-stacks-policy.sh", "SA02M_STACKS_LIB=/nonexistent")
        source += '\nID=vplc\nACTION=stop\nSA02M_SVC_OPERATION_ID=' + 'b' * 32 + '\nbegin_result "$ID" "$ACTION"\ni=0\nwhile [ "$i" -lt 30 ]; do\nemit_result \'{"ok":false,"error":"port_busy"}\' >/dev/null\ni=$((i + 1))\ndone\n'
        script = self.root / "publish.sh"
        script.write_text(source)
        (self.root / "json.py").write_text("raise RuntimeError('caller-owned json module imported by root helper')\n")
        proc = subprocess.Popen(["sh", str(script)], cwd=self.root, stderr=subprocess.PIPE)
        errors, reads = [], 0
        while proc.poll() is None:
            try:
                payload = json.loads((self.result / "vplc.json").read_text())
                reads += 1
                self.assertEqual((payload["id"], payload["action"], payload["op_id"]), ("vplc", "stop", "b" * 32))
            except FileNotFoundError:
                pass
            except json.JSONDecodeError as error:
                errors.append(error)
        self.assertEqual(proc.returncode, 0, proc.stderr.read())
        proc.stderr.close()
        self.assertGreater(reads, 0)
        self.assertEqual(errors, [])
        self.assertEqual(payload["error"], "port_busy")

    def test_full_helper_guard_reexec_keeps_operation_and_refuses_live_lease(self):
        if os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo or subprocess.run([sudo, "-n", "true"], capture_output=True, timeout=5).returncode:
                self.skipTest("full root helper dispatch requires root or passwordless sudo")
            # CI runs the registry unprivileged with passwordless sudo. Re-run
            # this actual composition case as root, never waive its assertions.
            run = subprocess.run([sudo, "-n", sys.executable, "-B", str(Path(__file__).resolve()),
                                  "ServiceOperations.test_full_helper_guard_reexec_keeps_operation_and_refuses_live_lease"],
                                 cwd=ROOT, text=True, capture_output=True, timeout=40)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("Ran 1 test", run.stderr)
            self.assertNotIn("skipped=", run.stderr)
            return
        sys.path.insert(0, str(ROOT / "opt/sa02m-port-coordinator/tests"))
        import test_process_ownership as ownership
        h = ownership.ProcessOwnership()
        h.setUp()
        self.addCleanup(h.tearDown)
        package = self.root / "coordinator"
        shutil.copytree(ROOT / "opt/sa02m-port-coordinator/sa02m_ports", package / "sa02m_ports")
        client = package / "sa02m_ports/client.py"
        client.write_text(client.read_text().replace("/run/sa02m-port-coordinator", h.root + "/run"))
        bindir = self.root / "bin"
        sc_log = self.root / "systemctl.log"
        sc = bindir / "systemctl"
        sc.write_text('''#!/bin/sh
printf '%s\\n' "$*" >>"''' + str(sc_log) + '''"
case "$*" in
  *--property=Id*) echo "$2" ;;
  *LoadState*) echo loaded ;;
  *FragmentPath*) echo /lib/systemd/system/vplc.service ;;
  is-active*) echo inactive; exit 3 ;;
  is-enabled*) echo masked; exit 1 ;;
esac
exit 0
''')
        sc.chmod(0o755)
        for command in ("curl", "dpkg"):
            shim = bindir / command
            shim.write_text('#!/bin/sh\nexit 0\n')
            shim.chmod(0o755)
        source = (ROOT / "etc/sa02m-web-service-ctl.sh").read_text()
        source = source.replace("SC=/usr/bin/systemctl", "SC=" + str(sc))
        source = source.replace("/opt/sa02m-port-coordinator", str(package))
        source = source.replace("RESULT_DIR=/var/run/sa02m-svcctl", "RESULT_DIR=" + str(self.result))
        source = source.replace("/var/log/sa02m_install.log", str(self.root / "helper.log"))
        source = source.replace("SA02M_STACKS_LIB=/usr/local/lib/sa02m-stacks-policy.sh", "SA02M_STACKS_LIB=/nonexistent")
        self.helper.write_text(source)
        op = "c" * 32
        run = subprocess.run(["sh", str(self.helper), "stop", "vplc", op], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        final = json.loads(run.stdout)
        self.assertEqual(final, {"ok": True, "id": "vplc", "action": "stop", "op_id": op})
        self.assertEqual(json.loads((self.result / "vplc.json").read_text()), final)
        self.assertIn("stop vplc.service", sc_log.read_text())
        before = sc_log.read_text().count("stop vplc.service")
        with h.lease():
            refused = subprocess.run(["sh", str(self.helper), "stop", "mqtt-bridge", "d" * 32], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertNotEqual(refused.returncode, 0)
        final = json.loads(refused.stdout)
        self.assertEqual((final["error"], final["op_id"], final["action"]), ("port_busy", "d" * 32, "stop"))
        self.assertEqual(sc_log.read_text().count("stop vplc.service"), before)

    def test_passwordless_dispatch_path_runs_the_actual_root_case(self):
        if os.geteuid() != 0:
            self.test_full_helper_guard_reexec_keeps_operation_and_refuses_live_lease()
            return
        # In a root-only WSL session model the nonroot caller, using the sudo
        # exec shim. Its fresh Python child has the real root identity and
        # performs the complete broker/descriptor/helper scenario.
        with patch("os.geteuid", return_value=1000), patch.dict(os.environ, self.env):
            self.test_full_helper_guard_reexec_keeps_operation_and_refuses_live_lease()


if __name__ == "__main__":
    unittest.main()
