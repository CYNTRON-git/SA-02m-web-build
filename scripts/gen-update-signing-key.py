#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate Ed25519 keypair for SA-02m offline .sa02m package signing.

Writes:
  etc/sa02m-update/trusted-keys/<key-id>.pem   (public, committed)
  private/sa02m-update-keys/<key-id>.ed25519   (private, gitignored via private/)

Prefer OpenSSL when available; fall back to the cryptography package.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ID = "release-2026-08"


def _find_openssl() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
        Path(r"C:\Program Files\Git\mingw64\bin\openssl.exe"),
        Path("/usr/bin/openssl"),
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _write_with_openssl(openssl: str, priv: Path, pub: Path) -> None:
    subprocess.check_call([openssl, "genpkey", "-algorithm", "ED25519", "-out", str(priv)])
    subprocess.check_call([openssl, "pkey", "-in", str(priv), "-pubout", "-out", str(pub)])


def _export_pub_with_openssl(openssl: str, priv: Path, pub: Path) -> None:
    subprocess.check_call([openssl, "pkey", "-in", str(priv), "-pubout", "-out", str(pub)])


def _write_with_cryptography(priv: Path, pub: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    priv.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def _export_pub_with_cryptography(priv: Path, pub: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(priv.read_bytes(), password=None)
    pub.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key-id", default=DEFAULT_ID)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    priv_dir = ROOT / "private" / "sa02m-update-keys"
    pub_dir = ROOT / "etc" / "sa02m-update" / "trusted-keys"
    priv_dir.mkdir(parents=True, exist_ok=True)
    pub_dir.mkdir(parents=True, exist_ok=True)

    priv = priv_dir / f"{args.key_id}.ed25519"
    pub = pub_dir / f"{args.key_id}.pem"
    openssl = _find_openssl()

    if priv.exists() and not args.force:
        print(f"private key exists: {priv} (use --force)", file=sys.stderr)
        if not pub.exists():
            if openssl:
                _export_pub_with_openssl(openssl, priv, pub)
            else:
                _export_pub_with_cryptography(priv, pub)
            print(f"wrote public key: {pub}")
        return 0

    if openssl:
        _write_with_openssl(openssl, priv, pub)
    else:
        _write_with_cryptography(priv, pub)

    try:
        priv.chmod(0o600)
    except OSError:
        pass

    print(f"private: {priv}")
    print(f"public:  {pub}")
    print("Do NOT commit the private key (private/ is gitignored).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
