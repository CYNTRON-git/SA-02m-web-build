# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import PackageError  # noqa: E402
from lib import transaction as txn  # noqa: E402
from lib import upload_receive as ur  # noqa: E402
from lib import validate_package as vp  # noqa: E402


def _sample_manifest(**overrides):
    payload_sha = overrides.pop("payload_sha256", "0" * 64)
    payload_size = overrides.pop("payload_size", 1)
    m = {
        "schema_version": 1,
        "product": "SA-02m",
        "model": "A40i",
        "arch": "armv7l",
        "version": "1.0.5.67",
        "repo_commit": "a" * 40,
        "built_at": "2026-08-07T16:00:00Z",
        "signing_key_id": "release-2026-08",
        "min_updater": "1.0.5.66",
        "min_version": "1.0.5.60",
        "payload": {
            "size": payload_size,
            "sha256": payload_sha,
            "uncompressed_size_max": 134217728,
        },
        "preflight": {
            "commands": ["/bin/bash", "/usr/bin/python3"],
            "free_bytes_min": 67108864,
            "free_bytes_multiplier": 3,
        },
        "deploy": [
            {
                "src": "www/network_config/index.html",
                "dst": "/var/www/network_config/index.html",
                "mode": "0644",
                "owner": "www-data:www-data",
            }
        ],
        "services": {
            "daemon_reload": True,
            "stop_before_apply": ["sa02m-flasher"],
            "restart": ["fcgiwrap", "nginx"],
            "health": {
                "http_url": "http://127.0.0.1:9999/login.html",
                "units_active": ["nginx", "fcgiwrap"],
                "version_file": "/var/www/network_config/VERSION",
            },
        },
        "delete": [],
        "migrations": [],
    }
    m.update(overrides)
    return m


def _build_payload_gz(version: str = "1.0.5.67") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = (version + "\n").encode("utf-8")
        info = tarfile.TarInfo(name="www/network_config/VERSION")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        html = b"<html>ok</html>\n"
        info2 = tarfile.TarInfo(name="www/network_config/index.html")
        info2.size = len(html)
        tf.addfile(info2, io.BytesIO(html))
    return buf.getvalue()


def _sign_manifest(manifest: dict, private_pem: bytes) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_pem, password=None)
    sig = key.sign(vp.signature_message(manifest))
    return base64.b64encode(sig).decode("ascii") + "\n"


def _build_sa02m(manifest: dict, payload: bytes, sig_text: str) -> bytes:
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode="w:") as tf:
        for name, blob in (
            ("manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8")),
            ("manifest.sig", sig_text.encode("utf-8")),
            ("payload.tar.gz", payload),
            ("payload.sha256", (hashlib.sha256(payload).hexdigest() + "  payload.tar.gz\n").encode("utf-8")),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    tar_bytes = outer.getvalue()
    assert len(tar_bytes) % 512 == 0
    return tar_bytes + vp.FOOTER


