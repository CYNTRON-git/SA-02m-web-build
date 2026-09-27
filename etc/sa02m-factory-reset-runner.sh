#!/bin/bash
# SA-02m config-only factory reset — installed as
# /usr/local/libexec/sa02m-factory-reset-runner
#
# Semantics: signed defaults + allowlist wipe. NEVER rootfs/eMMC/FIT/self-flash.
# Stages: validating|backing_up|confirmed|wipe|apply|verify|done|rolling_back|rolled_back|error
#
# shellcheck shell=bash
set -euo pipefail

STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
LOCKFILE="${SA02M_UPDATE_LOCK:-$STATEDIR/update.lock}"
LOGFILE="${SA02M_FACTORY_LOG:-$STATEDIR/factory-reset.log}"
TXN_JSON="$STATEDIR/transaction.json"
IMAGING_LOCK=/run/sa02m-imaging.lock
# RuntimeWatchdogUSec (µs) READ BACK from the manager before the reset window
# held it off; empty = nothing was held, so nothing is restored. The guard this
# replaced wrote RuntimeWatchdogSec=0 in and a hardcoded 15s out, checked
# neither, and logged the hold either way.
RUNTIME_WDT_PREV=""
IMAGING_HELD=0
DEFAULTS_ROOT="${SA02M_FACTORY_DEFAULTS_ROOT:-/usr/share/sa02m-factory-defaults}"
BACKUP_BIN="${SA02M_WEB_BACKUP:-/usr/local/sbin/sa02m-web-backup.sh}"
LISTS_DIR_FALLBACK="/etc/sa02m-factory-defaults/lists"
CONFIRM_PHRASE="SA02M-RESET"

# HomeKit bridge clear-list on factory reset (Operator decision Q-F: ERASE).
# Home of the list: docs/contracts/homekit-bridge.md §14 + image-identity-reset.md
# §7 — the store's contents go, the directories, the package and the unit stay.
HK_UNIT=sa02m-homekit.service
HK_VAR_DIR=/var/lib/sa02m-homekit
HK_RUN_DIR=/run/sa02m-homekit
HK_CONF=/etc/sa02m-homekit/sa02m-homekit.conf
HK_PKG_DIR=/opt/sa02m-homekit

SELF="${BASH_SOURCE[0]:-$0}"
CMD="${1:-run}"

# --- root file operations (fr_safe) ------------------------------------------
# This runner is root and writes into directories others can write:
# /etc/sa02m-alice and /etc/sa02m-homekit are root:www-data 0770 (any panel
# session plants any name there through cmd_exec.cgi), /var/lib/sa02m-homekit
# belongs to the bridge daemon, and $STATEDIR is 0775 root:www-data (tmpfiles)
# until prepare-statedir takes it. So nothing here chowns, chmods, seds or
# copies BY NAME into those directories: every such write goes through fr_safe
# — mkstemp (O_EXCL|O_NOFOLLOW) in the directory, owner/mode on the fd, fsync,
# rename over; a symlinked or non-regular dest, or a symlink/foreign directory
# anywhere on the path, is refused. The same discipline as
# etc/sa02m-restore-backup.sh; the trusted-path resolver below is a THIRD
# byte-identical copy of the block in etc/sa02m-web-backup.sh and
# etc/sa02m-restore-backup.sh (none of the three root scripts can import
# another: they run standalone on the board). Its identity with the backup's
# copy, and every fr_safe verb, are pinned by scripts/dev/test-factory-reset-runner.py.
IFS= read -r -d '' FR_SAFE_PY <<'PY' || true
import grp, gzip, json, os, pwd, re, stat, sys, tarfile, tempfile

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

class Refused(Exception):
    pass

# A missing conf directory is created with the owner/mode its tmpfiles line
# gives it (etc/tmpfiles.d/sa02m-alice.conf, sa02m-homekit.conf), never
# root:root 0755 — the CGI could not save its conf into that.
DIR_SPEC = {
    "/etc/sa02m-alice": (0o770, "root", "www-data"),
    "/etc/sa02m-homekit": (0o770, "root", "www-data"),
}
NOFOLLOW_RD = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
DIR_RD = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def warn(msg):
    print(f"WARN: {msg}", file=sys.stderr)


def owner_ids(spec):
    """'user:group' or 'uid:gid' -> (uid, gid); an unknown name -> None."""
    if not spec:
        return None
    user, _, group = spec.partition(":")
    try:
        uid = int(user) if user.isdigit() else pwd.getpwnam(user).pw_uid
        gid = int(group) if group.isdigit() else grp.getgrnam(group or user).gr_gid
    except KeyError as e:
        warn(f"owner {spec}: {e} does not exist here — keeping root")
        return None
    return uid, gid


def fsync_dir(d):
    dfd = os.open(d, DIR_RD)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def ensure_parent(parent):
    if os.path.lexists(parent):
        return
    resolve_trusted(parent)  # every directory above it pinned, or Unsafe
    spec = DIR_SPEC.get(parent)
    os.makedirs(os.path.dirname(parent), exist_ok=True)
    if spec is None:
        os.mkdir(parent, 0o755)
        return
    mode, owner, group = spec
    os.mkdir(parent, 0o700)
    ids = owner_ids(f"{owner}:{group}")
    if ids is not None:
        os.chown(parent, *ids, follow_symlinks=False)
    os.chmod(parent, mode)


def target_of(dest):
    """(path to write, its lstat or None). Refused when writing `dest` as
    root could follow a name somebody else planted: a symlink or directory in
    a directory others can write anywhere on the way, a symlinked or
    non-directory parent, a symlinked or non-regular dest."""
    try:
        target, _ = resolve_trusted(dest)
    except Unsafe as e:
        raise Refused(f"{e} — refusing to write {dest} (remove it and re-run)")
    parent = os.path.dirname(target)
    try:
        pst = os.lstat(parent)
    except FileNotFoundError:
        return target, None
    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
        raise Refused(f"{parent} is a symlink or not a directory — refusing to write {os.path.basename(target)} into it")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return target, None
    if stat.S_ISLNK(st.st_mode): raise Refused(f"{target} is a symlink — refusing to write through it (remove it and re-run)")
    if not stat.S_ISREG(st.st_mode): raise Refused(f"{target} exists and is not a regular file — refusing to replace it")
    return target, st


