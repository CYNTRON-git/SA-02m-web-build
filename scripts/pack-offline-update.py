#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack a signed SA-02m offline update package (.sa02m v1).

Release-machine tool. Builds:
  out/SA-02m-update-<version>.sa02m
  out/SA-02m-update-<version>.sa02m.sha256

Gates (hard fail):
  - scripts/sync-app-version.py --check
  - clean git worktree (exit 2 if dirty)
  - Ed25519 private key present

See docs/OFFLINE_UPDATE_PACKAGE_V1.md.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import gzip
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = ROOT / "scripts" / "offline-update-allowlist.txt"
DEPLOY_MAP_PATH = ROOT / "scripts" / "offline-update-deploy-map.json"
SYNC_SCRIPT = ROOT / "scripts" / "sync-app-version.py"
VERSION_FILE = ROOT / "www" / "network_config" / "VERSION"
DEFAULT_OUT_DIR = ROOT / "out"
DEFAULT_KEY_ID = "release-2026-08"
MIN_UPDATER = "1.0.5.66"
MIN_VERSION = "1.0.5.60"
# Wire footer is 21 bytes (plan text "+20" was a miscount of the 19-byte magic).
FOOTER_MAGIC = b"SA02M_UPDATE_END_V1"
FOOTER = FOOTER_MAGIC + b"\0\0"
assert len(FOOTER) == 21
SIG_DOMAIN = b"SA02M-MANIFEST-V1\0"
VERSION_RE = re.compile(r"^\d+(\.\d+){2,3}$")
DST_PREFIX_RE = re.compile(
    r"^/(var/www/network_config/|usr/local/(sbin|lib|libexec)/|"
    r"opt/sa02m-[a-z0-9-]+/|opt/mplc4/(mplc_cyntron|mplc_protocol_fast_modbus)\.so|"
    r"etc/systemd/system/sa02m-|"
    r"etc/nginx/|etc/tmpfiles\.d/|etc/sudoers\.d/|"
    r"etc/sa02m-update/trusted-keys/)"
)

# Mapping table (kept in sync with offline-update-deploy-map.json):
#   www/network_config/*  → /var/www/network_config/*
#   opt/sa02m-*/*         → /opt/sa02m-*/*
#   usr/local/{sbin,lib,libexec}/* → same under /
#   etc/systemd/sa02m-*   → /etc/systemd/system/<basename>
#   etc/nginx/network_config.conf → /etc/nginx/sites-available/network_config (rendered)
#   etc/sa02m-*.sh helpers → /usr/local/{sbin,lib}/… (see etc_helper_renames)
#   etc/sudoers.d/*, etc/tmpfiles.d/*, etc/sa02m-update/trusted-keys/* → /etc/…


class PackError(SystemExit):
    pass


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def find_openssl() -> str:
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in (
        Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
        Path(r"C:\Program Files\Git\mingw64\bin\openssl.exe"),
        Path("/usr/bin/openssl"),
        Path("/bin/openssl"),
    ):
        if candidate.is_file():
            return str(candidate)
    _die(
        "openssl not found in PATH. Install OpenSSL 3.x or add Git for Windows "
        "usr\\bin to PATH."
    )
    return ""  # unreachable


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        cwd=str(cwd or ROOT),
        input=input_bytes,
        capture_output=True,
        check=check,
    )


def ensure_sync_app_version() -> None:
    if not SYNC_SCRIPT.is_file():
        _die(f"missing {SYNC_SCRIPT}")
    r = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        _die(
            "sync-app-version --check failed; run "
            f"`{sys.executable} scripts/sync-app-version.py` and commit.\n{detail}"
        )


def ensure_clean_git() -> None:
    r = run(["git", "status", "--porcelain"], check=False)
    if r.returncode != 0:
        _die("git status failed", 2)
    if r.stdout.strip():
        dirty = r.stdout.decode("utf-8", errors="replace").rstrip()
        _die(
            "refusing to pack a dirty git worktree (exit 2).\n"
            "Commit or stash all changes, then re-run.\n"
            f"{dirty}",
            2,
        )


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and VERSION_RE.match(line):
            return line
    _die(f"no version in {VERSION_FILE}")
    return ""


def git_commit_sha() -> str:
    r = run(["git", "rev-parse", "HEAD"])
    sha = r.stdout.decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        _die(f"unexpected HEAD sha: {sha!r}")
    return sha


