#!/bin/bash
# SA-02m SSH-only backup restore — /usr/local/sbin/sa02m-restore-backup.sh
# Usage:
#   sa02m-restore-backup.sh --dry-run /path/backup.tar.gz
#   sa02m-restore-backup.sh --apply    /path/backup.tar.gz
# Plan §3.2: validate manifest + per-file sha256, allowlist, pre-restore snapshot,
# per-file atomic restore (no tar -C /), nginx reload, no reboot.
# shellcheck shell=bash
set -euo pipefail

MODE=""
ARCHIVE=""

usage() {
  echo "Usage: $0 --dry-run|--apply /path/backup.tar.gz" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) MODE=dry-run; shift ;;
    --apply) MODE=apply; shift ;;
    -h|--help) usage ;;
    *)
      if [ -z "$ARCHIVE" ]; then
        ARCHIVE=$1
        shift
      else
        usage
      fi
      ;;
  esac
done

[ -n "$MODE" ] && [ -n "$ARCHIVE" ] || usage
[ -f "$ARCHIVE" ] || { echo "ERROR: archive not found: $ARCHIVE" >&2; exit 1; }

export SA02M_WEB_BACKUP="${SA02M_WEB_BACKUP:-/usr/local/sbin/sa02m-web-backup.sh}"
export SA02M_BACKUP_EXPORT="${SA02M_BACKUP_EXPORT:-/var/lib/sa02m-update/backup-export}"

TMP=$(mktemp -d /tmp/sa02m-restore.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# Extract to staging only (never tar -C /).
tar -xzf "$ARCHIVE" -C "$TMP"

MANIFEST="$TMP/backup-manifest.json"
[ -f "$MANIFEST" ] || { echo "ERROR: backup-manifest.json missing" >&2; exit 1; }

python3 - "$MANIFEST" "$TMP" "$MODE" <<'PY'
import hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

manifest_path, staging, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if int(manifest.get("schema_version", 0)) != 1:
    print("ERROR: unsupported schema_version", file=sys.stderr)
    sys.exit(1)
paths = manifest.get("paths")
if not isinstance(paths, list):
    print("ERROR: paths[] missing", file=sys.stderr)
    sys.exit(1)

ALLOW = [
    re.compile(r"^/etc/sa02m_web\.env$"),
    re.compile(r"^/etc/nginx/\.htpasswd$"),
    re.compile(r"^/etc/nginx/sites-enabled/000-sa02m-network_config$"),
    re.compile(r"^/etc/sa02m-cloud/agent\.conf$"),
    re.compile(r"^/etc/sa02m-alice-client\.conf$"),
    re.compile(r"^/etc/sa02m-alice-devices\.conf$"),
    re.compile(r"^/etc/sa02m[^/]*\.conf$"),
    re.compile(r"^/etc/sa02m_[^/]*\.conf$"),
    re.compile(r"^/etc/sa02m-device-templates/"),
    re.compile(r"^/etc/network/interfaces\.d/"),
]

def allowed(p: str) -> bool:
    return any(r.search(p) for r in ALLOW)

errors = []
entries = []
for ent in paths:
    if not isinstance(ent, dict):
        errors.append("bad path entry")
        continue
    dest = ent.get("path") or ""
    ap = ent.get("archive_path") or ""
    expect = (ent.get("sha256") or "").lower()
    if not dest.startswith("/") or ".." in dest.split("/"):
        errors.append(f"invalid path {dest!r}")
        continue
    if not allowed(dest):
        errors.append(f"path not allowlisted: {dest}")
        continue
    if not ap.startswith("files/") or ".." in ap.split("/"):
        errors.append(f"bad archive_path {ap!r}")
        continue
    src = staging / ap
    if not src.is_file():
        errors.append(f"missing member {ap}")
        continue
    h = hashlib.sha256(src.read_bytes()).hexdigest()
    if h != expect:
        errors.append(f"sha256 mismatch {dest}: {h} != {expect}")
        continue
    mode_s = ent.get("mode") or "0o644"
    try:
        fmode = int(str(mode_s), 0)
    except ValueError:
        fmode = 0o644
    entries.append((dest, src, fmode & 0o777))

if errors:
    for e in errors:
        print("ERROR:", e, file=sys.stderr)
    sys.exit(1)

print(f"OK: validated {len(entries)} files (mode={mode})")
for dest, src, fmode in entries:
    print(f"  {dest} mode={oct(fmode)} size={src.stat().st_size}")

if mode == "dry-run":
    sys.exit(0)

export_dir = Path(os.environ.get("SA02M_BACKUP_EXPORT", "/var/lib/sa02m-update/backup-export"))
export_dir.mkdir(parents=True, exist_ok=True)
ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
snap = export_dir / f"pre-restore-{ts}.tar.gz"
backup_bin = os.environ.get("SA02M_WEB_BACKUP", "/usr/local/sbin/sa02m-web-backup.sh")
if Path(backup_bin).is_file():
    with snap.open("wb") as out:
        r = subprocess.run([backup_bin], stdout=out, stderr=subprocess.PIPE, check=False)
    if r.returncode != 0:
        print("ERROR: pre-restore backup failed:", r.stderr.decode("utf-8", "replace"), file=sys.stderr)
        sys.exit(1)
    print(f"pre-restore snapshot: {snap}")
else:
    print("WARN: sa02m-web-backup.sh missing — continuing without snapshot", file=sys.stderr)

def atomic_install(src: Path, dest: str, fmode: int) -> None:
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_p.with_name(dest_p.name + f".tmp.{os.getpid()}")
    shutil.copy2(src, tmp)
    os.chmod(tmp, fmode)
    try:
        os.chown(tmp, 0, 0)
    except OSError:
        pass
    os.replace(tmp, dest_p)
    dir_fd = os.open(str(dest_p.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

for dest, src, fmode in entries:
    atomic_install(src, dest, fmode)
    print(f"restored {dest}")

def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    out = r.stdout.decode("utf-8", "replace")
    if out.strip():
        print(out.rstrip())
    return r.returncode

rc = 0
nginx = shutil.which("nginx") or "/usr/sbin/nginx"
if Path(nginx).is_file():
    if run([nginx, "-t"]) != 0:
        print("ERROR: nginx -t failed", file=sys.stderr)
        rc = 1
    else:
        run(["systemctl", "reload", "nginx"])
run(["systemctl", "restart", "fcgiwrap"])
print("health: restore complete (no reboot)")
sys.exit(rc)
PY