def replace_with(target, fill, mode, ids):
    """Replace `target` by a new file: created O_EXCL|O_NOFOLLOW (mkstemp) in
    the same directory, owner and mode set on the fd, fsync, rename over.
    Nothing here follows a name planted later — rename replaces a symlink,
    never its target."""
    d, name = os.path.split(target)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.", suffix=".factory")
    try:
        with os.fdopen(fd, "wb") as out:
            fill(out)
            out.flush()
            if ids is not None:
                try:
                    os.fchown(out.fileno(), *ids)
                except OSError as e:
                    warn(f"{target}: could not set owner {ids[0]}:{ids[1]} ({e.strerror})")
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
            ours = os.fstat(out.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Where others can write the directory they can swap the temp name for a
    # symlink between mkstemp and the rename; the rename then lands THEIR
    # entry at `target`. Never leave that in place.
    st = os.lstat(target)
    if (st.st_dev, st.st_ino) != (ours.st_dev, ours.st_ino): drop_swapped(target)
    fsync_dir(d)


def drop_swapped(target):
    try:
        os.unlink(target)
    except OSError:
        pass
    raise Refused(f"{target}: the temp file was swapped before the rename — removed what was put in its place")


def copy_from(src_fd):
    def fill(out):
        while True:
            chunk = os.read(src_fd, 1 << 20)
            if not chunk:
                return
            out.write(chunk)
    return fill


def open_regular(path, expect=None):
    fd = os.open(path, NOFOLLOW_RD)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or (expect is not None and (st.st_dev, st.st_ino) != (expect.st_dev, expect.st_ino)):
        os.close(fd)
        raise Refused(f"{path} changed under the runner or is not a regular file")
    return fd, st


def cmd_install(src, dest, mode, owner):
    """A template onto a live conf path (atomic_install_file)."""
    ensure_parent(os.path.dirname(dest))
    target, st = target_of(dest)
    ids = owner_ids(owner)
    if ids is None and st is not None:
        ids = (st.st_uid, st.st_gid)
    sfd, _ = open_regular(src)
    try:
        replace_with(target, copy_from(sfd), int(mode, 8), ids)
    finally:
        os.close(sfd)


def cmd_restore(src, dest):
    """A journalled copy back onto its live path (rollback_from_journal):
    owner and mode come from the journal copy (cp -a kept them)."""
    ensure_parent(os.path.dirname(dest))
    target, _ = target_of(dest)
    sfd, sst = open_regular(src)
    try:
        replace_with(target, copy_from(sfd), stat.S_IMODE(sst.st_mode), (sst.st_uid, sst.st_gid))
    finally:
        os.close(sfd)


def cmd_force_key(dest, key, value):
    """`key = value` on every `key =` line of an INI conf, rewritten through a
    new file (never sed -i: that reads through a planted symlink)."""
    target, st = target_of(dest)
    if st is None:
        return
    fd, fst = open_regular(target, st)
    try:
        data = b""
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    text = data.decode("utf-8", "surrogateescape")
    pat = re.compile(r"^[ \t\r\f\v]*" + re.escape(key) + r"[ \t\r\f\v]*=.*$", re.M)
    new = pat.sub(f"{key} = {value}", text)
    if new == text:
        return
    body = new.encode("utf-8", "surrogateescape")
    replace_with(target, lambda out: out.write(body), stat.S_IMODE(fst.st_mode), (fst.st_uid, fst.st_gid))


def in_root_only_dir(path):
    real, dir_ro = resolve_trusted(path)
    if not dir_ro:
        raise Refused(f"{os.path.dirname(real)} is writable by others — refusing to use {real}")
    return real


def cmd_write_new(out):
    """stdin -> a NEW file only root can read (the mandatory backup)."""
    real = in_root_only_dir(out)
    fd = os.open(real, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        src = sys.stdin.buffer
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            os.write(fd, chunk)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(real)
        raise
    os.close(fd)
    fsync_dir(os.path.dirname(real))


def cmd_discard(path):
    real = in_root_only_dir(path)
    try:
        os.unlink(real)
    except FileNotFoundError:
        pass


def cmd_verify_backup(path):
    """The archive sa02m-web-backup.sh streams: a complete gzip (CRC checked
    to the end), manifest first, every manifest entry present."""
    real = in_root_only_dir(path)
    fd, st = open_regular(real)
    with os.fdopen(fd, "rb") as f:
        if st.st_size == 0:
            raise Refused(f"{real} is empty")
        with gzip.GzipFile(fileobj=f, mode="rb") as g:
            while g.read(1 << 20):
                pass
        f.seek(0)
        with tarfile.open(fileobj=f, mode="r:gz") as tar:
            members = tar.getmembers()
            if not members or members[0].name != "backup-manifest.json":
                raise Refused(f"{real}: first member is not backup-manifest.json")
            manifest = json.load(tar.extractfile(members[0]))
            names = {m.name for m in members}
            paths = manifest.get("paths")
            if manifest.get("schema_version") != 1 or not isinstance(paths, list):
                raise Refused(f"{real}: manifest is not a schema 1 backup manifest")
            missing = [p.get("archive_path") for p in paths if p.get("archive_path") not in names]
            if missing:
                raise Refused(f"{real}: manifest entries missing from the archive: {missing[:5]}")
    print(f"backup verified: {len(paths)} file(s), {st.st_size} bytes")


def cmd_check_dir(path):
    """A directory the runner clears with find/rm: a real directory whose own
    name nobody else can replace."""
    real = in_root_only_dir(path.rstrip("/") or "/")
    st = os.lstat(real)
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{real} is not a directory")


def rm_at(dfd, name):
    st = os.lstat(name, dir_fd=dfd)
    if stat.S_ISDIR(st.st_mode):
        sub = os.open(name, DIR_RD, dir_fd=dfd)
        try:
            for n in os.listdir(sub):
                rm_at(sub, n)
        finally:
            os.close(sub)
        os.rmdir(name, dir_fd=dfd)
    else:
        os.unlink(name, dir_fd=dfd)


def cmd_wipe_dir(path):
    """Every entry of a daemon-owned directory, the directory itself kept.
    Only fd-relative unlink/rmdir: a symlink inside is removed, never followed."""
    real = in_root_only_dir(path)
    dfd = os.open(real, DIR_RD)
    try:
        names = os.listdir(dfd)
        for n in names:
            rm_at(dfd, n)
        left = os.listdir(dfd)
        if left:
            raise Refused(f"{real} still holds {left[:5]} after the wipe")
        os.fsync(dfd)
    finally:
        os.close(dfd)
    print(f"erased {len(names)} entr{'y' if len(names) == 1 else 'ies'} of {real}")


def cmd_remove_name(dirpath, name):
    try:
        real = in_root_only_dir(dirpath)
        dfd = os.open(real, DIR_RD)
    except FileNotFoundError:
        return
    try:
        os.unlink(name, dir_fd=dfd)
        print(f"removed {real}/{name}")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        pass
    finally:
        os.close(dfd)


def package_template(pkg_dir):
    """The bridge conf template, from its one home: the installed package's
    own render() of the defaults (== etc/sa02m-homekit/sa02m-homekit.conf, the
    installer's seed). Imported as root only from directories only root can
    write; None when the package is absent or not trusted."""
    for d in (pkg_dir, os.path.join(pkg_dir, "sa02m_homekit"), os.path.join(pkg_dir, "sa02m_homekit", "__pycache__")):
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            if d.endswith("__pycache__"):
                continue
            return None
        if not root_only(st):
            warn(f"{d} is not root-only — not importing the HomeKit package as root")
            return None
    sys.dont_write_bytecode = True
    sys.path.insert(0, pkg_dir)
    try:
        from sa02m_homekit import config
        return config.render(config.BridgeConfig()).encode("utf-8")
    except Exception as e:  # noqa: BLE001 — any import failure means "no template"
        warn(f"HomeKit package template unavailable ({e})")
        return None


def cmd_hk_reset_conf(conf, pkg_dir, owner):
    target, st = target_of(conf)
    if st is None:
        return
    body = package_template(pkg_dir)
    if body is None:
        warn(f"{target}: no template — forcing enabled = false only")
        cmd_force_key(conf, "enabled", "false")
        return
    replace_with(target, lambda out: out.write(body), 0o660, owner_ids(owner) or (st.st_uid, st.st_gid))
    print(f"reset {target} to the package template (enabled = false)")


def drop_foreign(pfd, dreal, name, lst, kind):
    os.unlink(name, dir_fd=pfd)
    print(f"removed {dreal}/{name}: {stat.filemode(lst.st_mode)}, uid {lst.st_uid}, "
          f"{lst.st_nlink} link(s) — not the runner's {kind}")


def scrub_log(fd, where):
    os.ftruncate(fd, 0)
    os.write(fd, b"log truncated by the factory-reset runner: it held a config backup "
                 b"stream (an older runner appended the archive here)\n")
    os.fsync(fd)
    print(f"truncated {where}: it held a config backup stream")


def cmd_prepare_statedir(statedir, logfile, lockfile, txnfile):
    """The state dir is group-writable for the CGI (tmpfiles 0775 root:www-data)
    until this runner takes it: fchmod 0755 first, so from here on no name in
    it can be created, renamed or swapped by anybody else; then every name the
    runner opens by path is checked once. rc 3: the dir itself is unusable;
    rc 4: an entry in it is not the runner's own."""
    try:
        real, parent_ro = resolve_trusted(statedir)
    except Unsafe as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        sys.exit(3)
    if not parent_ro:
        print(f"REFUSED: {os.path.dirname(real)} is writable by others", file=sys.stderr)
        sys.exit(3)
    if not os.path.lexists(real):
        os.mkdir(real, 0o755)
    dfd = os.open(real, DIR_RD)
    try:
        st = os.fstat(dfd)
        if st.st_uid not in TRUSTED_UIDS:
            print(f"REFUSED: {real} is owned by uid {st.st_uid}", file=sys.stderr)
            sys.exit(3)
        os.fchmod(dfd, 0o755)
        bad = []
        for name, mode in (("backup-export", 0o700), ("rollback", 0o750), ("runner", 0o750),
                           ("staging", 0o750), ("state", 0o750)):
            try:
                est = os.lstat(name, dir_fd=dfd)
            except FileNotFoundError:
                os.mkdir(name, mode, dir_fd=dfd)
                est = os.lstat(name, dir_fd=dfd)
            if not stat.S_ISDIR(est.st_mode) or est.st_uid not in TRUSTED_UIDS:
                bad.append(f"{real}/{name} ({stat.filemode(est.st_mode)}, uid {est.st_uid})")
                continue
            sub = os.open(name, DIR_RD, dir_fd=dfd)
            try:
                cur = stat.S_IMODE(os.fstat(sub).st_mode)
                want = 0o700 if name == "backup-export" else cur & ~0o022
                if cur != want:
                    os.fchmod(sub, want)
            finally:
                os.close(sub)
        if bad:
            print("REFUSED: not the runner's own directory — remove and re-run: " + ", ".join(bad), file=sys.stderr)
            sys.exit(4)
    finally:
        os.close(dfd)
    # The log, the lock and the transaction are opened by NAME later (bash
    # redirections, python open), so a name planted while the dir was still
    # group-writable is dealt with here, by lstat — never resolved: a link
    # www-data made before the fchmod above would look root-made now.
    for path, kind in ((logfile, "log"), (lockfile, "lock"), (txnfile, "txn")):
        d, name = os.path.split(path)
        try:
            dreal, _ = resolve_trusted(d)
            if not root_only(os.lstat(dreal)):
                raise Refused(f"{dreal} is writable by others — refusing to use {name} in it")
            pfd = os.open(dreal, DIR_RD)
        except (Unsafe, Refused, OSError) as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            sys.exit(4)
        try:
            try:
                lst = os.lstat(name, dir_fd=pfd)
                # The lock and the transaction may be the CGI's own regular
                # files; the log is the runner's alone.
                foreign = (not stat.S_ISREG(lst.st_mode) or lst.st_nlink != 1
                           or (kind == "log" and lst.st_uid not in TRUSTED_UIDS))
                if foreign: drop_foreign(pfd, dreal, name, lst, kind)
            except FileNotFoundError:
                pass
            if kind != "log":
                continue
            fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=pfd)
            try:
                os.fchmod(fd, 0o600)
                if os.geteuid() == 0:
                    os.fchown(fd, 0, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                dirty, tail = False, b""
                while True:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        break
                    if b"\x00" in chunk or b"\x1f\x8b\x08" in tail + chunk:
                        dirty = True
                        break
                    tail = chunk[-2:]
                if dirty: scrub_log(fd, f"{dreal}/{name}")
            finally:
                os.close(fd)
        finally:
            os.close(pfd)


def main(argv):
    cmds = {
        "prepare-statedir": (cmd_prepare_statedir, 4),
        "install": (cmd_install, 4),
        "restore": (cmd_restore, 2),
        "force-key": (cmd_force_key, 3),
        "write-new": (cmd_write_new, 1),
        "discard": (cmd_discard, 1),
        "verify-backup": (cmd_verify_backup, 1),
        "check-dir": (cmd_check_dir, 1),
        "wipe-dir": (cmd_wipe_dir, 1),
        "remove-name": (cmd_remove_name, 2),
        "hk-reset-conf": (cmd_hk_reset_conf, 3),
    }
    if not argv or argv[0] not in cmds or len(argv) - 1 != cmds[argv[0]][1]:
        print(f"usage: fr_safe {'|'.join(cmds)} ARGS", file=sys.stderr)
        return 2
    try:
        cmds[argv[0]][0](*argv[1:])
    except (Refused, Unsafe, OSError, tarfile.TarError, EOFError, ValueError) as e:
        print(f"REFUSED: {argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0


sys.exit(main(sys.argv[1:]))
PY
fr_safe() { python3 -I -B -c "$FR_SAFE_PY" "$@"; }

# Before anything is logged: the log, the lock and the transaction are names in
# $STATEDIR. rc 3 = the dir itself unusable (nothing can be recorded there);
# rc 4 = an entry in it is not the runner's own (recorded in the transaction).
set +e
_prep_out=$(fr_safe prepare-statedir "$STATEDIR" "$LOGFILE" "$LOCKFILE" "$TXN_JSON" 2>&1)
_prep_rc=$?
set -e
if [ "$_prep_rc" -ne 0 ]; then
  printf 'sa02m-factory-reset: %s\n' "$_prep_out" >&2
  if [ "$_prep_rc" -eq 4 ]; then
    python3 - "$TXN_JSON" "${_prep_out//$'\n'/ }" <<'PY' || true
import json, os, sys, tempfile, time
path, msg = sys.argv[1], sys.argv[2][:500]
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as f:
        txn = json.load(f)
except (OSError, ValueError):
    txn = {}
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
txn.update(stage="error", result="failed", error_code="E_INTERNAL", error_message=msg,
           updated_at=now, finished_at=now)
fd, tmp = tempfile.mkstemp(prefix=".txn.", dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(txn, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write("\n")
    os.fchmod(f.fileno(), 0o644)
os.replace(tmp, path)
PY
  fi
  exit 1
fi

log() {
  local ts
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  printf '%s %s\n' "$ts" "$*" | tee -a "$LOGFILE" >/dev/null
}
[ -z "$_prep_out" ] || log "state dir: ${_prep_out//$'\n'/; }"
unset _prep_out _prep_rc

iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- imaging lock (same policy as update runner) -----------------------------

# ── BEGIN sa02m-runtime-watchdog (shared block — keep BYTE-IDENTICAL) ──────
# One home for "hold the systemd manager's hardware watchdog off while the
# live filesystem is being rewritten". Three callers cannot share a file:
# install.sh sources scripts/lib.sh out of an extracted tree, while
# etc/sa02m-update-runner.sh and etc/sa02m-factory-reset-runner.sh run
# standalone on the device, where scripts/ is not deployed. So the block is
# duplicated by construction and pinned byte-for-byte by
# scripts/dev/test-watchdog-hold.sh (the `cmp` idiom
# .ai-dev/quality/checks/watchdog-cap.sh already uses for the policy file).
# Nothing here logs: the three callers have different log() signatures — each
# logs what it got back.
#
# Policy home of the value being held off: etc/systemd/sa02m-watchdog.conf
# `[Manager] RuntimeWatchdogSec=15s` → /etc/systemd/system.conf.d/. At runtime
# the manager exposes it as the D-Bus property RuntimeWatchdogUSec (µs); on the
# bench board it reads 15000000. That property is WRITABLE only on systemd
# >= 250 — on an older manager `systemctl set-property --runtime Manager …`
# and the bus write are both silent no-ops. Nothing here trusts a write: the
# value is READ BACK and the caller is told what is really in force (the
# previous guard, etc/sa02m-update-runner.sh:216 before 1.0.6.41, logged
# «RuntimeWatchdogSec=0» without ever checking — quality-gate-rigor.md).
# The bus write is an OVERRIDE, so it survives the `daemon-reload` the modules
# do; restoring writes the previous value back as an override (the configured
# policy applies again from the next boot).
# `systemctl daemon-reexec` would also re-read the config and is deliberately
# NOT used here: re-execing PID 1 mid-install is a PID-1 event during the very
# window this hold protects, and there is no reason to add one when a runtime
# override does the job. It is NOT the incident's cause: D4 excludes it by
# timing, and the row that USED to lead there - the HW watchdog after a PID-1
# stall - is now excluded too, on the board (docs/bugs/bench-136-reset.md, D4
# addendum). Measured on 1.136, 2026-09-09: taking this hold makes PID 1 CLOSE
# /dev/watchdog0, so the timer is disarmed rather than merely unfed - the board
# then survives 40 s past its 16 s hardware timeout, and survives it again
# across a daemon-reexec, which is also why 01-system.sh:762 cannot undo the
# hold. So this helper is not what keeps the board alive during an install;
# what it does keep is the report honest.
sa02m_runtime_watchdog_usec() {   # prints RuntimeWatchdogUSec in µs; rc=1 when unreadable
    local v=""
    command -v busctl >/dev/null 2>&1 || return 1
    v=$(busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1 \
            org.freedesktop.systemd1.Manager RuntimeWatchdogUSec 2>/dev/null) || return 1
    v=${v##* }                      # "t 15000000" → "15000000"
    case "$v" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$v"
}

# <µs> → prints the value ACTUALLY in force after the attempt; rc=0 ONLY when
# that equals the requested one, rc=1 when the manager cannot be read at all
# (no busctl, chroot, dead D-Bus), rc=2 on a bad argument.
sa02m_runtime_watchdog_set() {
    local want=${1:-} now
    case "$want" in ''|*[!0-9]*) return 2 ;; esac
    now=$(sa02m_runtime_watchdog_usec) || return 1
    if [ "$now" != "$want" ] && command -v busctl >/dev/null 2>&1; then
        busctl set-property org.freedesktop.systemd1 /org/freedesktop/systemd1 \
            org.freedesktop.systemd1.Manager RuntimeWatchdogUSec t "$want" >/dev/null 2>&1 || true
        now=$(sa02m_runtime_watchdog_usec) || return 1
    fi
    if [ "$now" != "$want" ] && command -v systemctl >/dev/null 2>&1; then
        systemctl set-property --runtime Manager "RuntimeWatchdogSec=${want}us" >/dev/null 2>&1 || true
        now=$(sa02m_runtime_watchdog_usec) || return 1
    fi
    printf '%s\n' "$now"
    [ "$now" = "$want" ]
}
# ── END sa02m-runtime-watchdog ─────────────────────────────────────────────

install_imaging_lock() {
  date -Iseconds >"$IMAGING_LOCK"
  sync
  systemctl stop net-watchdog sa02m-watchdog-feed 2>/dev/null || true
  local now=""
  RUNTIME_WDT_PREV=$(sa02m_runtime_watchdog_usec 2>/dev/null) || RUNTIME_WDT_PREV=""
  if [ -n "$RUNTIME_WDT_PREV" ] && [ "$RUNTIME_WDT_PREV" != 0 ]; then
    if now=$(sa02m_runtime_watchdog_set 0); then
      log "imaging lock installed ($IMAGING_LOCK); runtime watchdog held off (was ${RUNTIME_WDT_PREV}us, now ${now}us)"
    else
      RUNTIME_WDT_PREV=""
      log "imaging lock installed ($IMAGING_LOCK); WARNING: runtime watchdog still ${now:-unknown}us — the manager refused the change, the reset runs with it armed"
    fi
  else
    log "imaging lock installed ($IMAGING_LOCK); runtime watchdog ${RUNTIME_WDT_PREV:-unreadable} — nothing to hold off"
  fi
  IMAGING_HELD=1
}

# Idempotent: the ERR trap, fail(), the success path and the EXIT trap can all
# reach it, and only the first call has a hold to give back.
cleanup_imaging_lock() {
  local now="" wdt="not held"
  if [ -n "$RUNTIME_WDT_PREV" ]; then
    if now=$(sa02m_runtime_watchdog_set "$RUNTIME_WDT_PREV"); then
      wdt="restored to ${now}us"
    else
      wdt="RESTORE FAILED: wanted ${RUNTIME_WDT_PREV}us, in force ${now:-unknown}us"
    fi
    RUNTIME_WDT_PREV=""
  fi
  systemctl start net-watchdog 2>/dev/null || true
  systemctl start sa02m-watchdog-feed 2>/dev/null || true
  rm -f "$IMAGING_LOCK"
  IMAGING_HELD=0
  log "imaging lock cleared; runtime watchdog $wdt"
}

# --- transaction journal (temp → fsync → rename; no Python dependency) -------
txn_write() {
  # Usage: txn_write key=value ...
  # Merges into existing transaction.json via python3 when available.
  local args=("$@")
  python3 - "$TXN_JSON" "${args[@]}" <<'PY'
import json, os, sys, tempfile, time
path = sys.argv[1]
fields = {}
for a in sys.argv[2:]:
    if "=" not in a:
        continue
    k, v = a.split("=", 1)
    if v in ("true", "True"):
        fields[k] = True
    elif v in ("false", "False"):
        fields[k] = False
    elif v.isdigit():
        fields[k] = int(v)
    elif v == "null":
        fields[k] = None
    else:
        fields[k] = v
txn = {}
# O_NOFOLLOW: the CGI (www-data) writes this file, so a symlink here is never
# ours to read through (its target would be merged into a 0644 file).
try:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8") as f:
        txn = json.load(f)
except FileNotFoundError:
    pass
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
txn.setdefault("schema_version", 1)
txn.setdefault("operation", "factory_reset")
txn.setdefault("id", __import__("uuid").uuid4().hex)
txn.update(fields)
txn["updated_at"] = now
if fields.get("stage") in ("done", "error", "rolled_back"):
    txn["finished_at"] = now
    if fields.get("stage") == "done":
        txn["result"] = "success"
    elif fields.get("stage") == "rolled_back":
        txn["result"] = "rolled_back"
    elif fields.get("stage") == "error":
        txn.setdefault("result", "failed")
data = json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(prefix=".txn.", dir=d)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        # mkstemp makes 0600; the status CGI reads this as www-data (the
        # update runner's transaction.py writes it 0644 too).
        os.fchmod(f.fileno(), 0o644)
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}

txn_get() {
  local key=$1
  python3 -c 'import json,os,sys; t=json.load(os.fdopen(os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW),encoding="utf-8")); v=t.get(sys.argv[2]); print("" if v is None else v)' \
    "$TXN_JSON" "$key" 2>/dev/null || true
}

fail() {
  local code=$1 msg=$2
  log "ERROR $code: $msg"
  if [ -n "${JOURNAL_DIR:-}" ] && [ -d "${JOURNAL_DIR:-}" ] && [ "${SA02M_FACTORY_ROLLING:-}" != "1" ]; then
    SA02M_FACTORY_ROLLING=1 rollback_from_journal || true
  fi
  txn_write "stage=error" "error_code=$code" "error_message=$msg" "result=failed" || true
  cleanup_imaging_lock || true
  exit 1
}

# --- preserve / wipe lists ---------------------------------------------------
load_lists() {
  local ver_dir wipe_f preserve_f
  ver_dir=$(resolve_defaults_dir)
  wipe_f="$ver_dir/lists/wipe.list"
  preserve_f="$ver_dir/lists/preserve.list"
  if [ ! -f "$wipe_f" ]; then
    wipe_f="$LISTS_DIR_FALLBACK/wipe.list"
  fi
  if [ ! -f "$preserve_f" ]; then
    preserve_f="$LISTS_DIR_FALLBACK/preserve.list"
  fi
  [ -f "$wipe_f" ] || fail E_INTERNAL "wipe.list missing"
  [ -f "$preserve_f" ] || fail E_INTERNAL "preserve.list missing"
  mapfile -t WIPE_LIST < <(grep -vE '^\s*(#|$)' "$wipe_f" || true)
  mapfile -t PRESERVE_LIST < <(grep -vE '^\s*(#|$)' "$preserve_f" || true)
}

path_matches_glob() {
  # $1=path $2=pattern (supports trailing / for prefix, or single * in basename)
  local path=$1 pat=$2
  case "$pat" in
    */)
      [[ "$path" == "$pat"* || "$path/" == "$pat"* ]] && return 0
      return 1
      ;;
    *\**)
      # A '*' in the BASENAME only (the lists' header): the directory must be
      # the same, and the glob never spans a '/'. This arm used to read `*\*`
      # — "ends in *" — so /etc/network/interfaces.d/*.conf matched nothing
      # and every reset stopped at "dst not on wipe allowlist: …/eth0.conf".
      [ "${path%/*}" = "${pat%/*}" ] || return 1
      # shellcheck disable=SC2254
      case "${path##*/}" in
        ${pat##*/}) return 0 ;;
      esac
      return 1
      ;;
    *)
      [ "$path" = "$pat" ] && return 0
      return 1
      ;;
  esac
}

is_preserved() {
  local path=$1 p
  for p in "${PRESERVE_LIST[@]}"; do
    path_matches_glob "$path" "$p" && return 0
  done
  # Absolute hard denies
  case "$path" in
    /dev/*|/boot/*|/proc/*|/sys/*) return 0 ;;
  esac
  return 1
}

is_wipe_allowed() {
  local path=$1 p
  is_preserved "$path" && return 1
  for p in "${WIPE_LIST[@]}"; do
    path_matches_glob "$path" "$p" && return 0
  done
  return 1
}

resolve_defaults_dir() {
  local ver bundled
  ver=$(tr -d '\r' </var/www/network_config/VERSION 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
  if [ -n "$ver" ] && [ -d "$DEFAULTS_ROOT/$ver" ]; then
    printf '%s\n' "$DEFAULTS_ROOT/$ver"
    return 0
  fi
  bundled=$(ls -1d "$DEFAULTS_ROOT"/*/ 2>/dev/null | sort -V | tail -1 || true)
  if [ -n "$bundled" ]; then
    printf '%s\n' "${bundled%/}"
    return 0
  fi
  if [ -d /etc/sa02m-factory-defaults/templates ]; then
    printf '%s\n' /etc/sa02m-factory-defaults
    return 0
  fi
  return 1
}

# --- path safety: never touch block devices / use tar -C / -------------------
assert_safe_dst() {
  local dst=$1
  case "$dst" in
    /etc/*|/var/lib/sa02m-update/*) ;;
    *) fail E_APPLY "dst outside config tree: $dst" ;;
  esac
  is_preserved "$dst" && fail E_APPLY "refusing preserved path: $dst"
  is_wipe_allowed "$dst" || fail E_APPLY "dst not on wipe allowlist: $dst"
  # Refuse if path resolves to a block device
  if [ -b "$dst" ] || [ -b "$(readlink -f "$dst" 2>/dev/null || true)" ]; then
    fail E_APPLY "block device refused: $dst"
  fi
}

# Journal prior content for rollback (copy, never tar -C /). cp -a copies a
# symlink or FIFO as itself — it never reads through the name.
journal_prior() {
  local dst=$1
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    local jdir="$JOURNAL_DIR/files"
    mkdir -p "$jdir$(dirname "$dst")"
    cp -a "$dst" "$jdir$dst" 2>/dev/null || true
    printf '%s\n' "$dst" >>"$JOURNAL_DIR/touched.list"
  else
    printf '%s\n' "$dst" >>"$JOURNAL_DIR/created.list"
  fi
}

atomic_install_file() {
  local src=$1 dst=$2 mode=$3 owner=$4 out
  assert_safe_dst "$dst"
  journal_prior "$dst"
  out=$(fr_safe install "$src" "$dst" "$mode" "$owner" 2>&1) || fail E_APPLY "cannot install $dst: $out"
  [ -z "$out" ] || log "$out"
}

clear_dir_allowlisted() {
  local dir=$1 out
  assert_safe_dst "$dir"
  [ -d "$dir" ] || return 0
  # find/rm below start from this name: it must be a real directory nobody
  # else can swap for a symlink (find follows a symlinked start point).
  out=$(fr_safe check-dir "$dir" 2>&1) || fail E_APPLY "refusing to clear $dir: $out"
  local jdir="$JOURNAL_DIR/files"
  mkdir -p "$jdir$dir"
  # Backup then remove contents (not the directory node)
  if compgen -G "$dir/*" >/dev/null 2>&1; then
    cp -a "$dir/." "$jdir$dir/" 2>/dev/null || true
    printf '%s\n' "$dir" >>"$JOURNAL_DIR/cleared_dirs.list"
    find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
  fi
}

rollback_from_journal() {
  log "rolling back from journal $JOURNAL_DIR"
  txn_write "stage=rolling_back" || true
  local f
  if [ -f "$JOURNAL_DIR/created.list" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      rm -f "$f" 2>/dev/null || true
    done <"$JOURNAL_DIR/created.list"
  fi
  if [ -d "$JOURNAL_DIR/files" ]; then
    # Restore files by walking journal tree — through fr_safe (a name planted in
    # a www-data directory since the journal was taken is refused, not followed).
    local rel out
    while IFS= read -r -d '' f; do
      rel="${f#"$JOURNAL_DIR/files"}"
      [ -n "$rel" ] || continue
      out=$(fr_safe restore "$f" "$rel" 2>&1) || log "WARN rollback could not restore $rel: $out"
    done < <(find "$JOURNAL_DIR/files" -type f -print0 2>/dev/null || true)
  fi
  txn_write "stage=rolled_back" "result=rolled_back" || true
}

# --- apply templates ---------------------------------------------------------
apply_defaults() {
  local base=$1
  local t="$base/templates"
  [ -d "$t" ] || fail E_INTERNAL "templates missing under $base"

  # Wipe user overlays dir
  if [ -d /etc/sa02m-device-templates/user ]; then
    clear_dir_allowlisted /etc/sa02m-device-templates/user/
  fi

  # Clear allowlisted interfaces.d/*.conf then reinstall canonical templates only.
  local iface_conf
  if compgen -G "/etc/network/interfaces.d/*.conf" >/dev/null 2>&1; then
    for iface_conf in /etc/network/interfaces.d/*.conf; do
      [ -e "$iface_conf" ] || continue
      is_wipe_allowed "$iface_conf" || continue
      local jdir="$JOURNAL_DIR/files"
      mkdir -p "$jdir$(dirname "$iface_conf")"
      cp -a "$iface_conf" "$jdir$iface_conf" 2>/dev/null || true
      printf '%s\n' "$iface_conf" >>"$JOURNAL_DIR/touched.list"
      rm -f "$iface_conf"
    done
  fi

  # Optional files that may be absent on device: remove if wipe-allowed and no template
  local optional
  for optional in \
    /etc/sa02m_storage.conf \
    /etc/sa02m_status_blocks.conf \
    /etc/sa02m_serial_profile.conf \
    /etc/sa02m_flasher.conf \
    /etc/sa02m-mqtt-snmp.conf \
    /etc/sa02m-mqtt-opcua.conf
  do
    if [ -e "$optional" ] && is_wipe_allowed "$optional"; then
      local jdir="$JOURNAL_DIR/files"
      mkdir -p "$jdir$(dirname "$optional")"
      cp -a "$optional" "$jdir$optional" 2>/dev/null || true
      printf '%s\n' "$optional" >>"$JOURNAL_DIR/touched.list"
      rm -f "$optional"
    fi
  done

  local src dst mode owner
  while IFS=$'\t' read -r src dst mode owner; do
    [ -n "$src" ] || continue
    if [ ! -f "$t/$src" ]; then
      log "WARN missing template $src — skip"
      continue
    fi
    atomic_install_file "$t/$src" "$dst" "$mode" "$owner"
  done <<'MAP'
etc/nginx/.htpasswd	/etc/nginx/.htpasswd	0600	root:root
etc/sa02m_web.env	/etc/sa02m_web.env	0640	root:www-data
etc/network/interfaces.d/eth0.conf	/etc/network/interfaces.d/eth0.conf	0644	root:root
etc/network/interfaces.d/eth1.conf	/etc/network/interfaces.d/eth1.conf	0644	root:root
etc/sa02m_modem.conf	/etc/sa02m_modem.conf	0644	root:root
etc/sa02m_network.conf	/etc/sa02m_network.conf	0644	root:root
etc/sa02m-modbus-mqtt.yaml	/etc/sa02m-modbus-mqtt.yaml	0660	root:www-data
etc/sa02m-gateway.yaml	/etc/sa02m-gateway.yaml	0660	root:www-data
etc/sa02m-alice-client.conf	/etc/sa02m-alice-client.conf	0640	root:www-data
etc/sa02m-alice-devices.conf	/etc/sa02m-alice-devices.conf	0640	root:www-data
etc/sa02m-alice/sa02m-alice-client.conf	/etc/sa02m-alice/sa02m-alice-client.conf	0640	root:www-data
etc/sa02m-alice/sa02m-alice-devices.conf	/etc/sa02m-alice/sa02m-alice-devices.conf	0640	root:www-data
MAP

  # Alice: force client_enabled=false even if only one layout exists. Never
  # sed -i here: /etc/sa02m-alice is www-data-writable and sed reads through a
  # planted symlink; fr_safe rewrites a regular file through a new one.
  local out
  for dst in /etc/sa02m-alice-client.conf /etc/sa02m-alice/sa02m-alice-client.conf; do
    if { [ -e "$dst" ] || [ -L "$dst" ]; } && is_wipe_allowed "$dst"; then
      out=$(fr_safe force-key "$dst" client_enabled false 2>&1) || fail E_APPLY "cannot force client_enabled = false in $dst: $out"
    fi
  done
}

# HomeKit (Q-F: factory reset ERASES the bridge's pairings — resale must not
# leave the previous owner's iPhones in control of the relays). Runs LAST, after
# the reversible config part has verified, so a failed reset never costs the
# pairings for nothing; a failure here still rolls the configs back.
# Order: conf to the template first (enabled = false — a daemon restarted
# behind our back exits instead of re-persisting keys), then stop, then erase
# only once systemd reports the unit fully down.
hk_unit_stopped() {
  local st
  st=$(timeout 10 systemctl is-active "$HK_UNIT" 2>/dev/null) || true
  case "$st" in
    inactive|failed) return 0 ;;
    *) return 1 ;;
  esac
}

wipe_homekit_pairings() {
  local out
  if [ ! -e "$HK_VAR_DIR" ] && [ ! -L "$HK_VAR_DIR" ] && [ ! -e "$HK_CONF" ] && [ ! -L "$HK_CONF" ]; then
    log "homekit: bridge not installed — nothing to erase"
    return 0
  fi
  if [ -e "$HK_CONF" ] || [ -L "$HK_CONF" ]; then
    journal_prior "$HK_CONF"
    out=$(fr_safe hk-reset-conf "$HK_CONF" "$HK_PKG_DIR" root:www-data 2>&1) || fail E_APPLY "HomeKit conf not reset: $out"
    [ -z "$out" ] || log "homekit: $out"
  fi
  timeout 20 systemctl stop "$HK_UNIT" >/dev/null 2>&1 || true
  hk_unit_stopped || fail E_APPLY "HomeKit bridge did not stop — its pairings were NOT erased"
  if [ -e "$HK_VAR_DIR" ] || [ -L "$HK_VAR_DIR" ]; then
    out=$(fr_safe wipe-dir "$HK_VAR_DIR" 2>&1) || fail E_APPLY "HomeKit pairing store not erased: $out"
    log "homekit: $out"
  fi
  # A stopped daemon cannot withdraw its own setup code (same as the card's
  # disable, usr/local/sbin/sa02m-homekit-web-trigger.sh).
  out=$(fr_safe remove-name "$HK_RUN_DIR" setup.json 2>&1) || log "WARN homekit: $out"
  [ -z "$out" ] || log "homekit: $out"
  log "homekit: pairings erased, conf at the template (docs/contracts/homekit-bridge.md §14)"
}

verify_reset() {
  [ -f /etc/nginx/.htpasswd ] || fail E_HEALTH "htpasswd missing after reset"
  grep -q '^admin:' /etc/nginx/.htpasswd || fail E_HEALTH "admin htpasswd line missing"
  if [ -f /etc/sa02m_web.env ]; then
    grep -q "SA02M_WEB_USER='admin'" /etc/sa02m_web.env || fail E_HEALTH "web env user not admin"
  fi
  # Preserve checks (must still exist if they existed before — only assert critical ones that always exist)
  [ -f /etc/machine-id ] || fail E_HEALTH "machine-id vanished"
  # Alice certs: if present before, must remain (checked via journal absence of those paths)
  if [ -f /var/lib/sa02m-alice/device.crt.pem ]; then
    is_preserved /var/lib/sa02m-alice/device.crt.pem || fail E_HEALTH "alice cert not preserved"
  fi
  # Never wrote to mmc
  true
}

# The mandatory backup: the same restorable archive the panel downloads
# (sa02m-web-backup.sh streams it to STDOUT and takes no arguments), written to
# a NEW root-only file (0600 in the 0700 backup-export dir, O_EXCL|O_NOFOLLOW)
# and verified whole before anything is reset. It carries .htpasswd,
# sa02m_web.env, the cloud agent.conf and the Alice confs, so it never touches
# the log. No fallback: an archive the restore cannot read, or an empty one, is
# not the backup the reset promises — the reset stops instead.
do_backup() {
  local out=$1 msg
  [ -x "$BACKUP_BIN" ] || fail E_BACKUP "backup helper $BACKUP_BIN missing — the mandatory backup cannot be taken"
  if ! timeout 300 "$BACKUP_BIN" 2>>"$LOGFILE" | fr_safe write-new "$out" 2>>"$LOGFILE"; then
    fr_safe discard "$out" >/dev/null 2>&1 || true
    fail E_BACKUP "backup helper failed — nothing was reset"
  fi
  msg=$(fr_safe verify-backup "$out" 2>&1) || backup_invalid "$out" "$msg"
  log "$msg"
}

backup_invalid() {
  fr_safe discard "$1" >/dev/null 2>&1 || true
  fail E_BACKUP "backup archive invalid — nothing was reset: $2"
}

run_reset() {
  local defaults_dir ts backup_path

  # flock
  exec 9>"$LOCKFILE"
  if ! flock -n 9; then
    fail E_LOCK "another update/reset holds $LOCKFILE"
  fi
  printf '%s\n' "$$" >&9

  # Require CGI-prepared transaction
  [ -f "$TXN_JSON" ] || fail E_INTERNAL "transaction.json missing"
  local op confirm_ok backup_ok
  op=$(txn_get operation)
  [ "$op" = "factory_reset" ] || fail E_INTERNAL "operation is not factory_reset"
  confirm_ok=$(txn_get confirm_phrase_ok)
  backup_ok=$(txn_get backup_ok)
  [ "$confirm_ok" = "True" ] || [ "$confirm_ok" = "true" ] || [ "$confirm_ok" = "1" ] \
    || fail E_CANCEL "confirm_phrase_ok not set (need SA02M-RESET via CGI)"
  [ "$backup_ok" = "True" ] || [ "$backup_ok" = "true" ] || [ "$backup_ok" = "1" ] \
    || fail E_CANCEL "backup_ok not set (mandatory backup)"

  txn_write "stage=validating" "progress_pct=5" "imaging_lock=false"
  load_lists
  defaults_dir=$(resolve_defaults_dir) || fail E_INTERNAL "factory defaults not installed"
  log "defaults_bundle=$defaults_dir"
  txn_write "defaults_bundle=$defaults_dir"
  python3 - "$TXN_JSON" "${WIPE_LIST[@]}" -- "${PRESERVE_LIST[@]}" <<'PY'
import json, os, sys, tempfile, time
path = sys.argv[1]
args = sys.argv[2:]
sep = args.index("--")
wipe, preserve = args[:sep], args[sep + 1 :]
txn = json.load(os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8"))
txn["wipe_manifest"] = wipe
txn["preserve_manifest"] = preserve
txn["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
data = json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(prefix=".txn.", dir=d)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(data)
    f.flush()
    os.fchmod(f.fileno(), 0o644)
    os.fsync(f.fileno())
os.replace(tmp, path)
PY

  # Self-copy before any mutate
  local txn_id runner_copy
  txn_id=$(txn_get id)
  [ -n "$txn_id" ] || txn_id="factory-$$"
  # The CGI (www-data) wrote this id and it becomes a path component of the
  # root self-copy that is exec'd next: a plain token only (uuid4, hex).
  [[ "$txn_id" =~ ^[A-Za-z0-9-]{1,64}$ ]] || fail E_INTERNAL "transaction id is not a plain token"
  runner_copy="$STATEDIR/runner/$txn_id/runner"
  if [ "${SA02M_FACTORY_REEXEC:-}" != "1" ]; then
    mkdir -p "$(dirname "$runner_copy")"
    cp -a "$SELF" "$runner_copy"
    chmod 755 "$runner_copy"
    log "re-exec from $runner_copy"
    export SA02M_FACTORY_REEXEC=1
    exec "$runner_copy" run
  fi

  ts=$(date +%Y%m%dT%H%M%SZ)
  backup_path="$STATEDIR/backup-export/pre-reset-$ts.tar.gz"
  JOURNAL_DIR="$STATEDIR/rollback/factory-$ts"
  export JOURNAL_DIR
  mkdir -p "$JOURNAL_DIR"
  : >"$JOURNAL_DIR/touched.list"
  : >"$JOURNAL_DIR/created.list"

  # On unexpected failure after imaging lock: journal rollback (no tar -C /)
  rollback_on_err() {
    local ec=$?
    [ "$ec" -eq 0 ] && return 0
    log "ERR trap ec=$ec — attempting journal rollback"
    rollback_from_journal || true
    cleanup_imaging_lock || true
  }
  trap rollback_on_err ERR

  txn_write "stage=backing_up" "progress_pct=15"
  do_backup "$backup_path"
  log "backup=$backup_path"

  # backup_path is shown only once the file exists and has verified.
  txn_write "stage=confirmed" "progress_pct=25" "backup_path=$backup_path"
  install_imaging_lock
  txn_write "imaging_lock=true"

  txn_write "stage=wipe" "progress_pct=40"
  log "wipe allowlist entries: ${#WIPE_LIST[@]}"

  txn_write "stage=apply" "progress_pct=60"
  apply_defaults "$defaults_dir"

  txn_write "stage=verify" "progress_pct=85"
  verify_reset
  wipe_homekit_pairings

  trap - ERR

  # Reload services that consume configs (best-effort)
  systemctl reload sa02m-serial-gateway 2>/dev/null || true
  systemctl restart sa02m-modbus-mqtt 2>/dev/null || true
  if command -v nginx >/dev/null 2>&1; then
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
  fi

  cleanup_imaging_lock
  txn_write "stage=done" "progress_pct=100" "imaging_lock=false" "result=success" \
    "error_code=null" "error_message="
  log "DONE factory reset"
  sync
}

# A factory reset is the longest single-purpose operation on the board, so the
# path that matters is the one that does NOT reach the end. Before this trap the
# hold was given back only from cleanup_imaging_lock — reached by run_reset's ERR
# trap and by fail() — so a SIGTERM (systemd stopping the job, an operator abort)
# left the manager's hardware watchdog disabled with nobody left to re-arm it.
# INT/TERM are turned into an exit because bash kills the shell on an unhandled
# signal WITHOUT running the EXIT trap.
on_exit() {
  if [ "$IMAGING_HELD" = "1" ]; then
    log "exiting with the imaging lock still held — releasing it"
    cleanup_imaging_lock || true
  fi
}
trap on_exit EXIT
trap 'exit 143' INT TERM

case "$CMD" in
  run|apply) run_reset ;;
  rollback)
    JOURNAL_DIR=$(ls -1d "$STATEDIR/rollback"/factory-* 2>/dev/null | sort | tail -1 || true)
    [ -n "${JOURNAL_DIR:-}" ] || { echo "no journal"; exit 1; }
    rollback_from_journal
    ;;
  *)
    echo "usage: $0 run|rollback" >&2
    exit 2
    ;;
esac