def load_allowlist_patterns() -> list[str]:
    if not ALLOWLIST_PATH.is_file():
        _die(f"missing allowlist: {ALLOWLIST_PATH}")
    patterns: list[str] = []
    for raw in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.replace("\\", "/"))
    if not patterns:
        _die("allowlist is empty")
    return patterns


def load_deploy_map() -> dict[str, Any]:
    if not DEPLOY_MAP_PATH.is_file():
        _die(f"missing deploy map: {DEPLOY_MAP_PATH}")
    return json.loads(DEPLOY_MAP_PATH.read_text(encoding="utf-8"))


def path_matches(path: str, pattern: str) -> bool:
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/"):
        return path == pattern.rstrip("/") or path.startswith(pattern)
    if pattern.endswith("/**"):
        base = pattern[:-3]
        return path == base.rstrip("/") or path.startswith(base if base.endswith("/") else base + "/")
    if "**" in pattern:
        # fnmatch does not treat ** specially; approximate with * for segments
        return fnmatch.fnmatch(path, pattern.replace("**", "*"))
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return fnmatch.fnmatch(path, pattern)
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def list_allowlisted_files(patterns: list[str]) -> list[str]:
    r = run(["git", "ls-files", "-z"])
    tracked = [p.decode("utf-8").replace("\\", "/") for p in r.stdout.split(b"\0") if p]
    selected: list[str] = []
    for path in tracked:
        if "/__pycache__/" in path or path.endswith((".pyc", ".pyo")):
            continue
        if fnmatch.fnmatch(path, "scripts/0*.sh"):
            continue
        if any(path_matches(path, pat) for pat in patterns):
            selected.append(path)
    selected = sorted(set(selected))
    if not selected:
        _die("allowlist matched zero tracked files")
    if "www/network_config/VERSION" not in selected:
        _die("allowlist must include www/network_config/VERSION")
    return selected


