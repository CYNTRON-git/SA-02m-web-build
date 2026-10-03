#!/bin/bash
# SA-02m downloadable config backup — installed as /usr/local/sbin/sa02m-web-backup.sh.
# Streams tar.gz to stdout: first member backup-manifest.json, then files/… allowlist.
# Optional last member: backup-manifest.json.sha256 (GNU sha256sum line).
# Plan §3.1. Run as root (sudo from web_backup.cgi), and the archive goes to the
# panel: a source is read only as a regular file reached without following a name
# anyone but root could have planted (resolve_trusted below); anything else is
# skipped with a WARN on stderr, never archived.
# shellcheck shell=bash
set -euo pipefail

VERSION_FILE="${SA02M_WEB_VERSION_FILE:-/var/www/network_config/VERSION}"
DEVICE_ID_FILE="${SA02M_DEVICE_ID_FILE:-/etc/machine-id}"

# Allowlist roots / globs (relative paths under /). Every path emitted here must
# also pass sa02m-restore-backup.sh's ALLOW list, or a restore of the archive
# fails as a whole. Alice: /etc/sa02m-alice/ is the live conf layout; the flat
# /etc/sa02m-alice-*.conf names are the older one (an old board may carry them).
# HomeKit: the conf (enabled/interface/port) only. /var/lib/sa02m-homekit/ is
# never archived — it holds the accessory's long-term key and the paired
# controllers, and an archive goes to the panel (docs/contracts/homekit-bridge.md
# P4); a restored board pairs anew. This list is the allow-list, so leaving the
# dir out is the whole guarantee.
# Home Connect: the conf (enabled, the integrator's Client ID — not a secret)
# only. /var/lib/sa02m-homeconnect/ is never archived — it holds the OAuth
# refresh token of the owner's BSH account (docs/contracts/home-connect.md P4,
# §12); a restored board signs in anew.
collect_paths() {
  local p
  # Explicit files
  for p in \
    /etc/sa02m_web.env \
    /etc/nginx/.htpasswd \
    /etc/nginx/sites-enabled/000-sa02m-network_config \
    /etc/sa02m-cloud/agent.conf \
    /etc/sa02m-alice/sa02m-alice-client.conf \
    /etc/sa02m-alice/sa02m-alice-devices.conf \
    /etc/sa02m-alice-client.conf \
    /etc/sa02m-alice-devices.conf \
    /etc/sa02m-homeconnect/sa02m-homeconnect.conf \
    /etc/sa02m-homekit/sa02m-homekit.conf \
    /etc/sa02m-agent-api/tokens.json
  do
    [ -e "$p" ] && printf '%s\n' "$p"
  done
  # /etc/sa02m*.conf (not directories)
  shopt -s nullglob
  for p in /etc/sa02m*.conf /etc/sa02m_*.conf; do
    [ -f "$p" ] || continue
    case "$p" in
      *.log) continue ;;
    esac
    printf '%s\n' "$p"
  done
  # Directories (files under them)
  if [ -d /etc/sa02m-device-templates ]; then
    find /etc/sa02m-device-templates -type f ! -name '*.log' 2>/dev/null || true
  fi
  if [ -d /etc/network/interfaces.d ]; then
    find /etc/network/interfaces.d -type f ! -name '*.log' 2>/dev/null || true
  fi
  shopt -u nullglob
}