class TestValidateContainer(unittest.TestCase):
    def test_footer_len_is_21(self) -> None:
        self.assertEqual(len(b"SA02M_UPDATE_END_V1"), 19)
        self.assertEqual(vp.FOOTER_LEN, 21)
        self.assertEqual(vp.FOOTER, b"SA02M_UPDATE_END_V1\0\0")

    def test_trailer_ok(self) -> None:
        raw = b"x" * 1024
        pad = (512 - (len(raw) % 512)) % 512
        blob = raw + (b"\0" * pad) + vp.FOOTER
        path = Path(tempfile.mkdtemp()) / "t.sa02m"
        path.write_bytes(blob)
        try:
            ts = vp.validate_container(path)
            self.assertEqual(ts % 512, 0)
            self.assertEqual(ts, len(blob) - vp.FOOTER_LEN)
            # file_size itself need NOT be % 512
            self.assertNotEqual(len(blob) % 512, 0)
        finally:
            path.unlink(missing_ok=True)

    def test_bad_trailer(self) -> None:
        path = Path(tempfile.mkdtemp()) / "bad.sa02m"
        path.write_bytes(b"x" * 2048)
        try:
            with self.assertRaises(PackageError) as cm:
                vp.validate_container(path)
            self.assertEqual(cm.exception.code, "E_TRAILER")
        finally:
            path.unlink(missing_ok=True)

    def test_tar_size_not_aligned(self) -> None:
        # tar_size = 1025 → not % 512; footer present
        blob = b"y" * 1025 + vp.FOOTER
        path = Path(tempfile.mkdtemp()) / "mis.sa02m"
        path.write_bytes(blob)
        try:
            with self.assertRaises(PackageError) as cm:
                vp.validate_container(path)
            self.assertEqual(cm.exception.code, "E_TAR")
        finally:
            path.unlink(missing_ok=True)

    def test_traversal_name(self) -> None:
        with self.assertRaises(PackageError) as cm:
            vp._check_inner_member_name("../evil")
        self.assertEqual(cm.exception.code, "E_TAR_TRAV")


