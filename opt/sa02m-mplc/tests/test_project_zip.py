# -*- coding: utf-8 -*-
"""Unit tests for the MPLC4 project-deploy zip validator/extractor.

Validating test for docs/contracts/mplc-project-deploy.md — the security net for
the upload surface: zip-slip/traversal rejection, the closed 4-member allow-list,
size/entry caps (anti zip-bomb), ProjInfo.json sanity, and the fixed-basename
extraction that a hostile entry name can never redirect. Stdlib only.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import ProjectZipError  # noqa: E402
from lib.project_zip import (  # noqa: E402
    ALLOWED,
    MAX_BODY,
    extract_project,
    parse_multipart_file,
    validate_zip,
)

GOOD_PROJINFO = json.dumps(
    {
        "ProjectId": "d34f9bb8-ecb1-4df6-a57d-9d6f63a3abb2",
        "ProjectName": "Тест",
        "VersionEditsInfo": {"IDEVersion": "1.3.10.34027"},
    }
).encode("utf-8")

GOOD_MEMBERS = {
    "cfg/config.bin": b"\x00\x01binary-config",
    "cfg/ProjInfo.json": GOOD_PROJINFO,
    "cfg/VMInfo.json": b"{}",
    "cfg/_files.xml": b'<?xml version="1.0"?><Files></Files>',
}


def _zip_bytes(members, *, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write_zip(members, **kw):
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(_zip_bytes(members, **kw))
    return path


class ValidateGoodZip(unittest.TestCase):
    def test_accepts_real_shaped_export(self):
        path = _write_zip(GOOD_MEMBERS)
        self.addCleanup(os.unlink, path)
        meta = validate_zip(path)
        self.assertEqual(meta["name"], "Тест")
        self.assertEqual(meta["id"], "d34f9bb8-ecb1-4df6-a57d-9d6f63a3abb2")
        self.assertEqual(meta["ide_version"], "1.3.10.34027")

    def test_tolerates_cfg_directory_entry(self):
        members = {"cfg/": b""}
        members.update(GOOD_MEMBERS)
        path = _write_zip(members)
        self.addCleanup(os.unlink, path)
        self.assertEqual(validate_zip(path)["name"], "Тест")


class RejectsHostileZip(unittest.TestCase):
    def _reject(self, members, code):
        path = _write_zip(members)
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ProjectZipError) as ctx:
            validate_zip(path)
        self.assertEqual(ctx.exception.code, code)

    def test_zip_slip_parent_traversal(self):
        m = dict(GOOD_MEMBERS)
        m["../../../../etc/cron.d/evil"] = b"* * * * * root sh -c evil\n"
        self._reject(m, "E_TRAVERSAL")

    def test_absolute_path_member(self):
        m = dict(GOOD_MEMBERS)
        m["/etc/passwd"] = b"root:x:0:0"
        self._reject(m, "E_TRAVERSAL")

    def test_unexpected_extra_member(self):
        m = dict(GOOD_MEMBERS)
        m["cfg/backdoor.sh"] = b"#!/bin/sh\n"
        self._reject(m, "E_MEMBERS")

    def test_missing_required_member(self):
        m = dict(GOOD_MEMBERS)
        del m["cfg/config.bin"]
        self._reject(m, "E_MEMBERS")

    def test_projinfo_not_json(self):
        m = dict(GOOD_MEMBERS)
        m["cfg/ProjInfo.json"] = b"not json at all"
        self._reject(m, "E_PROJINFO")

    def test_projinfo_missing_projectid(self):
        m = dict(GOOD_MEMBERS)
        m["cfg/ProjInfo.json"] = json.dumps({"ProjectName": "x"}).encode()
        self._reject(m, "E_PROJINFO")

    def test_not_a_zip(self):
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "wb") as f:
            f.write(b"PK-not-really-a-zip")
        with self.assertRaises(ProjectZipError) as ctx:
            validate_zip(path)
        self.assertEqual(ctx.exception.code, "E_ZIP")

    def test_symlink_member_rejected(self):
        # A zip member carrying unix symlink mode bits pointing outside cfg/.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in GOOD_MEMBERS.items():
                if name == "cfg/config.bin":
                    zi = zipfile.ZipInfo(name)
                    zi.external_attr = (0o120777 << 16)  # S_IFLNK
                    zf.writestr(zi, "/etc/shadow")
                else:
                    zf.writestr(name, data)
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        with self.assertRaises(ProjectZipError) as ctx:
            validate_zip(path)
        self.assertEqual(ctx.exception.code, "E_TRAVERSAL")

    def test_zip_bomb_entry_size_capped(self):
        m = dict(GOOD_MEMBERS)
        m["cfg/config.bin"] = b"A" * (9 * 1024 * 1024)  # > MAX_ENTRY
        self._reject(m, "E_SIZE")


class HashVerification(unittest.TestCase):
    def test_hash_mismatch_rejected_when_enabled(self):
        m = dict(GOOD_MEMBERS)
        m["cfg/_files.xml"] = (
            b'<Files><File Name="cfg/config.bin" '
            b'Hash="00-00-00-00-00-00-00-00-00-00-00-00-00-00-00-00-'
            b'00-00-00-00-00-00-00-00-00-00-00-00-00-00-00-00" /></Files>'
        )
        path = _write_zip(m)
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ProjectZipError) as ctx:
            validate_zip(path, verify_hashes=True)
        self.assertEqual(ctx.exception.code, "E_HASH")

    def test_unknown_hash_algo_not_false_reject(self):
        # The real IDE ledger hash is NOT a plain SHA-256 of packaged bytes; a
        # 64-hex ledger that simply differs is a mismatch, but the default path
        # (verify off) must accept the export unconditionally.
        path = _write_zip(GOOD_MEMBERS)
        self.addCleanup(os.unlink, path)
        self.assertEqual(validate_zip(path)["name"], "Тест")  # verify off = accepted


class Extraction(unittest.TestCase):
    def test_extracts_only_fixed_basenames(self):
        path = _write_zip(GOOD_MEMBERS)
        self.addCleanup(os.unlink, path)
        dest = tempfile.mkdtemp()
        written = extract_project(path, dest)
        self.assertEqual(written, sorted(ALLOWED.values()))
        on_disk = sorted(os.listdir(dest))
        self.assertEqual(on_disk, sorted(ALLOWED.values()))
        with open(os.path.join(dest, "config.bin"), "rb") as f:
            self.assertEqual(f.read(), GOOD_MEMBERS["cfg/config.bin"])


class Multipart(unittest.TestCase):
    def _body(self, payload, field="file", filename="p.zip"):
        b = "----WebKitFormBoundaryABC123"
        parts = [
            ("--" + b).encode(),
            b'Content-Disposition: form-data; name="%s"; filename="%s"'
            % (field.encode(), filename.encode()),
            b"Content-Type: application/zip",
            b"",
            payload,
            ("--" + b + "--").encode(),
            b"",
        ]
        return b"\r\n".join(parts), "multipart/form-data; boundary=" + b

    def test_extracts_binary_payload_verbatim(self):
        payload = _zip_bytes(GOOD_MEMBERS)
        body, ctype = self._body(payload)
        got = parse_multipart_file(body, ctype)
        self.assertEqual(got, payload)

    def test_missing_field_rejected(self):
        body, ctype = self._body(b"x", field="other")
        with self.assertRaises(ProjectZipError) as ctx:
            parse_multipart_file(body, ctype, field="file")
        self.assertEqual(ctx.exception.code, "E_UPLOAD")

    def test_not_multipart_rejected(self):
        with self.assertRaises(ProjectZipError):
            parse_multipart_file(b"raw", "application/zip")

    def test_roundtrip_parse_then_validate(self):
        payload = _zip_bytes(GOOD_MEMBERS)
        body, ctype = self._body(payload)
        raw = parse_multipart_file(body, ctype)
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "wb") as f:
            f.write(raw)
        self.assertEqual(validate_zip(path)["id"].count("-"), 4)  # a UUID


if __name__ == "__main__":
    unittest.main()