TMP=$(mktemp -d /tmp/sa02m-backup.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

LIST="$TMP/paths.txt"
MANIFEST="$TMP/backup-manifest.json"
STAGE="$TMP/stage"
mkdir -p "$STAGE/files"

collect_paths | sort -u > "$LIST"

device_id=""
[ -f "$DEVICE_ID_FILE" ] && device_id=$(tr -d '\r\n' < "$DEVICE_ID_FILE")
fw_ver=""
if [ -f "$VERSION_FILE" ]; then
  fw_ver=$(tr -d '\r' < "$VERSION_FILE" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
fi
created=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -Iseconds)

# Build staged tree + manifest paths[]
python3 - "$LIST" "$STAGE" "$MANIFEST" "$device_id" "$fw_ver" "$created" <<'PY'
import hashlib, json, os, shutil, stat, sys
from pathlib import Path

list_path, stage, manifest_path, device_id, fw_ver, created = sys.argv[1:7]
stage = Path(stage)
files_root = stage / "files"
paths_meta = []

# >>> trusted-path resolver — twin: etc/sa02m-web-backup.sh and
# etc/sa02m-restore-backup.sh carry this block byte-identical (row
# alice-conf-homes, case 7t); neither root script can import the other.
# Why: both run as root, and /etc/sa02m-alice is root:www-data 0771, so www-data
# (any panel session: cmd_exec.cgi) can create any name there. A name is
# trusted only where nobody but root could have made it.
class Unsafe(Exception):
    pass

TRUSTED_UIDS = {0, os.geteuid()}

def root_only(st):
    """Only a trusted uid can create, rename or replace a name in this directory."""
    return stat.S_ISDIR(st.st_mode) and st.st_uid in TRUSTED_UIDS and not st.st_mode & 0o022

def pinned(dir_st, st):
    """Nobody else can replace this entry: its directory is root-only, or it is
    a sticky, trusted-owned directory (/tmp) and the entry is trusted-owned."""
    if root_only(dir_st):
        return True
    return (stat.S_ISDIR(dir_st.st_mode) and dir_st.st_uid in TRUSTED_UIDS
            and bool(dir_st.st_mode & stat.S_ISVTX) and st.st_uid in TRUSTED_UIDS)

def resolve_trusted(path):
    """(resolved path, whether its last directory is root-only), or Unsafe.
    A symlink is followed only when nobody else could have made or replaced it
    (the installer's /etc/nginx/sites-enabled link); every directory on the way
    must be pinned likewise, so none can be swapped between this walk and the
    caller's open/rename. Only the LAST name may sit in a directory others can
    write: the caller must then open it O_NOFOLLOW or replace it by rename."""
    parts = [p for p in path.split("/") if p]
    cur, cur_st, hops = "/", os.lstat("/"), 0
    while parts:
        name = parts.pop(0)
        if name == ".":
            continue
        if name == "..":
            cur = os.path.dirname(cur)
            cur_st = os.lstat(cur)
            continue
        nxt = os.path.join(cur, name)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            if parts and not root_only(cur_st): raise Unsafe(f"{nxt} is missing in {cur}, which others can write")
            return os.path.join(nxt, *parts), root_only(cur_st)
        if stat.S_ISLNK(st.st_mode):
            if not pinned(cur_st, st): raise Unsafe(f"{nxt} is a symlink in {cur}, which others can write")
            hops += 1
            if hops > 16:
                raise Unsafe(f"{path}: too many levels of symlinks")
            target = os.readlink(nxt)
            if target.startswith("/"):
                cur, cur_st = "/", os.lstat("/")
            parts = [p for p in target.split("/") if p] + parts
            continue
        if parts:
            if not stat.S_ISDIR(st.st_mode):
                raise Unsafe(f"{nxt} is not a directory")
            if not pinned(cur_st, st): raise Unsafe(f"{nxt} is a directory in {cur}, which others can write")
            cur, cur_st = nxt, st
            continue
        return nxt, root_only(cur_st)
    return cur, root_only(cur_st)
# <<< trusted-path resolver

def open_source(src):
    """(fd, fstat) of `src` as a regular file, opened without following a name
    anyone but root could have planted; Unsafe/OSError otherwise. The bytes,
    the hash, the size and the mode then all come from this one fd — no
    check-then-open window."""
    real, dir_root_only = resolve_trusted(src)
    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode): raise Unsafe(f"{real} is not a regular file")
        # A hard link planted to a root-only file would pass every check
        # above (fs.protected_hardlinks=1 blocks it on a stock board; this does
        # not rely on that sysctl).
        if st.st_nlink > 1 and not dir_root_only: raise Unsafe(f"{real} has {st.st_nlink} hard links in a directory others can write")
    except BaseException:
        os.close(fd)
        raise
    return fd, st

with open(list_path, encoding="utf-8") as f:
    for line in f:
        src = line.strip()
        if not src:
            continue
        try:
            fd, st = open_source(src)
        except FileNotFoundError:
            continue
        except (Unsafe, OSError) as e:
            # An optional entry is dropped, never its bytes guessed at: the
            # rest of the backup is still worth having.
            print(f"WARN: backup skips {src}: {e}", file=sys.stderr)
            continue
        # Strip leading /
        rel = src.lstrip("/")
        dst = files_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb") as rf, open(dst, "wb") as out:
            for chunk in iter(lambda: rf.read(1024 * 1024), b""):
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        paths_meta.append({
            "path": src,
            "archive_path": "files/" + rel.replace("\\", "/"),
            "size": size,
            "sha256": h.hexdigest(),
            "mode": oct(st.st_mode & 0o777),
        })

manifest = {
    "schema_version": 1,
    "product": "SA-02m",
    "device_id": device_id or None,
    "firmware_version": fw_ver or None,
    "created_at": created,
    "paths": paths_meta,
}
Path(manifest_path).write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
# Copy manifest into stage as first archive member source
shutil.copy2(manifest_path, stage / "backup-manifest.json")
mh = hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
(stage / "backup-manifest.json.sha256").write_text(
    f"{mh}  backup-manifest.json\n", encoding="utf-8"
)
PY

# Stream tar.gz: manifest first, then files/, then manifest sha256 sidecar.
FILELIST="$TMP/tar.list"
{
  printf '%s\n' backup-manifest.json
  ( cd "$STAGE" && find files -type f 2>/dev/null | sort ) || true
  printf '%s\n' backup-manifest.json.sha256
} | sed '/^$/d' > "$FILELIST"

tar -czf - -C "$STAGE" -T "$FILELIST"
