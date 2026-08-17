#!/bin/bash
# SA-02m downloadable config backup — installed as /usr/local/sbin/sa02m-web-backup.sh.
# Streams tar.gz to stdout: first member backup-manifest.json, then files/… allowlist.
# Optional last member: backup-manifest.json.sha256 (GNU sha256sum line).
# Plan §3.1. Run as root (sudo from web_backup.cgi).
# shellcheck shell=bash
set -euo pipefail

VERSION_FILE="${SA02M_WEB_VERSION_FILE:-/var/www/network_config/VERSION}"
DEVICE_ID_FILE="${SA02M_DEVICE_ID_FILE:-/etc/machine-id}"

# Allowlist roots / globs (relative paths under /).
collect_paths() {
  local p
  # Explicit files
  for p in \
    /etc/sa02m_web.env \
    /etc/nginx/.htpasswd \
    /etc/nginx/sites-enabled/000-sa02m-network_config \
    /etc/sa02m-cloud/agent.conf \
    /etc/sa02m-alice-client.conf \
    /etc/sa02m-alice-devices.conf
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
import hashlib, json, os, shutil, sys
from pathlib import Path

list_path, stage, manifest_path, device_id, fw_ver, created = sys.argv[1:7]
stage = Path(stage)
files_root = stage / "files"
paths_meta = []

with open(list_path, encoding="utf-8") as f:
    for line in f:
        src = line.strip()
        if not src or not os.path.isfile(src):
            continue
        # Strip leading /
        rel = src.lstrip("/")
        dst = files_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        h = hashlib.sha256()
        with open(src, "rb") as rf:
            for chunk in iter(lambda: rf.read(1024 * 1024), b""):
                h.update(chunk)
        st = os.stat(src)
        paths_meta.append({
            "path": src,
            "archive_path": "files/" + rel.replace("\\", "/"),
            "size": st.st_size,
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
