"""Execute the actual CGI with isolated status files and an authenticated fixture."""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[3]
CGI = ROOT / "www/network_config/cgi-bin/gateway_status.cgi"


class GatewayStatusTests(unittest.TestCase):
    def test_stale_and_inactive_do_not_publish_running(self):
        for state, age in (("active", 0), ("active", 31), ("inactive", 0), ("activating", 0)):
            with self.subTest(state=state, age=age), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                snapshot = path / "gateway.json"
                snapshot.write_text(json.dumps({
                    "timestamp": time.time() - age, "ports": {"COM4": {"running": True}}}))
                ownership = path / "ownership.json"
                ownership.write_text(json.dumps({
                    "broker_pid": os.getpid(),
                    "broker_start_ticks": Path("/proc/self/stat").read_text().split(") ", 1)[1].split()[19],
                    "configured_scopes": {"/dev/COM4": ["mplc4.service"]},
                    "services": {"mplc4.service": {"phase": "restore_failed", "error": "fixture"}},
                }))
                sock = socket.socket(socket.AF_UNIX)
                sock.bind(str(path / "control.sock"))
                self.addCleanup(sock.close)
                source = CGI.read_text().replace("/run/sa02m-gateway/status.json", str(snapshot))
                source = source.replace("/run/sa02m-port-coordinator/status.json", str(ownership))
                source = source.replace("/run/sa02m-port-coordinator/control.sock", str(path / "control.sock"))
                (path / "cgi").write_text(source)
                (path / "lib_web_auth.sh").write_text("web_session_check_cookie() { return 0; }\n")
                (path / "systemctl").write_text("#!/bin/sh\nprintf '%s\\n' '" + state + "'\n")
                (path / "systemctl").chmod(0o755)
                result = subprocess.run(["bash", str(path / "cgi")], check=True,
                                        capture_output=True, text=True,
                                        env={**os.environ, "PATH": tmp + ":/usr/bin:/bin"})
                body = json.loads(result.stdout.split("\n\n", 1)[1])
                self.assertEqual(body["ports"]["COM4"]["running"], state == "active" and age == 0)
                self.assertTrue(body["port_coordinator"]["available"])
                self.assertEqual(body["port_coordinator"]["services"]["mplc4.service"]["phase"],
                                 "restore_failed")


if __name__ == "__main__":
    unittest.main()
