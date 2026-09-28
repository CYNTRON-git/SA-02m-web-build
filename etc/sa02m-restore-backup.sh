#!/bin/bash
# SA-02m SSH-only backup restore — /usr/local/sbin/sa02m-restore-backup.sh
# Usage:
#   sa02m-restore-backup.sh --dry-run /path/backup.tar.gz
#   sa02m-restore-backup.sh --apply    /path/backup.tar.gz
# Plan §3.2: validate manifest + per-file sha256, allowlist, pre-restore snapshot,
# per-file atomic restore (no tar -C /; a symlink or directory others could have
# planted on the path, or a non-regular dest, fails the preflight — a link only
# root could have made is written through), nginx reload, restart of the ACTIVE
# Alice units when their confs were restored, no reboot.
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
import grp, hashlib, json, os, pwd, re, shutil, stat, subprocess, sys, tempfile, time
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
    re.compile(r"^/etc/sa02m-alice/sa02m-alice-(client|devices)\.conf$"),
    re.compile(r"^/etc/sa02m-alice-client\.conf$"),
    re.compile(r"^/etc/sa02m-alice-devices\.conf$"),
    # HomeKit: the conf only — the pairing store is never in a backup (P4).
    re.compile(r"^/etc/sa02m-homekit/sa02m-homekit\.conf$"),
    re.compile(r"^/etc/sa02m[^/]*\.conf$"),
    re.compile(r"^/etc/sa02m_[^/]*\.conf$"),
    re.compile(r"^/etc/sa02m-device-templates/"),
    re.compile(r"^/etc/network/interfaces\.d/"),
]

def allowed(p: str) -> bool:
    return any(r.search(p) for r in ALLOW)

# A missing directory that has a declared owner/mode is created with it, not
# root:root 0755. /etc/sa02m-alice: the line in etc/tmpfiles.d/sa02m-alice.conf
# (and scripts/06-alice.sh `install -d`), which systemd-tmpfiles re-asserts every
# boot anyway; created any other way the CGI cannot save the restored confs.
DIR_SPEC = {
    "/etc/sa02m-alice": (0o770, "root", "www-data"),
    # opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf (and scripts/06c-homekit.sh).
    "/etc/sa02m-homekit": (0o770, "root", "www-data"),
}

class RestoreRefused(Exception):
    pass

# >>> trusted-path resolver — twin: etc/sa02m-web-backup.sh and
# etc/sa02m-restore-backup.sh carry this block byte-identical (row
# alice-conf-homes, case 7t); neither root script can import the other.
# Why: both run as root, and /etc/sa02m-alice is root:www-data 0770, so www-data
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

def dest_check(dest: str):
    """(why `dest` must not be written as root, or None; the path to write).
    Some ALLOW homes sit in a www-data-writable directory (/etc/sa02m-alice is
    root:www-data 0770), so a name there may be a symlink www-data planted:
    never write, chmod or chown through one. A link only root could have made
    (the installer's /etc/nginx/sites-enabled/000-sa02m-network_config) is
    written through to its target, as the restore always did."""
    try:
        target, _ = resolve_trusted(dest)
    except Unsafe as e:
        return f"{e} — refusing to write {dest} (remove it and re-run)", dest
    dest_p = Path(target)
    parent = dest_p.parent
    try:
        pst = os.lstat(parent)
    except FileNotFoundError:
        spec = DIR_SPEC.get(str(parent))
        if spec is not None:
            try:
                pwd.getpwnam(spec[1])
                grp.getgrnam(spec[2])
            except KeyError:
                return (f"{parent} is missing and its owner {spec[1]}:{spec[2]} does not exist "
                        "on this board — run the installer first"), target
        return None, target
    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
        return f"{parent} is a symlink or not a directory — refusing to write {dest_p.name} into it", target
    try:
        st = os.lstat(dest_p)
    except FileNotFoundError:
        return None, target
    if stat.S_ISLNK(st.st_mode):
        return f"{target} is a symlink — refusing to write through it (remove it and re-run)", target
    if not stat.S_ISREG(st.st_mode):
        return f"{target} exists and is not a regular file — refusing to replace it", target
    return None, target

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
    why, _ = dest_check(dest)
    if why:
        errors.append(why)
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

def ensure_parent(parent: Path) -> None:
    if os.path.lexists(parent):
        return
    spec = DIR_SPEC.get(str(parent))
    if spec is None:
        parent.mkdir(parents=True, exist_ok=True)
        return
    mode, owner, group = spec
    parent.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(parent, 0o700)
    os.chown(parent, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)
    os.chmod(parent, mode)

def atomic_install(src: Path, dest: str, fmode: int) -> None:
    ensure_parent(Path(dest).parent)
    # Re-checked here, not only in the preflight: www-data can plant a name
    # between the two. After this, root touches the directory only by
    # O_EXCL|O_NOFOLLOW create (mkstemp), fd operations and rename — none of
    # them follows a symlink planted later.
    why, target = dest_check(dest)
    if why is not None: raise RestoreRefused(why)
    dest_p = Path(target)
    # The archive records mode, not owner. Keep the owner the board already
    # gives this path (/etc/sa02m_web.env and the Alice confs are root:www-data:
    # root:root there locks the CGI out); a new file is root with its
    # directory's group (/etc/sa02m-alice is root:www-data 0770).
    try:
        st = os.lstat(dest_p)
        uid, gid = st.st_uid, st.st_gid
    except FileNotFoundError:
        uid, gid = 0, os.lstat(dest_p.parent).st_gid
    fd, tmp = tempfile.mkstemp(dir=str(dest_p.parent), prefix=f".{dest_p.name}.", suffix=".restore")
    try:
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
            out.flush()
            try:
                os.fchown(out.fileno(), uid, gid)
            except OSError as e:
                print(f"WARN: {dest_p}: could not keep owner {uid}:{gid} ({e.strerror}) — the CGI may lose write access", file=sys.stderr)
            os.fchmod(out.fileno(), fmode)
            os.fsync(out.fileno())
        os.replace(tmp, dest_p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(dest_p.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

restored = []
for dest, src, fmode in entries:
    try:
        atomic_install(src, dest, fmode)
    except RestoreRefused as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(f"ERROR: restore stopped; restored before it: {restored or 'nothing'}"
              f" (pre-restore snapshot above)", file=sys.stderr)
        sys.exit(1)
    restored.append(dest)
    print(f"restored {dest}")

def run(cmd, timeout=None):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"WARN: timed out after {timeout}s: {' '.join(cmd)}", file=sys.stderr)
        return 124
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
# The Alice units read the client conf at start (the in-place config_watch
# covers only the device document, and only while connected), so a restored
# conf takes effect on a restart. Restart only a unit that is already running —
# never start an opt-in unit the operator has stopped (same set and rule as the
# OTA runner's restart_if_active).
if any(d.startswith(("/etc/sa02m-alice/", "/etc/sa02m-alice-")) for d in restored):
    for unit in ("sa02m-alice-client", "sa02m-alice-config", "sa02m-cloud-control"):
        if run(["systemctl", "is-active", "--quiet", unit], timeout=10) != 0:
            continue
        if run(["systemctl", "restart", unit], timeout=30) != 0:
            print(f"ERROR: {unit}: restart failed — the restored Alice conf is not live; see systemctl status {unit}", file=sys.stderr)
            rc = 1
        else:
            print(f"restarted {unit} (restored Alice conf)")
# No HomeKit restart: the bridge re-reads its conf every 2 s and applies
# enabled/interface/port itself (docs/contracts/homekit-bridge.md §14).
print("health: restore complete (no reboot)")
sys.exit(rc)
PY
