# -*- coding: utf-8 -*-
"""Validate + extract a MasterSCADA4D project-export .zip for MPLC4 deploy.

The archive layout a real IDE export carries (confirmed on bench, 1.0.5.69):

    cfg/config.bin      (encrypted/compressed RT config)
    cfg/ProjInfo.json   (ProjectId / ProjectName / VersionEditsInfo.IDEVersion)
    cfg/VMInfo.json
    cfg/_files.xml      (<File Name="cfg/x" Hash="AA-BB-.." /> ledger)

Security model — the input is untrusted LAN multipart. Controls, in order:
  1. body size cap (anti-DoS, real export is ~14 KB)
  2. entry-count cap, then per-entry + total uncompressed caps (anti zip-bomb)
  3. zip-slip / traversal rejection: no `..`, no absolute path, no backslash,
     no symlink member — every member name must be one of the four allow-listed
     `cfg/…` names, nothing else
  4. exact-member-set check (a real MasterSCADA export, not an arbitrary zip)
  5. ProjInfo.json parses as JSON and carries ProjectId + ProjectName
  6. OPTIONAL _files.xml hash verification — see verify_file_hashes(): DEFAULT
     OFF because the IDE's ledger hash is NOT a plain SHA-256 of the packaged
     bytes (empirically mismatches the real export — the IDE hashes pre-export
     content), so enabling reject-on-mismatch would reject legitimate uploads.
     Kept implemented behind an explicit flag for on-device confirmation.

extract_project() writes ONLY fixed basenames into a destination the CALLER
pins — a zip entry name never reaches the filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Dict, List

try:
    from . import ProjectZipError
except ImportError:  # pragma: no cover - direct-path import in tests
    from __init__ import ProjectZipError  # type: ignore

# member-in-zip → FIXED output basename (never the zip's own path). The map is
# the closed allow-list; any file member outside it is rejected.
ALLOWED: Dict[str, str] = {
    "cfg/config.bin": "config.bin",
    "cfg/ProjInfo.json": "ProjInfo.json",
    "cfg/VMInfo.json": "VMInfo.json",
    "cfg/_files.xml": "_files.xml",
}
REQUIRED = frozenset(ALLOWED)

MAX_BODY = 5 * 1024 * 1024      # multipart body / archive-on-disk cap
MAX_ENTRY = 8 * 1024 * 1024     # per-file uncompressed cap
MAX_TOTAL = 24 * 1024 * 1024    # total uncompressed cap
MAX_ENTRIES = 32                # entry-count cap (checked before any name work)

_BOUNDARY_RE = re.compile(rb'boundary=(?:"([^"]+)"|([^;]+))', re.IGNORECASE)
_S_IFLNK = 0o120000  # symlink bits in a zip entry's unix mode (external_attr >> 16)


def _boundary(content_type: str) -> bytes:
    raw = content_type.encode("latin-1", "replace")
    m = _BOUNDARY_RE.search(raw)
    if not m:
        raise ProjectZipError("E_UPLOAD", "multipart boundary missing")
    b = (m.group(1) or m.group(2)).strip()
    if not b or len(b) > 200:
        raise ProjectZipError("E_UPLOAD", "multipart boundary invalid")
    return b


def parse_multipart_file(body: bytes, content_type: str, field: str = "file") -> bytes:
    """Return the raw bytes of multipart form field `field` from `body`.

    Hand-rolled and binary-safe: the stdlib `email`/`cgi` parsers normalise line
    endings and corrupt binary payloads (a zip), so we locate the part by its
    boundary and slice the body verbatim — the same reasoning as the offline
    updater's receiver. Fails closed on any structural problem.
    """
    if "multipart/form-data" not in content_type.lower():
        raise ProjectZipError("E_UPLOAD", "Content-Type must be multipart/form-data")
    dash = b"--" + _boundary(content_type)
    idx = body.find(dash)
    if idx < 0:
        raise ProjectZipError("E_UPLOAD", "multipart start boundary not found")
    pos = idx + len(dash)
    guard = 0
    while guard < MAX_ENTRIES + 1:
        guard += 1
        if body[pos:pos + 2] == b"--":
            break  # closing boundary
        if body[pos:pos + 2] == b"\r\n":
            pos += 2
        hend = body.find(b"\r\n\r\n", pos)
        if hend < 0:
            raise ProjectZipError("E_UPLOAD", "malformed multipart headers")
        headers = body[pos:hend].decode("latin-1", "replace")
        body_start = hend + 4
        nidx = body.find(b"\r\n" + dash, body_start)
        if nidx < 0:
            raise ProjectZipError("E_UPLOAD", "unterminated multipart part")
        part = body[body_start:nidx]
        is_form = "form-data" in headers.lower()
        has_name = re.search(r'name="?%s"?(?:[;"\s]|$)' % re.escape(field), headers, re.I)
        if is_form and has_name:
            if not part:
                raise ProjectZipError("E_UPLOAD", "empty upload")
            return part
        pos = nidx + 2 + len(dash)
    raise ProjectZipError("E_UPLOAD", "multipart field %r not found" % field)


def _reject_name(name: str) -> None:
    # zip-slip / traversal guards applied to EVERY member name before use.
    if not name or name != name.strip():
        raise ProjectZipError("E_TRAVERSAL", "empty or padded member name")
    if "\\" in name or name.startswith("/") or name.startswith("~"):
        raise ProjectZipError("E_TRAVERSAL", "absolute or backslash member: %r" % name)
    if ":" in name:
        raise ProjectZipError("E_TRAVERSAL", "drive-like member: %r" % name)
    parts = name.split("/")
    if ".." in parts or "." in parts:
        raise ProjectZipError("E_TRAVERSAL", "traversal member: %r" % name)


def validate_zip(path, *, verify_hashes: bool = False) -> Dict[str, object]:
    """Validate the archive at `path`; return project meta on success.

    Raises ProjectZipError (fail-closed) on any structural, size, traversal,
    membership, or ProjInfo problem. Nothing is written. `verify_hashes` gates
    the _files.xml ledger check — see the module docstring for why it defaults
    OFF (the IDE ledger hash is not a plain SHA-256 of the packaged bytes).
    """
    p = Path(path)
    if not p.is_file():
        raise ProjectZipError("E_ZIP", "archive not found")
    if p.stat().st_size > MAX_BODY:
        raise ProjectZipError("E_SIZE", "archive exceeds %d bytes" % MAX_BODY)
    try:
        zf = zipfile.ZipFile(str(p))
    except zipfile.BadZipFile:
        raise ProjectZipError("E_ZIP", "not a valid zip archive")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ProjectZipError("E_ZIP", "too many entries (%d)" % len(infos))
        file_members: List[str] = []
        total = 0
        for zi in infos:
            name = zi.filename
            _reject_name(name)
            mode = (zi.external_attr >> 16) & 0o170000
            if mode == _S_IFLNK:
                raise ProjectZipError("E_TRAVERSAL", "symlink member: %r" % name)
            if name.endswith("/"):
                # A directory entry: tolerated only as the cfg/ container.
                if name != "cfg/":
                    raise ProjectZipError("E_MEMBERS", "unexpected directory: %r" % name)
                continue
            if name not in ALLOWED:
                raise ProjectZipError("E_MEMBERS", "unexpected member: %r" % name)
            if zi.file_size > MAX_ENTRY:
                raise ProjectZipError("E_SIZE", "member %r exceeds cap" % name)
            total += zi.file_size
            if total > MAX_TOTAL:
                raise ProjectZipError("E_SIZE", "uncompressed total exceeds cap")
            file_members.append(name)
        missing = REQUIRED - set(file_members)
        if missing:
            raise ProjectZipError(
                "E_MEMBERS", "not a MasterSCADA export; missing %s" % sorted(missing)
            )
        # ProjInfo.json must be well-formed and identify the project.
        try:
            proj = json.loads(zf.read("cfg/ProjInfo.json").decode("utf-8", "replace"))
        except Exception:
            raise ProjectZipError("E_PROJINFO", "ProjInfo.json is not valid JSON")
        if not isinstance(proj, dict):
            raise ProjectZipError("E_PROJINFO", "ProjInfo.json is not an object")
        name = proj.get("ProjectName")
        pid = proj.get("ProjectId")
        if not pid or not isinstance(pid, str):
            raise ProjectZipError("E_PROJINFO", "ProjInfo.json has no ProjectId")
        if not name or not isinstance(name, str):
            raise ProjectZipError("E_PROJINFO", "ProjInfo.json has no ProjectName")
        ide_version = ""
        vi = proj.get("VersionEditsInfo")
        if isinstance(vi, dict):
            ide_version = str(vi.get("IDEVersion") or "")
        if verify_hashes:
            verify_file_hashes(zf)  # raises E_HASH on a definite mismatch
    return {"name": name, "id": pid, "ide_version": ide_version}


def verify_file_hashes(zf: zipfile.ZipFile) -> None:
    """Best-effort _files.xml ledger check: reject on a DEFINITE SHA-256 mismatch.

    Off by default (see module docstring). When enabled: for each ledger entry
    whose Name maps to an allow-listed member, recompute SHA-256 of the packaged
    bytes and reject on mismatch. An unparseable ledger or an entry without a
    32-byte hex hash is NOT treated as tamper (the IDE's algorithm is unconfirmed
    on-device) — it is skipped, never a false reject.
    """
    try:
        xml = zf.read("cfg/_files.xml").decode("utf-8", "replace")
    except Exception:
        return
    for m in re.finditer(r'<File\s+Name="([^"]+)"\s+Hash="([0-9A-Fa-f-]+)"', xml):
        member, ledger = m.group(1), m.group(2).replace("-", "").lower()
        if len(ledger) != 64:  # not a 32-byte hex digest — unknown algo, skip
            continue
        if member not in ALLOWED:
            continue
        try:
            got = hashlib.sha256(zf.read(member)).hexdigest()
        except KeyError:
            continue
        if got != ledger:
            raise ProjectZipError("E_HASH", "hash mismatch for %r" % member)


def extract_project(path, dest_dir) -> List[str]:
    """Extract ONLY the allow-listed members to FIXED basenames under `dest_dir`.

    `dest_dir` MUST be an absolute constant the caller pins — a zip entry name
    never contributes to the output path (zip-slip closed). validate_zip() must
    have passed first; this re-applies the per-member size cap defensively.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    with zipfile.ZipFile(str(path)) as zf:
        for member, out_name in ALLOWED.items():
            try:
                data = zf.read(member)
            except KeyError:
                raise ProjectZipError("E_MEMBERS", "missing member %r" % member)
            if len(data) > MAX_ENTRY:
                raise ProjectZipError("E_SIZE", "member %r exceeds cap" % member)
            out = dest / out_name  # fixed name — no zip-derived path component
            with open(str(out), "wb") as f:
                f.write(data)
            written.append(out_name)
    return sorted(written)
