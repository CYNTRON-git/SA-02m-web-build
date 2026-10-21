"""HTTP -> real Bash CGI -> real controller/library integration; only systemd is fake."""
import base64
import hashlib
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

try:
    import fcntl  # noqa: F401 - the controller's lock idiom; Windows CPython has none
except ImportError:
    # This module drives real Bash CGIs and the fcntl-locking controller: Linux-only.
    # Skip the MODULE (pytest and unittest both honour SkipTest at import) and tell
    # the quality runner, which only parses unittest's native "OK (skipped=N)": a
    # line in QUALITY_SKIP_LOG makes the row PARTIAL, never a silent PASS.
    _log = os.environ.get("QUALITY_SKIP_LOG")
    if _log:
        with open(_log, "a", encoding="utf-8") as _f:
            _f.write("test_control_cgi: fcntl unavailable (Windows CPython) - the CGI/controller integration is Linux-only\n")
    raise unittest.SkipTest("fcntl unavailable: the CGI/controller integration module is Linux-only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import transaction as tx
from lib.control import Controller
from lib import PackageError
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_validate_package import _sample_manifest, _build_payload_gz, _build_sa02m, _sign_manifest

ROOT = Path(__file__).resolve().parents[3]
CGI = Path(os.environ.get("SA02M_TEST_CGI", str(ROOT / "www/network_config/cgi-bin")))


class TestControllerHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin)+":"+os.environ["PATH"],
                        SA02M_UPDATE_STATEDIR=str(self.state), SA02M_SESSION_DIR=str(self.root / "sessions"))
        self.keydir = self.root / "keys"
        self.keydir.mkdir()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        (self.keydir / "release-2026-08.pem").write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        payload = _build_payload_gz()
        manifest = _sample_manifest(payload_sha256=hashlib.sha256(payload).hexdigest(), payload_size=len(payload))
        self.package = _build_sa02m(manifest, payload, _sign_manifest(manifest, private))
        self.write_exec("inspect", "#!/usr/bin/python3\nimport sys,json\nfrom pathlib import Path\nsys.path.insert(0,"+
            repr(str(ROOT / "opt/sa02m-update"))+")\nfrom lib.validate_package import validate_package\n"+
            "d=validate_package(Path(sys.argv[1]), trusted_keys_dir=Path("+repr(str(self.keydir))+
            "), installed_version='1.0.5.66', runner_version='1.0.7.5', check_compat=True)\n"+
            "d['ok']=True; print(json.dumps(d))\n")
        self.write_exec("systemctl", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> "+str(self.root / "starts")+
                        "\n[ ! -e "+str(self.root / "fail-start")+" ]\n")
        self.write_exec("sudo", "#!/usr/bin/python3\nimport sys\nfrom pathlib import Path\nsys.path.insert(0,"+
            repr(str(ROOT / "opt/sa02m-update"))+")\nfrom lib.control import Controller,main\n"+
            "assert sys.argv[1:3]==['-n','/usr/local/libexec/sa02m-update-control']\n"+
            "sys.exit(main(Controller(Path("+repr(str(self.state))+"), inspect="+repr(str(self.bin / "inspect"))+
            ", systemctl="+repr(str(self.bin / "systemctl"))+", require_secure=False),sys.argv[3:]))\n")
        auth = subprocess.check_output(["bash", "-c", '. "$1"; t=$(web_session_create admin); echo "$t"; web_csrf_token_for_session "$t"',
                                        "auth", str(CGI / "lib_web_auth.sh")], env=self.env, text=True)
        self.token, self.csrf = auth.strip().splitlines()
        parent = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(size)
                env = dict(parent.env, REQUEST_METHOD="POST", CONTENT_LENGTH=str(size),
                           CONTENT_TYPE=self.headers.get("Content-Type", ""),
                           HTTP_COOKIE=self.headers.get("Cookie", ""),
                           HTTP_X_SA02M_CSRF=self.headers.get("X-SA02M-CSRF", ""))
                proc = subprocess.run(["bash", str(CGI / self.path.lstrip("/"))], input=body,
                                      env=env, capture_output=True, timeout=15)
                raw = proc.stdout.replace(b"\r\n", b"\n")
                head, sep, output = raw.partition(b"\n\n")
                code = 202 if b"Status: 202" in head else 200
                self.send_response(code); self.end_headers(); self.wfile.write(output if sep else raw)
            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def write_exec(self, name, text):
        p = self.bin / name
        p.write_text(text); p.chmod(0o755)

    def request(self, endpoint, body=b"", content_type="application/json", csrf=True, auth=True):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        headers = {"Content-Type": content_type}
        if auth: headers["Cookie"] = "session_token="+self.token
        if csrf: headers["X-SA02M-CSRF"] = self.csrf
        path = "/web_factory_reset.cgi" if endpoint == "factory-reset" else "/web_update_"+endpoint+".cgi"
        c.request("POST", path, body, headers)
        r=c.getresponse(); status=r.status; raw=r.read(); c.close()
        return status, json.loads(raw)

    def upload(self, package=None):
        body = (b'--abc\r\nContent-Disposition: form-data; name="file"; filename="test.sa02m"\r\n'
                b'Content-Type: application/octet-stream\r\n\r\n'+
                (self.package if package is None else package)+b'\r\n--abc--\r\n')
        return self.request("upload", body, "multipart/form-data; boundary=abc")

    def test_upload_apply_repeat_and_cooperative_cancel(self):
        status, result=self.upload()
        self.assertTrue(result.get("ok"), result)
        self.assertEqual((self.state / "incoming/package.sa02m").read_bytes(), self.package)
        self.assertEqual(tx.load_transaction(self.state)["stage"], "uploaded")
        body=json.dumps({"confirm_version":"1.0.5.67"}).encode()
        status,result=self.request("apply",body)
        self.assertEqual(status,202,result)
        ident=result["transaction_id"]
        status,again=self.request("apply",body)
        self.assertEqual(again["transaction_id"],ident)
        self.assertEqual(len((self.root / "starts").read_text().splitlines()),1)
        self.assertIn("--no-block", (self.root / "starts").read_text())
        _, replacement=self.upload(b"bad")
        self.assertEqual(replacement["error_code"],"E_LOCK")
        self.assertEqual((self.state / "incoming/package.sa02m").read_bytes(),self.package)
        _, cancelled=self.request("cancel")
        self.assertTrue(cancelled.get("cancel_requested"),cancelled)
        self.assertEqual(tx.load_transaction(self.state)["stage"],"validating")
        self.assertEqual(json.loads((self.state / "cancel").read_text())["transaction_id"],ident)
        self.assertEqual(len((self.root / "starts").read_text().splitlines()),1)

    def test_invalid_confirm_start_failure_and_late_cancel(self):
        self.assertTrue(self.upload()[1]["ok"])
        _, mismatch=self.request("apply",b'{"confirm_version":"9.9.9"}')
        self.assertEqual(mismatch["error_code"],"E_COMPAT")
        self.assertFalse((self.root / "starts").exists())
        (self.root / "fail-start").touch()
        _, failure=self.request("apply",b'{"confirm_version":"1.0.5.67"}')
        self.assertEqual(failure["error_code"],"E_CMD")
        self.assertEqual(tx.load_transaction(self.state)["stage"],"error")
        tx.update_stage(self.state,"applying",result="pending")
        _, denied=self.request("cancel")
        self.assertEqual(denied["error_code"],"E_CANCEL")
        self.assertFalse((self.state / "cancel").exists())

    def test_factory_reset_sudo_refusal_answers_json(self):
        # An old sudoers file (no grant for sa02m-update-control) made sudo exit
        # non-zero with nothing on stdout, and the CGI answered an EMPTY body
        # after its headers (review A5): the panel saw a parse error, not a reason.
        self.write_exec("sudo", "#!/bin/sh\nexit 1\n")
        body = json.dumps({"confirm_phrase": "SA02M-RESET", "backup_ok": True}).encode()
        status, result = self.request("factory-reset", body)
        self.assertEqual(status, 200)
        self.assertEqual((result.get("ok"), result.get("error_code")), (False, "E_CMD"), result)
        self.assertIn("refresh", result.get("error_message", ""))
        self.assertFalse((self.root / "starts").exists())

    def test_auth_csrf_and_bad_upload(self):
        self.assertEqual(self.request("cancel",csrf=False)[1]["error_code"],"E_CSRF")
        self.assertEqual(self.request("cancel",auth=False)[1]["error"],"unauthorized")
        _, bad=self.upload(b"not a package")
        self.assertFalse(bad["ok"])
        self.assertFalse((self.state / "incoming/package.sa02m").exists())
        self.assertFalse((self.root / "starts").exists())

    def test_invalid_json_never_starts_internet_update(self):
        _, result=self.request("apply",b'{broken')
        self.assertEqual(result["error_code"],"E_REQUEST")
        self.assertFalse((self.root / "starts").exists())

    def test_truncated_multipart_cleans_partial(self):
        _, result=self.request("upload", b'--abc\r\nContent-Disposition: form-data; name="file"\r\n\r\nx',
                              "multipart/form-data; boundary=abc")
        self.assertEqual(result["error_code"],"E_UPLOAD")
        self.assertFalse((self.state / "incoming/package.partial").exists())

    def test_factory_reset_auth_confirmation_repeat_and_update_exclusion(self):
        body = b'{"confirm_phrase":"SA02M-RESET","backup_ok":true}'
        self.assertEqual(self.request("factory-reset", body, csrf=False)[1]["error_code"], "E_CSRF")
        self.assertEqual(self.request("factory-reset", body, auth=False)[1]["error_code"], "E_AUTH")
        for invalid in (b'{"confirm_phrase":"wrong","backup_ok":true}',
                        b'{"confirm_phrase":"SA02M-RESET","backup_ok":false}'):
            self.assertEqual(self.request("factory-reset", invalid)[1]["error_code"], "E_CANCEL")
        self.assertFalse((self.state / "transaction.json").exists())
        _, first = self.request("factory-reset", body)
        self.assertTrue(first["ok"], first)
        _, second = self.request("factory-reset", body)
        self.assertEqual(first["transaction_id"], second["transaction_id"])
        self.assertEqual(len((self.root / "starts").read_text().splitlines()), 1)
        self.assertEqual(self.upload()[1]["error_code"], "E_LOCK")
        self.assertEqual(self.request("cancel")[1]["error_code"], "E_CANCEL")
        self.assertEqual(tx.load_transaction(self.state)["operation"], "factory_reset")


    def test_dead_pending_and_maintenance_require_recovery_without_journal_change(self):
        pending = tx.default_transaction(source="file", stage="applying", target_version="1.0.5.67")
        pending["updated_at"] = "2000-01-01T00:00:00Z"
        tx.atomic_write_json(self.state / "transaction.json", pending)
        before = (self.state / "transaction.json").read_bytes()
        _, result = self.request("apply", b'{"confirm_version":"1.0.5.67"}')
        self.assertEqual(result["error_code"], "E_RECOVERY_REQUIRED")
        self.assertEqual((self.state / "transaction.json").read_bytes(), before)
        tx.update_stage(self.state, "error")
        (self.state / "maintenance").write_text(pending["id"])
        before = (self.state / "transaction.json").read_bytes()
        _, result = self.upload()
        self.assertEqual(result["error_code"], "E_RECOVERY_REQUIRED")
        self.assertEqual((self.state / "transaction.json").read_bytes(), before)
        self.assertFalse((self.state / "incoming/package.sa02m").exists())


    def test_control_serializes_requests_and_online_start_is_idempotent(self):
        with (self.state / "control.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.upload()[1]["error_code"], "E_LOCK")
        controller = Controller(self.state, systemctl=str(self.bin / "systemctl"), require_secure=False)
        first = controller.execute(["online"], io.BytesIO())
        second = controller.execute(["online"], io.BytesIO())
        self.assertEqual(first["http_status"], 202)
        self.assertEqual(first["transaction_id"], second["transaction_id"])
        self.assertEqual(len((self.root / "starts").read_text().splitlines()), 1)
        self.assertIn("sa02m-update-download.service", (self.root / "starts").read_text())
        self.assertEqual(self.request("factory-reset", b'{"confirm_phrase":"SA02M-RESET","backup_ok":true}')[1]["error_code"], "E_LOCK")
        self.assertFalse((self.state / "deployed_commit").exists())


    def test_launch_exception_never_reports_success_or_replaces_unknown_start(self):
        controller = Controller(self.state, systemctl=str(self.bin / "missing"), require_secure=False)
        with self.assertRaises(PackageError) as missing:
            controller.execute(["online"], io.BytesIO())
        self.assertEqual(missing.exception.code, "E_CMD")
        self.assertEqual(tx.load_transaction(self.state)["stage"], "error")
        with patch("lib.control.subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 15)):
            with self.assertRaises(PackageError) as timeout:
                controller.execute(["online"], io.BytesIO())
        self.assertEqual(timeout.exception.code, "E_RECOVERY_REQUIRED")
        self.assertEqual(tx.load_transaction(self.state)["stage"], "validating")
        self.assertFalse((self.state / "deployed_commit").exists())


    def test_runner_lock_prevents_upload_even_without_journal(self):
        with tx.held_lock(self.state):
            _, result=self.upload()
        self.assertEqual(result["error_code"],"E_LOCK")
        self.assertFalse((self.state / "incoming/package.sa02m").exists())


if __name__ == "__main__":
    unittest.main()