class TestManifestAndPackage(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.keys = self.tmp / "keys"
        self.keys.mkdir()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self.priv = Ed25519PrivateKey.generate()
        self.priv_pem = self.priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_pem = self.priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        (self.keys / "release-2026-08.pem").write_bytes(pub_pem)

    def test_reject_unknown_manifest_key(self) -> None:
        m = _sample_manifest()
        m["extra"] = 1
        with self.assertRaises(PackageError) as cm:
            vp.validate_manifest_object(m)
        self.assertEqual(cm.exception.code, "E_MANIFEST")

    def test_reject_preserve_dst(self) -> None:
        m = _sample_manifest()
        m["deploy"] = [
            {
                "src": "x.conf",
                "dst": "/etc/sa02m_web.env",
                "mode": "0644",
                "owner": "root:root",
            }
        ]
        with self.assertRaises(PackageError) as cm:
            vp.validate_manifest_object(m)
        self.assertEqual(cm.exception.code, "E_MANIFEST")

    def test_domain_separated_sig_roundtrip(self) -> None:
        payload = _build_payload_gz()
        digest = hashlib.sha256(payload).hexdigest()
        manifest = _sample_manifest(payload_sha256=digest, payload_size=len(payload))
        # Ensure payload size/sha in nested dict
        manifest["payload"]["sha256"] = digest
        manifest["payload"]["size"] = len(payload)
        sig = _sign_manifest(manifest, self.priv_pem)
        pkg = _build_sa02m(manifest, payload, sig)
        path = self.tmp / "ok.sa02m"
        path.write_bytes(pkg)

        result = vp.validate_package(
            path,
            trusted_keys_dir=self.keys,
            installed_version="1.0.5.66",
            runner_version="1.0.5.66",
            check_compat=True,
            extract_to=self.tmp / "stage",
        )
        self.assertTrue(result["signature_ok"])
        self.assertEqual(result["version"], "1.0.5.67")
        self.assertTrue((self.tmp / "stage" / "overlay" / "www" / "network_config" / "VERSION").is_file())

        # Tamper trailer
        bad = path.read_bytes()[:-1] + b"X"
        bad_path = self.tmp / "bad-trailer.sa02m"
        bad_path.write_bytes(bad)
        with self.assertRaises(PackageError) as cm:
            vp.validate_package(bad_path, trusted_keys_dir=self.keys)
        self.assertEqual(cm.exception.code, "E_TRAILER")

        # Tamper signature (flip a base64 char carefully)
        bad_sig = base64.b64encode(b"\x00" * 64).decode("ascii") + "\n"
        bad_pkg = _build_sa02m(manifest, payload, bad_sig)
        bad_sig_path = self.tmp / "bad-sig.sa02m"
        bad_sig_path.write_bytes(bad_pkg)
        with self.assertRaises(PackageError) as cm:
            vp.validate_package(bad_sig_path, trusted_keys_dir=self.keys)
        self.assertEqual(cm.exception.code, "E_SIG")

        # Tamper payload hash
        other = payload + b"\x00"
        # rebuild with wrong digest in member but matching size mismatch path:
        # change payload bytes but keep manifest hash → E_HASH
        wrong = _build_sa02m(manifest, other, sig)
        wrong_path = self.tmp / "bad-hash.sa02m"
        wrong_path.write_bytes(wrong)
        with self.assertRaises(PackageError) as cm:
            vp.validate_package(wrong_path, trusted_keys_dir=self.keys)
        self.assertIn(cm.exception.code, {"E_HASH", "E_SIG", "E_MANIFEST"})

    def test_sig_rejects_without_domain(self) -> None:
        """Signing raw canonical JSON (no domain prefix) must fail verify."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        payload = _build_payload_gz()
        digest = hashlib.sha256(payload).hexdigest()
        manifest = _sample_manifest()
        manifest["payload"]["sha256"] = digest
        manifest["payload"]["size"] = len(payload)
        key = load_pem_private_key(self.priv_pem, password=None)
        bad_sig = base64.b64encode(key.sign(vp.canonical_manifest_bytes(manifest))).decode("ascii") + "\n"
        pkg = _build_sa02m(manifest, payload, bad_sig)
        path = self.tmp / "nodomain.sa02m"
        path.write_bytes(pkg)
        with self.assertRaises(PackageError) as cm:
            vp.validate_package(path, trusted_keys_dir=self.keys)
        self.assertEqual(cm.exception.code, "E_SIG")


class TestUploadReceive(unittest.TestCase):
    def test_multipart_no_fieldstorage(self) -> None:
        src = Path(ur.__file__).read_text(encoding="utf-8")
        self.assertNotIn("cgi.FieldStorage", src)
        self.assertNotIn("import cgi", src)
        self.assertNotIn("from cgi", src)

        payload = b"A" * 1024 + b"\0" * 0
        pad = (512 - (len(payload) % 512)) % 512
        blob = payload + (b"\0" * pad) + vp.FOOTER
        boundary = b"----sa02mbound"
        body = (
            b"--"
            + boundary
            + b"\r\n"
            + b'Content-Disposition: form-data; name="file"; filename="x.sa02m"\r\n'
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + blob
            + b"\r\n--"
            + boundary
            + b"--\r\n"
        )
        incoming = Path(tempfile.mkdtemp()) / "incoming"
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": f'multipart/form-data; boundary={boundary.decode("ascii")}',
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        result = ur.receive_multipart_file(environ, incoming_dir=incoming, validate=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["size"], len(blob))
        self.assertTrue((incoming / "package.sa02m").is_file())
        self.assertFalse((incoming / "package.partial").exists())


class TestTransaction(unittest.TestCase):
    def test_atomic_journal_and_stages(self) -> None:
        statedir = Path(tempfile.mkdtemp())
        obj = txn.default_transaction(stage="uploaded", target_version="1.0.5.67")
        txn.save_transaction(obj, statedir)
        loaded = txn.load_transaction(statedir)
        assert loaded is not None
        self.assertEqual(loaded["stage"], "uploaded")
        updated = txn.update_stage(statedir, "validating", progress_pct=10)
        self.assertEqual(updated["stage"], "validating")
        self.assertTrue(txn.cancel_allowed("backing_up"))
        self.assertFalse(txn.cancel_allowed("applying"))
        self.assertEqual(txn.recovery_action("uploaded"), "wipe")
        self.assertEqual(txn.recovery_action("applying"), "rollback")

    def test_lock_exclusive(self) -> None:
        statedir = Path(tempfile.mkdtemp())
        with txn.held_lock(statedir):
            with self.assertRaises(PackageError) as cm:
                txn.UpdateLock(statedir).acquire(blocking=False)
            self.assertEqual(cm.exception.code, "E_LOCK")


if __name__ == "__main__":
    unittest.main()
