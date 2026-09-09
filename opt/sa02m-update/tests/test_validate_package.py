# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_REPO = Path(__file__).resolve().parents[3]
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

    def test_services_required_only_manifest_ok(self) -> None:
        # Pre-1.0.6.37 manifest shape (no optional services keys) stays valid:
        # the optional keys must never become required (older packers).
        vp.validate_manifest_object(_sample_manifest())

    @staticmethod
    def _load_packer():
        import importlib.util

        pack_path = _REPO / "scripts" / "pack-offline-update.py"
        spec = importlib.util.spec_from_file_location("pack_offline_update", pack_path)
        pack = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(pack)
        return pack

    def test_packer_services_tier_rule(self) -> None:
        # CONSCIOUS REWRITE (1.0.6.39, audit 2026-09-08 D6). The case this
        # replaces (test_packer_frozen_v1_for_min_updater_1_0_5_66) pinned the
        # regression GREEN: it asserted `enable` is absent from the packed
        # block, i.e. that no offline pack ever enables sa02m-dns-ensure.service
        # (contract docs/contracts/boot-network-dns.md), and it never touched
        # the production call site. What is pinned now is the TIER RULE:
        #   * below SERVICES_OPTIONAL_SINCE the block is frozen v1 — required
        #     keys only, sa02m-rules still in restart[] — and validates;
        #   * at/above it the block carries every optional key, the belt unit
        #     is in enable[], and it validates too;
        #   * build_services_block() with NO argument (what build_manifest
        #     calls) obeys the same rule for the module's own MIN_UPDATER —
        #     the branch the old test never exercised, which is how the tier
        #     stayed decorative (D2).
        pack = self._load_packer()
        legacy_keys = frozenset({"daemon_reload", "stop_before_apply", "restart", "health"})
        optional_keys = frozenset(pack.SERVICES_OPTIONAL_KEYS)
        self.assertEqual(optional_keys, vp._SERVICES_KEYS_OPTIONAL)

        frozen = pack.build_services_block("1.0.6.36")
        self.assertEqual(set(frozen), legacy_keys)
        self.assertIn("sa02m-rules", frozen["restart"])
        m = _sample_manifest()
        m["services"] = frozen
        vp.validate_manifest_object(m)

        full = pack.build_services_block(pack.SERVICES_OPTIONAL_SINCE)
        self.assertEqual(set(full), legacy_keys | optional_keys)
        self.assertIn("sa02m-dns-ensure.service", full["enable"])
        m = _sample_manifest()
        m["services"] = full
        vp.validate_manifest_object(m)

        self.assertFalse(pack.emit_optional_service_keys("1.0.6.36"))
        self.assertTrue(pack.emit_optional_service_keys(pack.SERVICES_OPTIONAL_SINCE))

        # The production site: no argument ⇒ the module's MIN_UPDATER decides.
        tier_on = pack.semver_key(pack.MIN_UPDATER) >= pack.semver_key(pack.SERVICES_OPTIONAL_SINCE)
        self.assertEqual(pack.emit_optional_service_keys(pack.MIN_UPDATER), tier_on)
        self.assertEqual(pack.build_services_block(), full if tier_on else frozen)
        manifest = pack.build_manifest(
            version="9.9.9.9",
            commit="a" * 40,
            key_id="release-2026-08",
            payload_gz=b"x",
            uncompressed_size=1,
            deploy=[],
        )
        self.assertEqual(manifest["services"], pack.build_services_block())
        self.assertEqual(manifest["min_updater"], pack.MIN_UPDATER)

    def test_packer_frozen_note_names_the_omitted_keys(self) -> None:
        # D7: the honest limit reaches the pack-time output, not only the docs.
        pack = self._load_packer()
        note = pack.frozen_services_note("1.0.6.36")
        self.assertTrue(note.startswith("note: services block is frozen v1"))
        for key in pack.SERVICES_OPTIONAL_KEYS:
            self.assertIn(key, note)
        self.assertIn("sa02m-dns-ensure.service", note)
        self.assertEqual(pack.frozen_services_note(pack.SERVICES_OPTIONAL_SINCE), "")
        # The production call prints exactly what the module's MIN_UPDATER earns.
        self.assertEqual(bool(pack.frozen_services_note()), not pack.emit_optional_service_keys(pack.MIN_UPDATER))

    def test_runner_reports_the_deployed_version_not_a_stamp(self) -> None:
        # D2: the advertised min_updater (packer MIN_UPDATER) and the version a
        # board reports (runner UPDATER_VERSION) must not be able to diverge
        # silently. Before 1.0.6.39 the runner stamped 1.0.5.66, so any
        # MIN_UPDATER bump would have E_COMPAT-rejected every board. Runs the
        # SHIPPED runner's `version` subcommand: it must report the VERSION
        # file it was deployed with (CRLF tolerated), the documented floor when
        # the file is absent or unparseable, and the explicit env override.
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH — the runner's version derivation was NOT verified")
        runner = (_REPO / "etc" / "sa02m-update-runner.sh").as_posix()
        pack = self._load_packer()

        def report(version_file: Path, **extra: str) -> str:
            env = {k: v for k, v in os.environ.items() if k != "SA02M_UPDATER_VERSION"}
            env["SA02M_WEB_VERSION_FILE"] = version_file.as_posix()
            # The stamp lives at the DEFAULT $STATEDIR/runner.version — point the
            # state dir at the fixture dir so the default derivation is what runs.
            env["SA02M_UPDATE_STATEDIR"] = version_file.parent.as_posix()
            env.update(extra)
            r = subprocess.run(
                [bash, runner, "version"], env=env, capture_output=True, text=True, timeout=60
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()

        with tempfile.TemporaryDirectory() as d:
            vf = Path(d) / "VERSION"
            vf.write_bytes(b"# comment\r\n9.8.7.6\r\n")
            self.assertEqual(report(vf), "9.8.7.6")
            self.assertEqual(report(Path(d) / "missing"), "1.0.5.66")
            vf.write_bytes(b"garbage\n")
            self.assertEqual(report(vf), "1.0.5.66")
            self.assertEqual(report(vf, SA02M_UPDATER_VERSION="7.7.7.7"), "7.7.7.7")
            # 1.0.6.40 (item 6): every runner install site stamps the release
            # that installed THIS runner into $STATEDIR/runner.version. A
            # www-only delivery refreshes VERSION without the runner, so the
            # stamp — not the newer VERSION — is what the board reports; an
            # absent (pre-1.0.6.40 board) or unparseable stamp falls through
            # to VERSION, and the env override still wins over both.
            stamp = Path(d) / "runner.version"
            vf.write_bytes(b"9.8.7.6\n")
            stamp.write_bytes(b"1.2.3.4\r\n")
            self.assertEqual(report(vf), "1.2.3.4")
            stamp.write_bytes(b"not a version\n")
            self.assertEqual(report(vf), "9.8.7.6")
            stamp.write_bytes(b"1.2.3.4\n")
            self.assertEqual(report(Path(d) / "missing"), "1.2.3.4")
            self.assertEqual(report(vf, SA02M_UPDATER_VERSION="7.7.7.7"), "7.7.7.7")
        # A pack built from this tree must stay applicable by the runner of the
        # release it updates: MIN_UPDATER never exceeds what this tree reports.
        repo_version = ""
        for line in (_REPO / "www" / "network_config" / "VERSION").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                repo_version = line
                break
        self.assertTrue(repo_version)
        self.assertLessEqual(pack.semver_key(pack.MIN_UPDATER), pack.semver_key(repo_version))

    def test_inspect_preflight_derives_the_same_version_as_the_runner(self) -> None:
        # Review 1.0.6.39 F3: the D2 derivation lives in TWO shipped homes —
        # the runner (apply-time compat gate) and sa02m-update-inspect.sh (the
        # preflight the UI reads before apply). A bump of one fallback would
        # leave the panel saying "compatible" where the runner refuses, or the
        # reverse. Drives the SHIPPED inspect script against the runner's
        # fixtures and pins the two fallback literals to each other.
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not on PATH — the inspect preflight's version derivation was NOT verified")
        runner = _REPO / "etc" / "sa02m-update-runner.sh"
        inspect = _REPO / "etc" / "sa02m-update-inspect.sh"

        def fallback_literal(script: Path) -> str:
            hits = [
                line.split("=", 1)[1].strip()
                for line in script.read_text(encoding="utf-8").splitlines()
                if line.startswith("UPDATER_VERSION_FALLBACK=")
            ]
            self.assertEqual(len(hits), 1, "%s: expected exactly one fallback assignment" % script.name)
            return hits[0]

        self.assertEqual(fallback_literal(inspect), fallback_literal(runner))
        self.assertEqual(fallback_literal(runner), "1.0.5.66")

        def preflight(version_file: Path, package: Path, **extra: str) -> dict:
            env = {k: v for k, v in os.environ.items() if k != "SA02M_UPDATER_VERSION"}
            env["SA02M_WEB_VERSION_FILE"] = version_file.as_posix()
            env["SA02M_UPDATE_STATEDIR"] = version_file.parent.as_posix()
            # No validator module => the script's bootstrap branch, which still
            # derives UPDATER_VERSION first and prints it in its JSON.
            env["SA02M_UPDATE_VALIDATE_PY"] = (package.parent / "no-validator-here.py").as_posix()
            env["PYTHONIOENCODING"] = "utf-8"
            env.update(extra)
            r = subprocess.run(
                [bash, inspect.as_posix(), package.as_posix()],
                env=env, capture_output=True, text=True, timeout=60,
            )
            out = json.loads(r.stdout.replace("\r", "").strip())
            # A tiny file is refused at the trailer, i.e. AFTER the derivation —
            # proves the value came through the whole preflight, not an early exit.
            self.assertEqual(out["error_code"], "E_TRAILER", r.stderr)
            return out

        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "package.sa02m"
            pkg.write_bytes(b"not a package")
            vf = Path(d) / "VERSION"
            vf.write_bytes(b"# comment\r\n9.8.7.6\r\n")
            out = preflight(vf, pkg)
            self.assertEqual(out["updater_version"], "9.8.7.6")
            self.assertEqual(out["installed_version"], "9.8.7.6")
            out = preflight(Path(d) / "missing", pkg)
            self.assertEqual(out["updater_version"], "1.0.5.66")
            self.assertIsNone(out["installed_version"])
            vf.write_bytes(b"garbage\n")
            self.assertEqual(preflight(vf, pkg)["updater_version"], "1.0.5.66")
            self.assertEqual(preflight(vf, pkg, SA02M_UPDATER_VERSION="7.7.7.7")["updater_version"], "7.7.7.7")
            # The stamp mirror (1.0.6.40, item 6): the preflight reports the
            # stamped runner release, not the www-only-refreshed VERSION — while
            # installed_version stays the VERSION file (the panel shows both).
            stamp = Path(d) / "runner.version"
            vf.write_bytes(b"9.8.7.6\n")
            stamp.write_bytes(b"1.2.3.4\r\n")
            out = preflight(vf, pkg)
            self.assertEqual(out["updater_version"], "1.2.3.4")
            self.assertEqual(out["installed_version"], "9.8.7.6")
            stamp.write_bytes(b"not a version\n")
            self.assertEqual(preflight(vf, pkg)["updater_version"], "9.8.7.6")
            stamp.write_bytes(b"1.2.3.4\n")
            self.assertEqual(preflight(Path(d) / "missing", pkg)["updater_version"], "1.2.3.4")
            self.assertEqual(preflight(vf, pkg, SA02M_UPDATER_VERSION="7.7.7.7")["updater_version"], "7.7.7.7")

    def test_services_optional_keys_accepted(self) -> None:
        # The 1.0.6.37 packer manifest: enable (emitted since 1.0.5.69 — the
        # validator rejected it until the required/optional split) plus the
        # conditional-restart sets for the /opt code freshness fix.
        m = _sample_manifest()
        m["services"]["enable"] = ["sa02m-devices-api.service"]
        m["services"]["restart_if_active"] = [
            "sa02m-alice-client",
            "sa02m-alice-config",
            "sa02m-cloud-control",
        ]
        m["services"]["restart_if_changed"] = {"sa02m-modbus-mqtt": "/opt/sa02m-modbus-mqtt/"}
        vp.validate_manifest_object(m)  # must not raise

    def test_services_unknown_key_still_rejected(self) -> None:
        m = _sample_manifest()
        m["services"]["bogus"] = []
        with self.assertRaises(PackageError) as cm:
            vp.validate_manifest_object(m)
        self.assertEqual(cm.exception.code, "E_MANIFEST")

    def test_services_restart_if_active_must_be_string_array(self) -> None:
        for bad in ("sa02m-rules", ["sa02m-rules", 5], [""]):
            m = _sample_manifest()
            m["services"]["restart_if_active"] = bad
            with self.assertRaises(PackageError) as cm:
                vp.validate_manifest_object(m)
            self.assertEqual(cm.exception.code, "E_MANIFEST")

    def test_services_restart_if_changed_shape(self) -> None:
        for bad in (
            ["not-a-dict"],
            {"sa02m-modbus-mqtt": "opt/relative/"},
            {"sa02m-modbus-mqtt": "/opt/sa02m-modbus-mqtt"},  # no trailing slash
            {"sa02m-modbus-mqtt": "/opt/../etc/"},
            {"": "/opt/x/"},
        ):
            m = _sample_manifest()
            m["services"]["restart_if_changed"] = bad
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
