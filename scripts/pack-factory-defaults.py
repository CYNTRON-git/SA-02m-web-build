#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack signed SA-02m factory-defaults bundle (*.sa02m-defaults v1).

Config-only defaults — never rootfs/eMMC. Layout mirrors .sa02m:

  [0 .. tar_size)           ustar with manifest.json, manifest.sig,
                            payload.tar.gz, payload.sha256
  [tar_size .. tar_size+20) FOOTER = b\"SA02M_FACTORY_END_V1\" + b\"\\0\\0\"

Signature domain: b\"SA02M-FACTORY-V1\\0\" + canonical_json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "etc" / "sa02m-factory-defaults" / "templates"
LISTS = ROOT / "etc" / "sa02m-factory-defaults" / "lists"
DEFAULT_OUT_DIR = ROOT / "etc" / "sa02m-factory-defaults"
VERSION_FILE = ROOT / "www" / "network_config" / "VERSION"

# Magic is 20 bytes; + \0\0 => 22. Plan text said "+20" by miscount — wire
# format uses len(FOOTER), matching opt/sa02m-update validate_package.py.
FOOTER_MAGIC = b"SA02M_FACTORY_END_V1"
FOOTER = FOOTER_MAGIC + b"\0\0"
FOOTER_LEN = len(FOOTER)
assert FOOTER_LEN == 22
assert FOOTER[:-2] == FOOTER_MAGIC

SIG_DOMAIN = b"SA02M-FACTORY-V1\0"
DEFAULT_KEY_ID = "release-2026-08"

# Payload paths → device destinations (relative to templates/)
APPLY_MAP: Tuple[Tuple[str, str, str, str], ...] = (
    ("etc/nginx/.htpasswd", "/etc/nginx/.htpasswd", "0600", "root:root"),
    ("etc/sa02m_web.env", "/etc/sa02m_web.env", "0640", "root:www-data"),
    ("etc/network/interfaces.d/eth0.conf", "/etc/network/interfaces.d/eth0.conf", "0644", "root:root"),
    ("etc/network/interfaces.d/eth1.conf", "/etc/network/interfaces.d/eth1.conf", "0644", "root:root"),
    ("etc/sa02m_modem.conf", "/etc/sa02m_modem.conf", "0644", "root:root"),
    ("etc/sa02m_network.conf", "/etc/sa02m_network.conf", "0644", "root:root"),
    ("etc/sa02m-modbus-mqtt.yaml", "/etc/sa02m-modbus-mqtt.yaml", "0660", "root:www-data"),
    ("etc/sa02m-gateway.yaml", "/etc/sa02m-gateway.yaml", "0660", "root:www-data"),
    ("etc/sa02m-alice-client.conf", "/etc/sa02m-alice-client.conf", "0640", "root:www-data"),
    ("etc/sa02m-alice-devices.conf", "/etc/sa02m-alice-devices.conf", "0640", "root:www-data"),
    (
        "etc/sa02m-alice/sa02m-alice-client.conf",
        "/etc/sa02m-alice/sa02m-alice-client.conf",
        "0640",
        "root:www-data",
    ),
    (
        "etc/sa02m-alice/sa02m-alice-devices.conf",
        "/etc/sa02m-alice/sa02m-alice-devices.conf",
        "0640",
        "root:www-data",
    ),
)


def _read_list(path: Path) -> List[str]:
    out: List[str] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if re.fullmatch(r"[0-9]+(\.[0-9]+){1,3}", line):
            return line
    raise SystemExit(f"no semver in {VERSION_FILE}")


def git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if re.fullmatch(r"[0-9a-f]{40}", out):
            return out
    except (OSError, subprocess.CalledProcessError):
        pass
    return "0" * 40


def canonical_json(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _find_openssl() -> Optional[str]:
    which = shutil.which("openssl")
    if which:
        return which
    candidate = Path(r"C:\Program Files\Git\usr\bin\openssl.exe")
    if candidate.is_file():
        return str(candidate)
    return None


def sign_ed25519(priv_key: Path, message: bytes) -> bytes:
    """Return raw 64-byte Ed25519 signature."""
    openssl = _find_openssl()
    if openssl:
        with tempfile.NamedTemporaryFile(delete=False) as msgf:
            msgf.write(message)
            msg_path = msgf.name
        try:
            raw = subprocess.check_output(
                [
                    openssl,
                    "pkeyutl",
                    "-sign",
                    "-inkey",
                    str(priv_key),
                    "-rawin",
                    "-in",
                    msg_path,
                ]
            )
            if len(raw) == 64:
                return raw
        finally:
            try:
                os.unlink(msg_path)
            except OSError:
                pass
    # cryptography fallback
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = priv_key.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"not an Ed25519 private key: {priv_key}")
    return key.sign(message)


