#!/bin/bash
# SA-02m package inspect — installed as /usr/local/libexec/sa02m-update-inspect.
# Read-only: validate incoming package.sa02m and print JSON for CGI/UI.
# shellcheck shell=bash
set -euo pipefail

STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
PACKAGE="${1:-$STATEDIR/incoming/package.sa02m}"
UPDATER_VERSION="${SA02M_UPDATER_VERSION:-1.0.5.66}"
VALIDATE_PY="${SA02M_UPDATE_VALIDATE_PY:-/opt/sa02m-update/lib/validate_package.py}"
VERSION_FILE="${SA02M_WEB_VERSION_FILE:-/var/www/network_config/VERSION}"

json_err() {
    local code=$1 msg=$2
    python3 -c 'import json,sys; print(json.dumps({"ok":False,"error_code":sys.argv[1],"error_message":sys.argv[2]},ensure_ascii=False))' \
        "$code" "$msg"
}

if [ ! -f "$PACKAGE" ]; then
    json_err E_TAR "package not found: $PACKAGE"
    exit 1
fi

installed=""
if [ -f "$VERSION_FILE" ]; then
    installed=$(tr -d '\r' <"$VERSION_FILE" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
fi

if [ -f "$VALIDATE_PY" ]; then
    PACKAGE="$PACKAGE" INSTALLED="${installed:-}" UPDATER="$UPDATER_VERSION" \
    VALIDATE_PY="$VALIDATE_PY" \
    python3 <<'PY'
import json, os, sys
from pathlib import Path

lib = Path(os.environ["VALIDATE_PY"]).resolve().parent
root = lib.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(lib))
try:
    from lib.validate_package import validate_package, read_package_sha256
    from lib import PackageError
except ImportError:
    from validate_package import validate_package, read_package_sha256  # type: ignore
    from __init__ import PackageError  # type: ignore

path = Path(os.environ["PACKAGE"])
installed = os.environ.get("INSTALLED") or ""
updater = os.environ.get("UPDATER") or ""
out = {
    "ok": False,
    "package_path": str(path),
    "package_size": path.stat().st_size,
    "package_sha256": None,
    "updater_version": updater,
    "installed_version": installed or None,
    "signature_ok": False,
    "compatible": False,
    "warnings": [],
    "error_code": None,
    "error_message": None,
    "inspect": {},
}
try:
    out["package_sha256"] = read_package_sha256(path)
    result = validate_package(
        path,
        trusted_keys_dir=Path("/etc/sa02m-update/trusted-keys"),
        installed_version=installed or None,
        runner_version=updater or None,
        check_compat=bool(installed),
    )
    out["ok"] = True
    out["signature_ok"] = bool(result.get("signature_ok"))
    out["compatible"] = True
    out["inspect"] = {
        "version": result.get("version"),
        "commit": result.get("repo_commit"),
        "signing_key_id": result.get("signing_key_id"),
        "signature_ok": bool(result.get("signature_ok")),
        "compatible": True,
        "warnings": [],
    }
except PackageError as exc:
    out["error_code"] = exc.code
    out["error_message"] = exc.message
    out["inspect"]["warnings"] = [exc.message]
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)
except Exception as exc:  # noqa: BLE001
    out["error_code"] = "E_INTERNAL"
    out["error_message"] = str(exc)
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)
print(json.dumps(out, ensure_ascii=False))
sys.exit(0)
PY
    exit $?
fi

# Bootstrap fallback when opt/sa02m-update is not installed yet (release N image
# before module deploy). Trailer + outer members only; signature not verified.
python3 - "$PACKAGE" "${installed:-}" "$UPDATER_VERSION" <<'PY'
import hashlib, json, os, sys, tarfile

path, installed, updater = sys.argv[1], sys.argv[2], sys.argv[3]
FOOTER = b"SA02M_UPDATE_END_V1" + b"\0\0"  # 21 bytes
FOOTER_LEN = len(FOOTER)
size = os.path.getsize(path)
out = {
    "ok": False,
    "package_path": path,
    "package_size": size,
    "package_sha256": None,
    "updater_version": updater,
    "installed_version": installed or None,
    "signature_ok": False,
    "compatible": False,
    "warnings": ["validator_module_missing"],
    "error_code": None,
    "error_message": None,
    "inspect": {},
}
if size < 1024 + FOOTER_LEN:
    out["error_code"] = "E_TRAILER"
    out["error_message"] = "package too small"
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)
with open(path, "rb") as f:
    f.seek(-FOOTER_LEN, 2)
    footer = f.read(FOOTER_LEN)
    f.seek(0)
    h = hashlib.sha256()
    while True:
        chunk = f.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
out["package_sha256"] = h.hexdigest()
if footer != FOOTER:
    out["error_code"] = "E_TRAILER"
    out["error_message"] = "bad trailer"
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)
tar_size = size - FOOTER_LEN
if tar_size % 512 != 0:
    out["error_code"] = "E_TAR"
    out["error_message"] = "tar_size not 512-aligned"
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)

required = {"manifest.json", "manifest.sig", "payload.tar.gz", "payload.sha256"}
try:
    with open(path, "rb") as f:
        blob = f.read(tar_size)
    with tarfile.open(fileobj=__import__("io").BytesIO(blob), mode="r:") as tf:
        names = []
        for m in tf.getmembers():
            if m.name in (".", "./"):
                continue
            names.append(m.name.split("/")[-1])
        have = set(names)
        if have != required:
            out["error_code"] = "E_TAR"
            out["error_message"] = "outer members mismatch: %s" % sorted(have)
            print(json.dumps(out, ensure_ascii=False))
            sys.exit(1)
        with tarfile.open(fileobj=__import__("io").BytesIO(blob), mode="r:") as tf2:
            mf = tf2.extractfile("manifest.json")
            if mf is None:
                raise RuntimeError("manifest.json missing")
            manifest = json.loads(mf.read().decode("utf-8"))
except Exception as e:
    out["error_code"] = "E_TAR"
    out["error_message"] = str(e)
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)

ver = str(manifest.get("version", ""))
out["inspect"] = {
    "version": ver,
    "commit": manifest.get("repo_commit"),
    "product": manifest.get("product"),
    "model": manifest.get("model"),
    "arch": manifest.get("arch"),
    "min_version": manifest.get("min_version"),
    "min_updater": manifest.get("min_updater"),
    "signing_key_id": manifest.get("signing_key_id"),
    "signature_ok": False,
    "compatible": False,
    "warnings": ["signature_not_verified_bootstrap"],
}
out["ok"] = True
out["warnings"] = out["inspect"]["warnings"]
print(json.dumps(out, ensure_ascii=False))
sys.exit(0)
PY
