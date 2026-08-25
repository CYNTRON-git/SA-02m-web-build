# -*- coding: utf-8 -*-
"""Validate .sa02m v1 containers: trailer, outer tar, manifest, Ed25519, payload, safe tar."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    from .semver import check_update_gates, is_valid as semver_valid
    from . import PackageError
except ImportError:  # pragma: no cover - flat PYTHONPATH=/opt/sa02m-update/lib
    from semver import check_update_gates, is_valid as semver_valid  # type: ignore
    from __init__ import PackageError  # type: ignore

# --- container layout (§2.1) -------------------------------------------------

# Magic is 19 bytes; + \0\0 => 21. Plan text said "+20" by miscount — wire format uses len(FOOTER).
FOOTER_MAGIC = b"SA02M_UPDATE_END_V1"
FOOTER = FOOTER_MAGIC + b"\0\0"
FOOTER_LEN = len(FOOTER)
assert FOOTER_LEN == 21

OUTER_REQUIRED_MEMBERS = frozenset(
    {"manifest.json", "manifest.sig", "payload.tar.gz", "payload.sha256"}
)

SIG_DOMAIN = b"SA02M-MANIFEST-V1\0"

# --- preserve / allowlists (§2.2, §2.4) --------------------------------------

PRESERVE_PATHS: Tuple[str, ...] = (
    "/etc/sa02m_web.env",
    "/etc/sa02m_*.conf",
    "/etc/network/interfaces.d/",
    "/etc/nginx/.htpasswd",
    "/etc/nginx/sites-enabled/000-sa02m-network_config",
    "/etc/sa02m-device-templates/",
    "/etc/sa02m-cloud/",
    "/var/lib/sa02m-flasher/",
    "/var/lib/sa02m-update/",
    "/etc/sa02m-alice-client.conf",
    "/etc/sa02m-alice-devices.conf",
    "/var/lib/sa02m-alice/",
)

_DST_PREFIX_RES = (
    re.compile(r"^/var/www/network_config/"),
    re.compile(r"^/usr/local/(sbin|lib|libexec)/"),
    re.compile(r"^/opt/sa02m-[a-z0-9-]+/"),
    # Closed-set MPLC plugins (firmware/mplc4 → /opt/mplc4/<name>.so only).
    re.compile(r"^/opt/mplc4/(mplc_cyntron|mplc_protocol_fast_modbus)\.so$"),
    re.compile(r"^/etc/systemd/system/sa02m-"),
    re.compile(r"^/etc/nginx/"),
    re.compile(r"^/etc/tmpfiles\.d/"),
    re.compile(r"^/etc/sudoers\.d/"),
    re.compile(r"^/etc/sa02m-update/trusted-keys/"),
)

_DELETE_RE = re.compile(
    r"^/(var/www/network_config|opt/sa02m-[a-z0-9-]+|usr/local/|etc/systemd/system/sa02m-)/"
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_MODE_RE = re.compile(r"^[0-7]{3,4}$")
_OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]*:[a-z_][a-z0-9_-]*$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_MANIFEST_TOP_KEYS = frozenset(
    {
        "schema_version",
        "product",
        "model",
        "arch",
        "version",
        "repo_commit",
        "built_at",
        "signing_key_id",
        "min_updater",
        "min_version",
        "payload",
        "preflight",
        "deploy",
        "services",
        "delete",
        "migrations",
    }
)

_PAYLOAD_KEYS = frozenset({"size", "sha256", "uncompressed_size_max"})
_PREFLIGHT_KEYS = frozenset({"commands", "free_bytes_min", "free_bytes_multiplier"})
_DEPLOY_KEYS = frozenset({"src", "dst", "mode", "owner"})
_SERVICES_KEYS = frozenset({"daemon_reload", "stop_before_apply", "restart", "health"})
_HEALTH_KEYS = frozenset({"http_url", "units_active", "version_file"})
_MIGRATION_KEYS = frozenset({"id", "min_from", "script", "sha256", "reversible"})

TAR_BOMB_RATIO_MAX = 20
DEFAULT_TRUSTED_KEYS_DIR = Path("/etc/sa02m-update/trusted-keys")
MAX_FALLBACK_KEYS = 8


def validate_container(path: Path) -> int:
    """Validate footer trailer and tar_size alignment; return tar_size.

    FOOTER is SA02M_UPDATE_END_V1 (19) + \\0\\0 (2) = 21 bytes.
    file_size = tar_size + FOOTER_LEN (not required to be % 512 == 0).
    tar_size % 512 == 0 only.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PackageError("E_TRAILER", f"cannot stat package: {exc}") from exc
    if size < 1024 + FOOTER_LEN:
        raise PackageError("E_TRAILER", f"package too small: {size}")
    with path.open("rb") as f:
        f.seek(-FOOTER_LEN, os.SEEK_END)
        footer = f.read(FOOTER_LEN)
    if len(footer) != FOOTER_LEN or footer[:-2] != FOOTER_MAGIC or footer[-2:] != b"\0\0":
        raise PackageError("E_TRAILER", "missing or invalid SA02M_UPDATE_END_V1 trailer")
    tar_size = size - FOOTER_LEN
    if tar_size % 512 != 0:
        raise PackageError("E_TAR", f"tar_size {tar_size} is not a multiple of 512")
    return tar_size