def build_payload_tar_gz(staging: Path) -> Tuple[bytes, int]:
    """Copy templates into staging and return (gz_bytes, uncompressed_size)."""
    if not TEMPLATES.is_dir():
        raise SystemExit(f"missing templates: {TEMPLATES}")
    payload_root = staging / "payload"
    if payload_root.exists():
        shutil.rmtree(payload_root)
    shutil.copytree(TEMPLATES, payload_root)

    # Ensure user overlays dir exists empty (only .keep)
    user_dir = payload_root / "etc" / "sa02m-device-templates" / "user"
    user_dir.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO()
    uncompressed = 0
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.USTAR_FORMAT, compresslevel=9) as tf:
        for path in sorted(payload_root.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(payload_root).as_posix()
            info = tarfile.TarInfo(name=rel)
            data = path.read_bytes()
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            if path.name == ".htpasswd" or path.name.endswith(".env"):
                info.mode = 0o600
            tf.addfile(info, io.BytesIO(data))
            uncompressed += len(data)
    return buf.getvalue(), uncompressed


def build_manifest(
    *,
    version: str,
    commit: str,
    key_id: str,
    payload_gz: bytes,
    uncompressed: int,
    wipe: Sequence[str],
    preserve: Sequence[str],
) -> Dict[str, Any]:
    apply = []
    for src, dst, mode, owner in APPLY_MAP:
        apply.append({"src": src, "dst": dst, "mode": mode, "owner": owner})
    return {
        "schema_version": 1,
        "product": "SA-02m",
        "model": "A40i",
        "arch": "armv7l",
        "kind": "factory-defaults",
        "version": version,
        "repo_commit": commit,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "signing_key_id": key_id,
        "payload": {
            "size": len(payload_gz),
            "sha256": hashlib.sha256(payload_gz).hexdigest(),
            "uncompressed_size_max": max(uncompressed * 2, 1_048_576),
        },
        "wipe": list(wipe),
        "preserve": list(preserve),
        "apply": apply,
        "admin_default": {"user": "admin", "password_hint": "cyntron"},
    }


def write_outer(path: Path, members: Dict[str, bytes]) -> None:
    required = {"manifest.json", "manifest.sig", "payload.tar.gz", "payload.sha256"}
    if set(members) != required:
        raise SystemExit(f"outer members must be exactly {sorted(required)}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=tarfile.USTAR_FORMAT) as tf:
        for name in ("manifest.json", "manifest.sig", "payload.tar.gz", "payload.sha256"):
            data = members[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    tar_bytes = buf.getvalue()
    if len(tar_bytes) % 512 != 0:
        raise SystemExit(f"tar_size {len(tar_bytes)} not multiple of 512")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(tar_bytes)
        f.write(FOOTER)
        f.flush()
        os.fsync(f.fileno())


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default=None, help="defaults to www/network_config/VERSION")
    ap.add_argument("--key-id", default=DEFAULT_KEY_ID)
    ap.add_argument(
        "--signing-key",
        type=Path,
        default=None,
        help="Ed25519 private PEM (default private/sa02m-update-keys/<key-id>.ed25519)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="directory for <version>/ bundle + .sa02m-defaults file",
    )
    ap.add_argument("--unsigned", action="store_true", help="write empty manifest.sig (CI only)")
    args = ap.parse_args(argv)

    version = args.version or read_version()
    commit = git_commit()
    wipe = _read_list(LISTS / "wipe.list")
    preserve = _read_list(LISTS / "preserve.list")
    if not wipe:
        raise SystemExit("wipe.list empty")

    with tempfile.TemporaryDirectory(prefix="sa02m-factory-pack-") as tmp:
        staging = Path(tmp)
        payload_gz, uncompressed = build_payload_tar_gz(staging)
        manifest = build_manifest(
            version=version,
            commit=commit,
            key_id=args.key_id,
            payload_gz=payload_gz,
            uncompressed=uncompressed,
            wipe=wipe,
            preserve=preserve,
        )
        canon = canonical_json(manifest)
        message = SIG_DOMAIN + canon

        if args.unsigned:
            sig_b64 = base64.b64encode(b"\0" * 64).decode("ascii") + "\n"
        else:
            key_path = args.signing_key or (
                ROOT / "private" / "sa02m-update-keys" / f"{args.key_id}.ed25519"
            )
            if not key_path.is_file():
                raise SystemExit(f"signing key missing: {key_path} (or pass --unsigned)")
            sig = sign_ed25519(key_path, message)
            sig_b64 = base64.b64encode(sig).decode("ascii") + "\n"

        members = {
            "manifest.json": canon + b"\n",
            "manifest.sig": sig_b64.encode("ascii"),
            "payload.tar.gz": payload_gz,
            "payload.sha256": (hashlib.sha256(payload_gz).hexdigest() + "\n").encode("ascii"),
        }

        ver_dir = args.out_dir / version
        ver_dir.mkdir(parents=True, exist_ok=True)
        # Also materialize templates + lists next to the shipped tree for installers.
        shutil.copytree(TEMPLATES, ver_dir / "templates", dirs_exist_ok=True)
        (ver_dir / "lists").mkdir(exist_ok=True)
        shutil.copy2(LISTS / "wipe.list", ver_dir / "lists" / "wipe.list")
        shutil.copy2(LISTS / "preserve.list", ver_dir / "lists" / "preserve.list")
        (ver_dir / "manifest.json").write_bytes(members["manifest.json"])

        out_file = args.out_dir / f"SA-02m-factory-defaults-{version}.sa02m-defaults"
        write_outer(out_file, members)
        sidecar = out_file.with_suffix(out_file.suffix + ".sha256")
        digest = hashlib.sha256(out_file.read_bytes()).hexdigest()
        sidecar.write_text(f"{digest}  {out_file.name}\n", encoding="ascii")

        print(f"wrote {out_file}")
        print(f"wrote {sidecar}")
        print(f"version={version} payload_sha256={manifest['payload']['sha256']}")
        print(f"wipe={len(wipe)} preserve={len(preserve)} apply={len(manifest['apply'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