def git_archive_extract(paths: list[str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    # git archive path list can be large — write to a path list via stdin is not
    # supported; pass paths as args in chunks.
    chunk_size = 200
    for i in range(0, len(paths), chunk_size):
        chunk = paths[i : i + chunk_size]
        r = run(["git", "archive", "--format=tar", "HEAD", "--", *chunk])
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:") as tf:
            try:
                tf.extractall(path=dest, filter="data")  # type: ignore[arg-type]
            except TypeError:
                tf.extractall(path=dest)


def overlay_workdir_files(paths: list[str], dest: Path) -> int:
    """Copy current worktree files over git-archive overlay (--allow-dirty)."""
    n = 0
    for rel in paths:
        src = ROOT / rel
        if not src.is_file():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        n += 1
    # Also include newly untracked allowlisted files present on disk
    for src in (ROOT / "opt").rglob("*") if (ROOT / "opt").is_dir() else []:
        if not src.is_file() or "__pycache__" in src.parts or src.suffix in (".pyc", ".pyo"):
            continue
        rel = src.relative_to(ROOT).as_posix()
        if not rel.startswith("opt/sa02m-"):
            continue
        out = dest / rel
        if out.is_file():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        n += 1
    for src in [
        ROOT / "etc" / "sa02m-update-runner.sh",
        ROOT / "etc" / "sa02m-update-inspect.sh",
        ROOT / "etc" / "sa02m-web-backup.sh",
        ROOT / "etc" / "sa02m-restore-backup.sh",
        ROOT / "etc" / "sa02m-factory-reset-runner.sh",
        ROOT / "etc" / "systemd" / "sa02m-update.service",
        ROOT / "etc" / "systemd" / "sa02m-update-recover.service",
        ROOT / "etc" / "systemd" / "sa02m-factory-reset.service",
        ROOT / "etc" / "nginx" / "network_config.conf",
    ]:
        if src.is_file():
            rel = src.relative_to(ROOT).as_posix()
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            n += 1
    return n


def render_nginx_template(overlay: Path) -> None:
    conf = overlay / "etc" / "nginx" / "network_config.conf"
    if not conf.is_file():
        return
    text = conf.read_text(encoding="utf-8")
    text = text.replace("__PORT__", "9999").replace("__WEB_ROOT__", "/var/www/network_config")
    conf.write_text(text, encoding="utf-8", newline="\n")


def iter_overlay_files(overlay: Path) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(overlay):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                _die(f"refusing symlink in overlay: {full}")
            rel = full.relative_to(overlay).as_posix()
            files.append(rel)
    return sorted(files)


def _exec_mode(rel: str, globs: list[str] | None) -> bool:
    if not globs:
        return False
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(Path(rel).name, g) for g in globs)


def map_deploy_entry(rel: str, deploy_map: dict[str, Any]) -> dict[str, str] | None:
    """Return deploy{src,dst,mode,owner} or None if path is not deployable."""
    rel = rel.replace("\\", "/")

    exact = deploy_map.get("exact_rules") or {}
    if rel in exact:
        rule = exact[rel]
        return {
            "src": rel,
            "dst": str(rule["dst"]),
            "mode": str(rule["mode"]),
            "owner": str(rule["owner"]),
        }

    renames = deploy_map.get("etc_helper_renames") or {}
    if rel in renames:
        rule = renames[rel]
        return {
            "src": rel,
            "dst": str(rule["dst"]),
            "mode": str(rule["mode"]),
            "owner": str(rule["owner"]),
        }

    # Flat etc/systemd/sa02m-* → /etc/systemd/system/<basename>
    flat = deploy_map.get("systemd_flat_map") or {}
    src_dir = str(flat.get("src_dir") or "etc/systemd/")
    if rel.startswith(src_dir) and "/" not in rel[len(src_dir) :]:
        base = Path(rel).name
        if fnmatch.fnmatch(base, str(flat.get("name_glob") or "sa02m-*")):
            dst = str(flat.get("dst_dir") or "/etc/systemd/system/") + base
            return {
                "src": rel,
                "dst": dst,
                "mode": str(flat.get("mode") or "0644"),
                "owner": str(flat.get("owner") or "root:root"),
            }

    for rule in deploy_map.get("prefix_rules") or []:
        src_prefix = str(rule["src_prefix"])
        if not rel.startswith(src_prefix):
            continue
        dst = str(rule["dst_prefix"]) + rel[len(src_prefix) :]
        require = rule.get("require_dst_prefix")
        if require and not dst.startswith(str(require)):
            continue
        mode = str(rule.get("mode") or "0644")
        if _exec_mode(rel, rule.get("exec_globs")):
            mode = "0755"
        return {
            "src": rel,
            "dst": dst,
            "mode": mode,
            "owner": str(rule.get("owner") or "root:root"),
        }

    return None


def build_deploy_list(overlay_files: list[str], deploy_map: dict[str, Any]) -> list[dict[str, str]]:
    deploy: list[dict[str, str]] = []
    skipped: list[str] = []
    for rel in overlay_files:
        if Path(rel).name == ".gitkeep":
            skipped.append(rel)
            continue
        entry = map_deploy_entry(rel, deploy_map)
        if entry is None:
            skipped.append(rel)
            continue
        if not DST_PREFIX_RE.match(entry["dst"]):
            _die(f"deploy dst not allowlisted by §2.2 regex: {entry['dst']} (src={rel})")
        deploy.append(entry)
    if skipped:
        print(
            f"note: {len(skipped)} overlay path(s) have no deploy mapping (not shipped to live paths):",
            file=sys.stderr,
        )
        for s in skipped[:20]:
            print(f"  - {s}", file=sys.stderr)
        if len(skipped) > 20:
            print(f"  … and {len(skipped) - 20} more", file=sys.stderr)
    if not deploy:
        _die("deploy[] is empty — check allowlist/deploy-map")
    # Stable order for review diffs
    deploy.sort(key=lambda d: d["dst"])
    return deploy


def make_payload_tar_gz(overlay: Path, files: list[str]) -> tuple[bytes, int]:
    """Return (gzip_bytes, uncompressed_tar_size)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for rel in files:
            full = overlay / rel
            info = tarfile.TarInfo(name=rel)
            data = full.read_bytes()
            info.size = len(data)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            # mode bits for archive members (runner still applies deploy[].mode)
            if rel.startswith("www/network_config/cgi-bin/") or rel.endswith(
                (".cgi", ".sh")
            ):
                info.mode = 0o755
            else:
                info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    if len(raw) % 512 != 0:
        _die(f"inner tar size not multiple of 512: {len(raw)}")
    gz = gzip.compress(raw, mtime=0, compresslevel=9)
    return gz, len(raw)


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def resolve_signing_key(key_id: str, explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            _die(f"signing key not found: {explicit}")
        return explicit
    env = os.environ.get("SA02M_UPDATE_SIGNING_KEY", "").strip()
    if env:
        p = Path(env)
        if not p.is_file():
            _die(f"SA02M_UPDATE_SIGNING_KEY not a file: {p}")
        return p
    default = ROOT / "private" / "sa02m-update-keys" / f"{key_id}.ed25519"
    if not default.is_file():
        _die(
            "Ed25519 private key missing.\n"
            f"  expected: {default}\n"
            "  or set SA02M_UPDATE_SIGNING_KEY=/path/to/key.ed25519\n"
            "Generate with:\n"
            f"  {sys.executable} scripts/gen-update-signing-key.py --key-id {key_id}\n"
            "Then commit the public key under etc/sa02m-update/trusted-keys/ "
            f"(private/ stays gitignored)."
        )
    return default


def sign_manifest(manifest: dict[str, Any], key_path: Path, openssl: str) -> bytes:
    message = SIG_DOMAIN + canonical_json(manifest)
    with tempfile.NamedTemporaryFile(prefix="sa02m-sigmsg-", delete=False) as mf:
        mf.write(message)
        msg_path = mf.name
    try:
        r = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-sign",
                "-inkey",
                str(key_path),
                "-rawin",
                "-in",
                msg_path,
            ],
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace")
            _die(f"openssl pkeyutl -sign failed:\n{err}")
        sig_raw = r.stdout
        if len(sig_raw) != 64:
            _die(f"unexpected signature length {len(sig_raw)} (want 64)")
        return base64.b64encode(sig_raw) + b"\n"
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass


def build_manifest(
    *,
    version: str,
    commit: str,
    key_id: str,
    payload_gz: bytes,
    uncompressed_size: int,
    deploy: list[dict[str, str]],
) -> dict[str, Any]:
    digest = hashlib.sha256(payload_gz).hexdigest()
    unc_max = max(134217728, uncompressed_size * 2)
    return {
        "schema_version": 1,
        "product": "SA-02m",
        "model": "A40i",
        "arch": "armv7l",
        "version": version,
        "repo_commit": commit,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signing_key_id": key_id,
        "min_updater": MIN_UPDATER,
        "min_version": MIN_VERSION,
        "payload": {
            "size": len(payload_gz),
            "sha256": digest,
            "uncompressed_size_max": unc_max,
        },
        "preflight": {
            "commands": [
                "/bin/bash",
                "/usr/bin/python3",
                "/usr/bin/openssl",
                "/usr/bin/rsync",
                "/usr/bin/tar",
                "/usr/bin/gzip",
                "/usr/bin/curl",
                "/usr/bin/sha256sum",
                "/usr/bin/flock",
                "/usr/bin/systemctl",
                "/usr/bin/install",
                "/usr/local/libexec/sa02m-update-runner",
            ],
            "free_bytes_min": 67108864,
            "free_bytes_multiplier": 3,
        },
        "deploy": deploy,
        "services": {
            "daemon_reload": True,
            "stop_before_apply": ["sa02m-flasher"],
            "enable": [
                "sa02m-devices-api.service",
                "sa02m-devices-logger.service",
                # Must stay in step with the online generator's list in
                # etc/sa02m-update-runner.sh — an entry in one path only means
                # the offline and online updates disagree about what runs at
                # the next boot. See docs/contracts/boot-network-dns.md.
                "sa02m-dns-ensure.service",
            ],
            "restart": [
                "fcgiwrap",
                "nginx",
                "sa02m-devices-api",
                "sa02m-devices-logger",
                # Telemetry owns its own device id + legacy-retained clear
                # (1.0.6.21) — the new .py does nothing until the process
                # restarts. This generator runs on the dev host, so unlike the
                # online path the entry is live in the very package that
                # delivers the change. Must stay in step with the online
                # generator's list in etc/sa02m-update-runner.sh.
                "sa02m-telemetry",
            ],
            "health": {
                "http_url": "http://127.0.0.1:9999/login.html",
                "units_active": ["nginx", "fcgiwrap", "sa02m-devices-api"],
                "version_file": "/var/www/network_config/VERSION",
            },
        },
        "delete": [],
        "migrations": [],
    }


def write_outer_package(
    path: Path,
    *,
    manifest: dict[str, Any],
    sig_b64: bytes,
    payload_gz: bytes,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    # Pretty JSON is for humans in the archive; signature uses canonical_json(manifest).
    payload_sha_member = (manifest["payload"]["sha256"] + "  payload.tar.gz\n").encode("ascii")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for name, data in (
            ("manifest.json", manifest_bytes),
            ("manifest.sig", sig_b64),
            ("payload.tar.gz", payload_gz),
            ("payload.sha256", payload_sha_member),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            tf.addfile(info, io.BytesIO(data))
    tar_bytes = buf.getvalue()
    if len(tar_bytes) % 512 != 0:
        _die(f"outer tar size not multiple of 512: {len(tar_bytes)}")
    path.write_bytes(tar_bytes + FOOTER)


def write_sidecar(package_path: Path) -> Path:
    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    side = Path(str(package_path) + ".sha256")
    side.write_text(f"{digest}  {package_path.name}\n", encoding="ascii")
    return side


def roundtrip_validate(package_path: Path, keys_dir: Path) -> None:
    lib_parent = ROOT / "opt" / "sa02m-update"
    if not (lib_parent / "lib" / "validate_package.py").is_file():
        print("note: validate_package.py absent — skip round-trip validate", file=sys.stderr)
        return
    # Prefer package root on sys.path so `from lib import …` matches device layout.
    sys.path.insert(0, str(lib_parent))
    from lib import validate_package as vp  # type: ignore

    info = vp.validate_package(package_path, trusted_keys_dir=keys_dir)
    n_deploy = len(info.get("manifest", {}).get("deploy", []))
    print(
        f"round-trip OK: version={info['version']} deploy={n_deploy} "
        f"sig={info.get('signature_ok')} key={info.get('signature_key')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Pack signed SA-02m .sa02m update package")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--key-id", default=DEFAULT_KEY_ID)
    ap.add_argument("--signing-key", type=Path, default=None, help="Ed25519 private key PEM")
    ap.add_argument(
        "--skip-validate",
        action="store_true",
        help="Do not import opt/sa02m-update/lib/validate_package.py",
    )
    ap.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow packing with a dirty git worktree (bench/CI only; not for release)",
    )
    args = ap.parse_args()

    ensure_sync_app_version()
    if not args.allow_dirty:
        ensure_clean_git()
    else:
        print("warning: --allow-dirty set; package is not a release artifact", file=sys.stderr)

    version = read_version()
    commit = git_commit_sha()
    openssl = find_openssl()
    key_path = resolve_signing_key(args.key_id, args.signing_key)
    patterns = load_allowlist_patterns()
    deploy_map = load_deploy_map()
    paths = list_allowlisted_files(patterns)

    with tempfile.TemporaryDirectory(prefix="sa02m-pack-") as tmp:
        overlay = Path(tmp) / "overlay"
        git_archive_extract(paths, overlay)
        if args.allow_dirty:
            n = overlay_workdir_files(paths, overlay)
            print(f"allow-dirty: overlaid {n} worktree files", file=sys.stderr)
        render_nginx_template(overlay)
        overlay_files = iter_overlay_files(overlay)
        # Ensure VERSION in payload matches branch version gate
        ver_payload = (overlay / "www" / "network_config" / "VERSION").read_text(encoding="utf-8")
        first = ""
        for line in ver_payload.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and VERSION_RE.match(s):
                first = s
                break
        if first != version:
            _die(f"payload VERSION {first!r} != pack version {version!r}")

        deploy = build_deploy_list(overlay_files, deploy_map)
        payload_gz, unc_size = make_payload_tar_gz(overlay, overlay_files)
        manifest = build_manifest(
            version=version,
            commit=commit,
            key_id=args.key_id,
            payload_gz=payload_gz,
            uncompressed_size=unc_size,
            deploy=deploy,
        )
        sig = sign_manifest(manifest, key_path, openssl)

        out_dir = args.out_dir if args.out_dir.is_absolute() else ROOT / args.out_dir
        package_path = out_dir / f"SA-02m-update-{version}.sa02m"
        write_outer_package(
            package_path, manifest=manifest, sig_b64=sig, payload_gz=payload_gz
        )
        side = write_sidecar(package_path)

    # Self-check footer math without validator
    size = package_path.stat().st_size
    tar_size = size - len(FOOTER)
    if tar_size % 512 != 0:
        _die(f"internal error: tar_size%512 != 0 ({tar_size})")
    with package_path.open("rb") as f:
        f.seek(-len(FOOTER), os.SEEK_END)
        if f.read() != FOOTER:
            _die("internal error: footer mismatch")

    keys_dir = ROOT / "etc" / "sa02m-update" / "trusted-keys"
    if not args.skip_validate:
        if not keys_dir.is_dir():
            print(
                f"warning: {keys_dir} missing — skip signature round-trip "
                "(public key should be committed there)",
                file=sys.stderr,
            )
        else:
            roundtrip_validate(package_path, keys_dir)

    print(f"Wrote {package_path} ({size} bytes, {len(deploy)} deploy entries)")
    print(f"Wrote {side}")
    print(f"version={version} commit={commit} key_id={args.key_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", errors="replace") if e.stderr else str(e)
        _die(f"command failed: {e.cmd}\n{err}")