def canonical_manifest_bytes(obj: Dict[str, Any]) -> bytes:
    """Domain-separated signing payload uses this UTF-8 canonical JSON."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def signature_message(obj: Dict[str, Any]) -> bytes:
    return SIG_DOMAIN + canonical_manifest_bytes(obj)


def _type_err(path: str, expect: str, value: Any) -> PackageError:
    return PackageError("E_MANIFEST", f"{path}: expected {expect}, got {type(value).__name__}")


def _reject_unknown(path: str, obj: Dict[str, Any], allowed: Set[str]) -> None:
    extra = set(obj.keys()) - allowed
    if extra:
        raise PackageError("E_MANIFEST", f"{path}: unknown keys: {sorted(extra)}")


def _require_keys(path: str, obj: Dict[str, Any], required: Iterable[str]) -> None:
    missing = [k for k in required if k not in obj]
    if missing:
        raise PackageError("E_MANIFEST", f"{path}: missing keys: {missing}")


def dst_allowed(dst: str) -> bool:
    if not isinstance(dst, str) or not dst.startswith("/") or ".." in dst.split("/"):
        return False
    return any(r.match(dst) for r in _DST_PREFIX_RES)


def delete_allowed(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
        return False
    return bool(_DELETE_RE.match(path))


def path_is_preserved(dst: str) -> bool:
    """True if deploy dst conflicts with server-side PRESERVE_PATHS."""
    for rule in PRESERVE_PATHS:
        if rule.endswith("/"):
            if dst == rule.rstrip("/") or dst.startswith(rule):
                return True
        elif "*" in rule:
            # only /etc/sa02m_*.conf style
            prefix, _, suffix = rule.partition("*")
            if dst.startswith(prefix) and dst.endswith(suffix) and dst.count("/") == rule.count("/"):
                return True
        elif dst == rule:
            return True
    return False


def validate_manifest_object(obj: Any) -> Dict[str, Any]:
    """Hand-written manifest v1 validator (no jq / jsonschema). additionalProperties: false."""
    if not isinstance(obj, dict):
        raise PackageError("E_MANIFEST", "manifest root must be object")
    _reject_unknown("manifest", obj, _MANIFEST_TOP_KEYS)
    _require_keys(
        "manifest",
        obj,
        (
            "schema_version",
            "product",
            "model",
            "arch",
            "version",
            "repo_commit",
            "built_at",
            "signing_key_id",
            "min_updater",
            "min_version",
            "payload",
            "preflight",
            "deploy",
            "services",
            "delete",
            "migrations",
        ),
    )

    if obj["schema_version"] != 1:
        raise PackageError("E_MANIFEST", "schema_version must be 1")
    if obj["product"] != "SA-02m":
        raise PackageError("E_COMPAT", f"product {obj['product']!r} != SA-02m")
    if obj["model"] != "A40i":
        raise PackageError("E_COMPAT", f"model {obj['model']!r} != A40i")
    if obj["arch"] != "armv7l":
        raise PackageError("E_COMPAT", f"arch {obj['arch']!r} != armv7l")

    for key in ("version", "min_updater", "min_version"):
        if not isinstance(obj[key], str) or not semver_valid(obj[key]):
            raise PackageError("E_MANIFEST", f"{key} must be M.M.P[.S] semver")

    if not isinstance(obj["repo_commit"], str) or not _HEX40_RE.match(obj["repo_commit"]):
        raise PackageError("E_MANIFEST", "repo_commit must be 40 lowercase hex")
    if not isinstance(obj["built_at"], str) or not _ISO8601_RE.match(obj["built_at"]):
        raise PackageError("E_MANIFEST", "built_at must be YYYY-MM-DDTHH:MM:SSZ")
    if not isinstance(obj["signing_key_id"], str) or not _KEY_ID_RE.match(obj["signing_key_id"]):
        raise PackageError("E_MANIFEST", "signing_key_id invalid")

    payload = obj["payload"]
    if not isinstance(payload, dict):
        raise _type_err("payload", "object", payload)
    _reject_unknown("payload", payload, _PAYLOAD_KEYS)
    _require_keys("payload", payload, _PAYLOAD_KEYS)
    if not isinstance(payload["size"], int) or isinstance(payload["size"], bool) or payload["size"] < 1:
        raise PackageError("E_MANIFEST", "payload.size must be positive int")
    if not isinstance(payload["sha256"], str) or not _HEX64_RE.match(payload["sha256"]):
        raise PackageError("E_MANIFEST", "payload.sha256 must be 64 lowercase hex")
    if (
        not isinstance(payload["uncompressed_size_max"], int)
        or isinstance(payload["uncompressed_size_max"], bool)
        or payload["uncompressed_size_max"] < 1
    ):
        raise PackageError("E_MANIFEST", "payload.uncompressed_size_max must be positive int")

    preflight = obj["preflight"]
    if not isinstance(preflight, dict):
        raise _type_err("preflight", "object", preflight)
    _reject_unknown("preflight", preflight, _PREFLIGHT_KEYS)
    _require_keys("preflight", preflight, _PREFLIGHT_KEYS)
    cmds = preflight["commands"]
    if not isinstance(cmds, list) or not cmds or not all(isinstance(c, str) and c.startswith("/") for c in cmds):
        raise PackageError("E_MANIFEST", "preflight.commands must be non-empty absolute paths")
    for int_key in ("free_bytes_min", "free_bytes_multiplier"):
        val = preflight[int_key]
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise PackageError("E_MANIFEST", f"preflight.{int_key} must be positive int")

    deploy = obj["deploy"]
    if not isinstance(deploy, list):
        raise _type_err("deploy", "array", deploy)
    for i, item in enumerate(deploy):
        p = f"deploy[{i}]"
        if not isinstance(item, dict):
            raise _type_err(p, "object", item)
        _reject_unknown(p, item, _DEPLOY_KEYS)
        _require_keys(p, item, _DEPLOY_KEYS)
        if not isinstance(item["src"], str) or not item["src"] or item["src"].startswith("/"):
            raise PackageError("E_MANIFEST", f"{p}.src must be relative path")
        if ".." in Path(item["src"]).parts:
            raise PackageError("E_MANIFEST", f"{p}.src must not contain ..")
        if not dst_allowed(item["dst"]):
            raise PackageError("E_MANIFEST", f"{p}.dst not in allowlist: {item['dst']}")
        if path_is_preserved(item["dst"]):
            raise PackageError("E_MANIFEST", f"{p}.dst targets PRESERVE_PATHS: {item['dst']}")
        if not isinstance(item["mode"], str) or not _MODE_RE.match(item["mode"]):
            raise PackageError("E_MANIFEST", f"{p}.mode invalid")
        if not isinstance(item["owner"], str) or not _OWNER_RE.match(item["owner"]):
            raise PackageError("E_MANIFEST", f"{p}.owner must be user:group")

    services = obj["services"]
    if not isinstance(services, dict):
        raise _type_err("services", "object", services)
    _reject_unknown("services", services, _SERVICES_KEYS)
    _require_keys("services", services, _SERVICES_KEYS)
    if not isinstance(services["daemon_reload"], bool):
        raise PackageError("E_MANIFEST", "services.daemon_reload must be bool")
    for list_key in ("stop_before_apply", "restart"):
        arr = services[list_key]
        if not isinstance(arr, list) or not all(isinstance(x, str) and x for x in arr):
            raise PackageError("E_MANIFEST", f"services.{list_key} must be string array")
    health = services["health"]
    if not isinstance(health, dict):
        raise _type_err("services.health", "object", health)
    _reject_unknown("services.health", health, _HEALTH_KEYS)
    _require_keys("services.health", health, _HEALTH_KEYS)
    if not isinstance(health["http_url"], str) or not health["http_url"].startswith("http"):
        raise PackageError("E_MANIFEST", "services.health.http_url invalid")
    if not isinstance(health["units_active"], list) or not all(
        isinstance(x, str) and x for x in health["units_active"]
    ):
        raise PackageError("E_MANIFEST", "services.health.units_active must be string array")
    if not isinstance(health["version_file"], str) or not health["version_file"].startswith("/"):
        raise PackageError("E_MANIFEST", "services.health.version_file must be absolute path")

    delete = obj["delete"]
    if not isinstance(delete, list):
        raise _type_err("delete", "array", delete)
    for i, dpath in enumerate(delete):
        if not delete_allowed(dpath):
            raise PackageError("E_MANIFEST", f"delete[{i}] not allowed: {dpath!r}")

    migrations = obj["migrations"]
    if not isinstance(migrations, list):
        raise _type_err("migrations", "array", migrations)
    for i, mig in enumerate(migrations):
        p = f"migrations[{i}]"
        if not isinstance(mig, dict):
            raise _type_err(p, "object", mig)
        _reject_unknown(p, mig, _MIGRATION_KEYS)
        _require_keys(p, mig, _MIGRATION_KEYS)
        if not isinstance(mig["id"], str) or not mig["id"]:
            raise PackageError("E_MANIFEST", f"{p}.id invalid")
        if not isinstance(mig["min_from"], str) or not semver_valid(mig["min_from"]):
            raise PackageError("E_MANIFEST", f"{p}.min_from must be semver")
        if not isinstance(mig["script"], str) or not mig["script"] or mig["script"].startswith("/"):
            raise PackageError("E_MANIFEST", f"{p}.script must be relative path")
        if not isinstance(mig["sha256"], str) or not _HEX64_RE.match(mig["sha256"]):
            raise PackageError("E_MANIFEST", f"{p}.sha256 must be 64 hex")
        if not isinstance(mig["reversible"], bool):
            raise PackageError("E_MANIFEST", f"{p}.reversible must be bool")

    return obj


def _load_pem_public_key(pem: bytes):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    return load_pem_public_key(pem)


def _verify_ed25519_cryptography(pem: bytes, message: bytes, signature: bytes) -> None:
    key = _load_pem_public_key(pem)
    try:
        key.verify(signature, message)
    except Exception as exc:  # InvalidSignature and load errors
        raise PackageError("E_SIG", f"Ed25519 verify failed: {exc}") from exc


def _verify_ed25519_openssl(pem: bytes, message: bytes, signature: bytes) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise PackageError("E_SIG", "neither cryptography nor openssl available for Ed25519 verify")
    with tempfile.TemporaryDirectory(prefix="sa02m-sig-") as td:
        tdp = Path(td)
        (tdp / "key.pem").write_bytes(pem)
        (tdp / "msg").write_bytes(message)
        (tdp / "sig").write_bytes(signature)
        proc = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(tdp / "key.pem"),
                "-rawin",
                "-in",
                str(tdp / "msg"),
                "-sigfile",
                str(tdp / "sig"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise PackageError("E_SIG", f"openssl Ed25519 verify failed: {detail}")


def verify_manifest_signature(
    manifest: Dict[str, Any],
    sig_text: str,
    *,
    trusted_keys_dir: Path = DEFAULT_TRUSTED_KEYS_DIR,
) -> str:
    """Verify domain-separated Ed25519 over canonical manifest JSON.

    Tries trusted_keys/<signing_key_id>.pem first, then up to MAX_FALLBACK_KEYS
    other *.pem files. Returns the key id / filename stem that verified.
    """
    raw_b64 = sig_text.strip()
    try:
        signature = base64.b64decode(raw_b64, validate=True)
    except Exception as exc:
        raise PackageError("E_SIG", f"manifest.sig is not valid base64: {exc}") from exc
    if len(signature) != 64:
        raise PackageError("E_SIG", f"manifest.sig must decode to 64 bytes, got {len(signature)}")

    message = signature_message(manifest)
    keys_dir = Path(trusted_keys_dir)
    if not keys_dir.is_dir():
        raise PackageError("E_SIG", f"trusted keys dir missing: {keys_dir}")

    key_id = str(manifest.get("signing_key_id", ""))
    candidates: List[Path] = []
    preferred = keys_dir / f"{key_id}.pem"
    if preferred.is_file():
        candidates.append(preferred)
    others = sorted(p for p in keys_dir.glob("*.pem") if p.resolve() != preferred.resolve())
    for p in others:
        if len(candidates) >= MAX_FALLBACK_KEYS:
            break
        candidates.append(p)
    if not candidates:
        raise PackageError("E_SIG", f"no public keys in {keys_dir}")

    errors: List[str] = []
    use_crypto = True
    try:
        import cryptography  # noqa: F401
    except ImportError:
        use_crypto = False

    for key_path in candidates:
        pem = key_path.read_bytes()
        try:
            if use_crypto:
                _verify_ed25519_cryptography(pem, message, signature)
            else:
                _verify_ed25519_openssl(pem, message, signature)
            return key_path.stem
        except PackageError as exc:
            errors.append(f"{key_path.name}: {exc.message}")
        except Exception as exc:  # pragma: no cover
            errors.append(f"{key_path.name}: {exc}")

    raise PackageError("E_SIG", "signature rejected by all trusted keys: " + "; ".join(errors))


def _sha256_file(path: Path, *, max_bytes: Optional[int] = None) -> str:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise PackageError("E_HASH", "payload exceeds declared size while hashing")
            h.update(chunk)
    return h.hexdigest()


def _parse_sha256_sidecar(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    token = line.split()[0] if line else ""
    token = token.lower()
    if not _HEX64_RE.match(token):
        raise PackageError("E_HASH", "payload.sha256 member is not a 64-hex digest")
    return token


def _read_outer_members(path: Path, tar_size: int) -> Dict[str, bytes]:
    """Extract the four required outer members into memory (manifest/sig/sha small; payload streamed separately)."""
    members: Dict[str, bytes] = {}
    try:
        with path.open("rb") as fh:
            # Use TarFile over a file object capped at tar_size via read wrapper.
            class _Cap:
                def __init__(self, raw, limit: int) -> None:
                    self._raw = raw
                    self._remain = limit

                def read(self, n: int = -1) -> bytes:
                    if self._remain <= 0:
                        return b""
                    if n is None or n < 0:
                        n = self._remain
                    n = min(n, self._remain)
                    data = self._raw.read(n)
                    self._remain -= len(data)
                    return data

                def seekable(self) -> bool:
                    return False

            cap = _Cap(fh, tar_size)
            with tarfile.open(fileobj=cap, mode="r|") as tf:  # stream mode
                seen: Set[str] = set()
                for ti in tf:
                    name = ti.name
                    if name.startswith("./"):
                        name = name[2:]
                    if name not in OUTER_REQUIRED_MEMBERS:
                        raise PackageError("E_TAR", f"unexpected outer member: {name!r}")
                    if name in seen:
                        raise PackageError("E_TAR", f"duplicate outer member: {name!r}")
                    if ti.issym() or ti.islnk():
                        raise PackageError("E_TAR_SYMLINK", f"outer member must not be link: {name}")
                    if not ti.isreg():
                        raise PackageError("E_TAR", f"outer member must be regular file: {name}")
                    if name == "payload.tar.gz":
                        # Stream hash + write to temp later; for now collect via extractfile
                        ef = tf.extractfile(ti)
                        if ef is None:
                            raise PackageError("E_TAR", "cannot read payload.tar.gz")
                        members[name] = ef.read()
                    else:
                        if ti.size > 2 * 1024 * 1024:
                            raise PackageError("E_TAR", f"outer member too large: {name}")
                        ef = tf.extractfile(ti)
                        if ef is None:
                            raise PackageError("E_TAR", f"cannot read {name}")
                        members[name] = ef.read()
                    seen.add(name)
                if seen != OUTER_REQUIRED_MEMBERS:
                    missing = OUTER_REQUIRED_MEMBERS - seen
                    raise PackageError("E_TAR", f"outer tar missing members: {sorted(missing)}")
    except PackageError:
        raise
    except tarfile.TarError as exc:
        raise PackageError("E_TAR", f"outer tar error: {exc}") from exc
    except OSError as exc:
        raise PackageError("E_TAR", f"outer tar I/O: {exc}") from exc
    return members


def _check_inner_member_name(name: str) -> str:
    if name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/") or name.startswith("\\"):
        raise PackageError("E_TAR_TRAV", f"absolute name rejected: {name!r}")
    parts = Path(name).parts
    if ".." in parts:
        raise PackageError("E_TAR_TRAV", f"path traversal rejected: {name!r}")
    lower = name.lower()
    if lower.endswith(".tar") or lower.endswith(".tar.gz") or lower.endswith(".sa02m") or lower.endswith(".tgz"):
        raise PackageError("E_TAR", f"nested archive rejected: {name!r}")
    return name


def validate_safe_tar_members(
    tf: tarfile.TarFile,
    *,
    uncompressed_size_max: int,
    compressed_size: int,
) -> List[tarfile.TarInfo]:
    """Reject unsafe inner payload members; enforce bomb ratio / size cap."""
    infos: List[tarfile.TarInfo] = []
    seen: Set[str] = set()
    total_uncomp = 0
    for ti in tf:
        name = _check_inner_member_name(ti.name)
        if name in seen:
            raise PackageError("E_TAR", f"duplicate inner member: {name!r}")
        seen.add(name)
        if ti.issym() or ti.islnk():
            raise PackageError("E_TAR_SYMLINK", f"symlink/hardlink rejected: {name}")
        if ti.isdev() or ti.ischr() or ti.isblk() or ti.isfifo():
            raise PackageError("E_TAR_DEVICE", f"device/fifo rejected: {name}")
        if getattr(ti, "sparse", None):
            raise PackageError("E_TAR", f"sparse member rejected: {name}")
        # GNU sparse often surfaces as isreg with pax; reject typebits for sym/dev already.
        if not (ti.isreg() or ti.isdir()):
            raise PackageError("E_TAR", f"unsupported member type: {name}")
        if ti.isreg():
            if ti.size < 0:
                raise PackageError("E_TAR", f"negative size: {name}")
            total_uncomp += ti.size
            if total_uncomp > uncompressed_size_max:
                raise PackageError(
                    "E_TAR_BOMB",
                    f"uncompressed size {total_uncomp} > max {uncompressed_size_max}",
                )
        infos.append(ti)
    if compressed_size > 0 and total_uncomp > compressed_size * TAR_BOMB_RATIO_MAX:
        raise PackageError(
            "E_TAR_BOMB",
            f"compression ratio {total_uncomp}/{compressed_size} exceeds {TAR_BOMB_RATIO_MAX}",
        )
    return infos


def extract_payload_safe(
    payload_path: Path,
    dest_dir: Path,
    *,
    uncompressed_size_max: int,
) -> None:
    """Extract inner payload.tar.gz to staging with Py3.12 data filter + mode caps."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    compressed_size = payload_path.stat().st_size
    try:
        with tarfile.open(payload_path, mode="r:gz") as tf:
            validate_safe_tar_members(
                tf,
                uncompressed_size_max=uncompressed_size_max,
                compressed_size=compressed_size,
            )
        with tarfile.open(payload_path, mode="r:gz") as tf:
            # filter='data' is Python 3.12+; required on device.
            tf.extractall(path=dest_dir, members=None, filter="data")  # type: ignore[call-arg]
    except PackageError:
        raise
    except TypeError:
        # Older Python without filter= — still enforce manual checks then extract.
        with tarfile.open(payload_path, mode="r:gz") as tf:
            validate_safe_tar_members(
                tf,
                uncompressed_size_max=uncompressed_size_max,
                compressed_size=compressed_size,
            )
        with tarfile.open(payload_path, mode="r:gz") as tf:
            tf.extractall(path=dest_dir)
    except tarfile.TarError as exc:
        raise PackageError("E_TAR", f"payload extract error: {exc}") from exc

    # Cap modes; strip setuid/setgid/sticky.
    for root, dirs, files in os.walk(dest_dir):
        for d in dirs:
            p = Path(root) / d
            mode = p.stat().st_mode
            p.chmod(stat.S_IMODE(mode) & 0o755)
        for fn in files:
            p = Path(root) / fn
            mode = p.stat().st_mode
            p.chmod(stat.S_IMODE(mode) & 0o644)


