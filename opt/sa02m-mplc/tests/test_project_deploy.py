"""Run the CGI and privileged helper against isolated directories and a simulated RT."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from .test_project_zip import GOOD_MEMBERS, GOOD_PROJINFO, _zip_bytes


ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(shutil.which("flock") and shutil.which("timeout"), "requires Linux service utilities")
class ProjectDeployment(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.cfg = self.base / "server/cfg"
        self.cfg.mkdir(parents=True)
        self.cfg.chmod(0o750)
        self.state = self.base / "state"
        (self.state / "incoming").mkdir(parents=True)
        self.actions = self.base / "actions"
        self.monitor = self.base / "monitor"
        self.reply = self.base / "response"
        self.meta = json.loads(GOOD_PROJINFO)
        self.meta["VersionEditsInfo"]["ProjectVersion"] = 7
        self.archive = _zip_bytes({**GOOD_MEMBERS, "cfg/ProjInfo.json": json.dumps(self.meta).encode()})
        self.env = dict(os.environ, PATH=f"{self.base}:{os.environ['PATH']}", REQUEST_METHOD="POST",
                        CONTENT_TYPE="multipart/form-data; boundary=bench")
        self.script("sudo", "shift\nexec \"$@\"")
        self.script("curl", f"cat '{self.reply}'")
        self.script("svc", f"echo \"$1\" >>'{self.actions}'\n"
                    f"if [ \"$1\" = start ]; then echo 'load successful' >>'{self.monitor}'; fi")
        (self.base / "lib_web_auth.sh").write_text("web_session_check_cookie() { return 0; }\nweb_csrf_require() { return 0; }\n")
        constants = {
            "/opt/mplc4/server": str(self.cfg.parent),
            "/var/lib/sa02m-mplc": str(self.state),
            "/opt/sa02m-mplc": str(ROOT / "opt/sa02m-mplc"),
            "/usr/local/sbin/sa02m-web-service-ctl.sh": str(self.base / "svc"),
            "/usr/local/sbin/sa02m-mplc-project-deploy.sh": str(self.base / "helper"),
            "/var/log/mplc4/monitor/mplc_monitor.log": str(self.monitor),
            "/run/sa02m-flasher/flasher.sock": str(self.base / "absent.sock"),
            "/var/log/sa02m_install.log": str(self.base / "log"),
            "VERIFY_TIMEOUT=60": "VERIFY_TIMEOUT=4",
        }
        for src, dest in [("etc/sa02m-mplc-project-deploy.sh", "helper"),
                          ("www/network_config/cgi-bin/mplc_project_deploy.cgi", "cgi")]:
            code = (ROOT / src).read_text()
            for old, new in constants.items():
                code = code.replace(old, new)
            (self.base / dest).write_text(code)
            (self.base / dest).chmod(0o755)

    def script(self, name, body):
        dest = self.base / name
        dest.write_text("#!/bin/sh\n" + body + "\n")
        dest.chmod(0o755)

    def post(self, extra_bytes=0):
        body = (b'--bench\r\nContent-Disposition: form-data; name="file"; filename="project.zip"\r\n'
                b'Content-Type: application/zip\r\n\r\n' + self.archive + b'\r\n--bench--\r\n')
        run = subprocess.run([self.base / "cgi"], input=body, capture_output=True, timeout=8,
                             env=dict(self.env, CONTENT_LENGTH=str(len(body) + extra_bytes)), check=True)
        return json.loads(run.stdout.split(b"\r\n\r\n", 1)[1])

    def complete(self):
        for _ in range(120):
            status = json.loads((self.state / "status.json").read_text())
            if not status["pending"]:
                return status
            time.sleep(0.1)
        self.fail("deployment did not reach a terminal status")

    def test_multipart_loads_matching_version_and_rejects_concurrent_submit(self):
        self.script("svc", f"echo \"$1\" >>'{self.actions}'; if [ \"$1\" = stop ]; then sleep 2; fi")
        self.reply.write_text(json.dumps({"code": 0, "projectId": self.meta["ProjectId"].upper(), "projectVersion": 7}))
        self.assertTrue(self.post()["pending"])
        self.assertEqual(self.post()["error_code"], "E_LOCK")
        self.assertEqual(self.complete()["result"], "success")
        self.assertEqual((self.cfg / "config.bin").read_bytes(), GOOD_MEMBERS["cfg/config.bin"])
        self.assertEqual(list((self.state / "incoming").iterdir()), [])

    def test_wrong_version_restores_empty_directory_and_restarts_runtime(self):
        self.reply.write_text(json.dumps({"code": 0, "projectId": self.meta["ProjectId"], "projectVersion": 6}))
        self.script("curl", f"if [ ! -f '{self.cfg}/config.bin' ]; then echo '{{\"projectId\": \"\", \"projectVersion\": 0}}'; exit; fi\n"
                    f"if [ ! -f '{self.base}/first' ]; then touch '{self.base}/first'; exit 7; fi\n"
                    f"cat '{self.reply}'")
        self.assertTrue(self.post()["pending"])
        self.assertEqual(self.complete()["error_code"], "E_VERIFY")
        self.assertEqual(list(self.cfg.iterdir()), [])
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o750)
        self.assertEqual(self.actions.read_text().splitlines(), ["stop", "start", "stop", "start"])

    def test_legacy_load_message_and_truncated_request(self):
        self.reply.write_text("{}")
        self.assertEqual(self.post(extra_bytes=1)["error_code"], "E_UPLOAD")
        self.assertFalse(self.actions.exists())
        self.assertTrue(self.post()["pending"])
        self.assertEqual(self.complete()["result"], "success")

    def test_wrong_running_identity_after_rollback_is_reported(self):
        marker = self.cfg / "previous-project"
        marker.write_bytes(b"previous configuration")
        self.script("curl", f"if [ ! -f '{self.actions}' ]; then echo '{{\"projectId\":\"previous\",\"code\":0}}'; "
                    "else echo '{\"projectId\":\"other\",\"code\":0}'; fi")
        self.assertTrue(self.post()["pending"])
        status = self.complete()
        self.assertEqual((status["stage"], status["error_code"]), ("restore", "E_RESTORE"))
        self.assertEqual(marker.read_bytes(), b"previous configuration")

    def test_known_api_cannot_fall_back_to_log_when_it_disappears(self):
        self.script("curl", f"if [ -f '{self.cfg}/config.bin' ]; then exit 7; fi\n"
                    "echo '{\"projectId\":\"\",\"code\":0}'")
        self.assertTrue(self.post()["pending"])
        self.assertEqual(self.complete()["error_code"], "E_VERIFY")

    def test_null_identity_cannot_confirm_deployment_from_log(self):
        self.reply.write_text('{"projectId":null,"code":0}')
        self.assertTrue(self.post()["pending"])
        self.assertEqual(self.complete()["error_code"], "E_VERIFY")

    def test_unknown_initial_stop_preserves_cfg_without_backup_or_start(self):
        marker = self.cfg / "previous-project"
        marker.write_bytes(b"original")
        self.reply.write_text("{}")
        self.script("svc", f"echo \"$1\" >>'{self.actions}'; exit 1")
        self.assertTrue(self.post()["pending"])
        result = self.complete()
        self.assertEqual(result["error_code"], "E_STOP")
        self.assertEqual(marker.read_bytes(), b"original")
        self.assertEqual(self.actions.read_text().splitlines(), ["stop"])
        self.assertEqual(list((self.state / "backups").glob("*.tgz")), [])

    def test_unknown_replacement_stop_retains_new_cfg_and_prior_backup(self):
        import tarfile

        marker = self.cfg / "previous-project"
        marker.write_bytes(b"original")
        self.reply.write_text("{}")
        # The start client returns failure while its replacement job/runtime
        # remains live. The next stop cannot prove completion; rollback must
        # preserve both the running replacement and the recoverable backup.
        running = self.base / "replacement-running"
        self.script("svc", f"echo \"$1\" >>'{self.actions}'\n"
                    f"if [ \"$1\" = start ]; then touch '{running}'; exit 1; fi\n"
                    f"[ ! -e '{running}' ]")
        self.assertTrue(self.post()["pending"])
        result = self.complete()
        self.assertEqual((result["stage"], result["error_code"]), ("restore", "E_RESTORE"))
        self.assertIn("backup retained", result["error_message"])
        self.assertTrue(running.exists())
        self.assertFalse(marker.exists(), "rollback overwrote a possibly running replacement")
        self.assertEqual((self.cfg / "config.bin").read_bytes(), GOOD_MEMBERS["cfg/config.bin"])
        backups = list((self.state / "backups").glob("*.tgz"))
        self.assertEqual(len(backups), 1)
        with tarfile.open(backups[0]) as archive:
            self.assertEqual(archive.extractfile("cfg/previous-project").read(), b"original")
        self.assertEqual(self.actions.read_text().splitlines(), ["stop", "start", "stop"])