def check_device_compat(
    manifest: Dict[str, Any],
    *,
    installed_version: str,
    runner_version: str,
    product: str = "SA-02m",
    model: str = "A40i",
    arch: str = "armv7l",
) -> None:
    if manifest.get("product") != product:
        raise PackageError("E_COMPAT", "product mismatch")
    if manifest.get("model") != model:
        raise PackageError("E_COMPAT", "model mismatch")
    if manifest.get("arch") != arch:
        raise PackageError("E_COMPAT", "arch mismatch")
    reason = check_update_gates(
        installed=installed_version,
        target=str(manifest["version"]),
        min_version=str(manifest["min_version"]),
        runner_version=runner_version,
        min_updater=str(manifest["min_updater"]),
    )
    if reason:
        raise PackageError("E_COMPAT", reason)


def check_version_file_match(manifest: Dict[str, Any], version_file_text: str) -> None:
    """manifest.version must equal first non-empty VERSION line from payload."""
    first = ""
    for line in version_file_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            first = line
            break
    if first != manifest["version"]:
        raise PackageError(
            "E_COMPAT",
            f"manifest.version {manifest['version']!r} != payload VERSION {first!r}",
        )


def validate_package(
    path: Path,
    *,
    trusted_keys_dir: Path = DEFAULT_TRUSTED_KEYS_DIR,
    installed_version: Optional[str] = None,
    runner_version: Optional[str] = None,
    extract_to: Optional[Path] = None,
    check_compat: bool = False,
) -> Dict[str, Any]:
    """Full streaming-ish validation of a .sa02m file. Returns inspect dict."""
    path = Path(path)
    tar_size = validate_container(path)
    members = _read_outer_members(path, tar_size)

    try:
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError("E_MANIFEST", f"manifest.json parse error: {exc}") from exc

    manifest = validate_manifest_object(manifest)
    key_used = verify_manifest_signature(
        manifest,
        members["manifest.sig"].decode("utf-8", errors="replace"),
        trusted_keys_dir=trusted_keys_dir,
    )

    payload_bytes = members["payload.tar.gz"]
    if len(payload_bytes) != manifest["payload"]["size"]:
        raise PackageError(
            "E_HASH",
            f"payload size {len(payload_bytes)} != manifest {manifest['payload']['size']}",
        )
    digest = hashlib.sha256(payload_bytes).hexdigest()
    if digest != manifest["payload"]["sha256"]:
        raise PackageError("E_HASH", "payload.tar.gz sha256 != manifest.payload.sha256")
    sidecar = _parse_sha256_sidecar(members["payload.sha256"].decode("utf-8", errors="replace"))
    if sidecar != digest:
        raise PackageError("E_HASH", "payload.sha256 member != payload.tar.gz digest")

    staging_payload: Optional[Path] = None
    tmp_root: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if extract_to is not None:
            dest = Path(extract_to)
            dest.mkdir(parents=True, exist_ok=True)
            staging_payload = dest / "payload.tar.gz"
            staging_payload.write_bytes(payload_bytes)
            extract_payload_safe(
                staging_payload,
                dest / "overlay",
                uncompressed_size_max=int(manifest["payload"]["uncompressed_size_max"]),
            )
            ver_path = dest / "overlay" / "www" / "network_config" / "VERSION"
            if ver_path.is_file():
                check_version_file_match(manifest, ver_path.read_text(encoding="utf-8"))
        else:
            # Still run safe-tar structural checks without leaving files.
            tmp_root = tempfile.TemporaryDirectory(prefix="sa02m-val-")
            staging_payload = Path(tmp_root.name) / "payload.tar.gz"
            staging_payload.write_bytes(payload_bytes)
            with tarfile.open(staging_payload, mode="r:gz") as tf:
                validate_safe_tar_members(
                    tf,
                    uncompressed_size_max=int(manifest["payload"]["uncompressed_size_max"]),
                    compressed_size=len(payload_bytes),
                )

        if check_compat:
            if not installed_version or not runner_version:
                raise PackageError("E_INTERNAL", "check_compat requires installed_version and runner_version")
            check_device_compat(
                manifest,
                installed_version=installed_version,
                runner_version=runner_version,
            )
    finally:
        if tmp_root is not None:
            tmp_root.cleanup()

    return {
        "ok": True,
        "tar_size": tar_size,
        "version": manifest["version"],
        "repo_commit": manifest["repo_commit"],
        "signing_key_id": manifest["signing_key_id"],
        "signature_key": key_used,
        "signature_ok": True,
        "payload_sha256": digest,
        "payload_size": len(payload_bytes),
        "manifest": manifest,
    }


def read_package_sha256(path: Path) -> str:
    return _sha256_file(Path(path))
