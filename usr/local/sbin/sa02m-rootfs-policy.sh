#!/bin/sh
# One policy for the installed root and mounted image partitions. No web grant.
exec /usr/bin/python3 - "$@" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

LIVE_ROOT = "/"
FINDMNT = "/usr/bin/findmnt"
TUNE2FS = "/usr/sbin/tune2fs"
BLKID = "/usr/sbin/blkid"
MOUNT = "/bin/mount"
changed = {"fstab": "unchanged", "defaults": "unchanged", "live": "unchanged"}
backup = None
temporary = None


def run(argv):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    if result.returncode:
        raise RuntimeError("command failed: " + repr(argv) + ": " + result.stderr.strip())
    return result.stdout.strip()


def safe_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("unsafe file: " + str(path))
    return info


def safe_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("unsafe directory: " + str(path))


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mount_state(root):
    rows = json.loads(run([FINDMNT, "--json", "--mountpoint", str(root),
                           "--output", "SOURCE,FSTYPE,TARGET,OPTIONS"]))["filesystems"]
    if len(rows) != 1 or rows[0]["fstype"] != "ext4" or rows[0]["target"] != str(root):
        raise RuntimeError("root must be exactly one ext4 mountpoint")
    return rows[0]


def defaults(device):
    output = run([TUNE2FS, "-l", str(device)])
    rows = re.findall(r"^Default mount options:\s*(.*)$", output, re.MULTILINE)
    if len(rows) != 1:
        raise RuntimeError("unreadable superblock defaults")
    return rows[0].split()


def unescape(value):
    return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value)


try:
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    args = sys.argv[1:]
    if args == ["live"]:
        mode = "live"
        root = Path(LIVE_ROOT)
    elif len(args) == 3 and args[0] in ("offline", "check"):
        mode, root_arg, device_arg = args
        root = Path(root_arg)
        if root == Path("/"):
            raise RuntimeError("offline root must not be the host root")
    else:
        raise RuntimeError("usage: sa02m-rootfs-policy.sh live | offline ROOT DEVICE | check ROOT DEVICE")
    if not root.is_absolute() or root.resolve() != root:
        raise RuntimeError("root must be absolute and have no symlink components")
    safe_dir(root)
    etc = root / "etc"
    safe_dir(etc)
    lock = os.open(etc / ".sa02m-rootfs-policy.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    lock_stat = os.fstat(lock)
    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1 or lock_stat.st_uid != 0 or stat.S_IMODE(lock_stat.st_mode) != 0o600:
        raise RuntimeError("unsafe policy lock")
    deadline = time.monotonic() + 2
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError("policy lock busy")
            time.sleep(0.05)

    mounted = mount_state(root)
    mounted_device = os.path.realpath(mounted["source"])
    device = Path(mounted_device if mode == "live" else device_arg).resolve()
    device_stat = device.stat()
    if not stat.S_ISBLK(device_stat.st_mode) or str(device) != mounted_device:
        raise RuntimeError("device is not the root's mounted block device")
    fstab = etc / "fstab"
    before = safe_file(fstab)
    if before.st_size > 1024 * 1024:
        raise RuntimeError("fstab exceeds 1 MiB")
    original = fstab.read_bytes()
    text = original.decode("utf-8")
    matches = []
    for match in re.finditer(r"^(?!\s*#)[ \t]*(\S+)([ \t]+)(\S+)([ \t]+)(\S+)([ \t]+)(\S+)([^\n]*)$", text, re.MULTILINE):
        if unescape(match[3]) == "/":
            matches.append(match)
    if len(matches) != 1 or matches[0][5] != "ext4":
        raise RuntimeError("fstab needs exactly one ext4 root entry")
    entry = matches[0]
    source = unescape(entry[1])
    if "=" in source:
        tag, value = source.split("=", 1)
        if tag not in ("UUID", "LABEL", "PARTUUID", "PARTLABEL") or run([BLKID, "-s", tag, "-o", "value", str(device)]) != value:
            raise RuntimeError("fstab source does not identify the mounted device")
    elif not source.startswith("/") or Path(source).resolve() != device:
        raise RuntimeError("fstab source does not identify the mounted device")
    options = entry[7].split(",")
    if any(o.startswith("data=") and o != "data=ordered" for o in options):
        raise RuntimeError("explicit conflicting data= policy requires controlled migration")
    new_options = [o for o in options if not o.startswith("commit=")]
    new_options.append("commit=60")
    desired = (text[:entry.start(7)] + ",".join(new_options) + text[entry.end(7):]).encode("utf-8")
    old_defaults = defaults(device)
    conflicting = set(old_defaults) & {"journal_data_writeback", "journal_data"}
    if mode == "check":
        if [o for o in options if o.startswith("commit=")] != ["commit=60"] or conflicting:
            raise RuntimeError("rootfs policy check failed")
        print("rootfs policy verified: commit=60, ordered-compatible defaults")
        sys.exit(0)

    # Validate the actual candidate root record with libmount before publication.
    fd, name = tempfile.mkstemp(prefix=".fstab-policy-", dir=etc)
    temporary = Path(name)
    with os.fdopen(fd, "wb") as stream:
        stream.write(desired)
        stream.flush()
        os.fchown(stream.fileno(), before.st_uid, before.st_gid)
        os.fchmod(stream.fileno(), stat.S_IMODE(before.st_mode))
        os.fsync(stream.fileno())
    shutil.copystat(fstab, temporary, follow_symlinks=False)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    run([FINDMNT, "--verify", "--tab-file", str(temporary), "--target", "/"])
    live_change = mode == "live" and "commit=60" not in mounted["options"].split(",")
    if desired != original or conflicting or live_change:
        backup = Path(tempfile.mkdtemp(prefix=".sa02m-rootfs-policy-", dir=etc))
        shutil.copy2(fstab, backup / "fstab", follow_symlinks=False)
        os.chown(backup / "fstab", before.st_uid, before.st_gid)
        with (backup / "fstab").open("rb") as stream:
            os.fsync(stream.fileno())
        with (backup / "before.json").open("x") as stream:
            json.dump({"source": str(device), "mount": mounted, "defaults": old_defaults,
                       "uid": before.st_uid, "gid": before.st_gid, "mode": stat.S_IMODE(before.st_mode)}, stream)
            stream.flush(); os.fsync(stream.fileno())
        fsync_dir(backup)
        fsync_dir(etc)
        print("backup: " + str(backup), flush=True)
    if desired != original:
        now = safe_file(fstab)
        if (now.st_dev, now.st_ino, now.st_mtime_ns, now.st_size) != (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size):
            raise RuntimeError("fstab changed during preparation")
        os.replace(temporary, fstab)
        temporary = None
        changed["fstab"] = "changed"
        fsync_dir(etc)
    if conflicting:
        changed["defaults"] = "unknown"
        run([TUNE2FS, "-o", "^journal_data_writeback,^journal_data", str(device)])
        after_defaults = defaults(device)
        if set(after_defaults) != set(old_defaults) - conflicting:
            raise RuntimeError("superblock default verification failed")
        changed["defaults"] = "changed"
    if live_change:
        changed["live"] = "unknown"
        run([MOUNT, "-o", "remount,commit=60", str(root)])
        if "commit=60" not in mount_state(root)["options"].split(","):
            raise RuntimeError("live commit interval verification failed")
        changed["live"] = "changed"
    print("rootfs policy applied: " + " ".join(k + "=" + v for k, v in changed.items()) +
          "; data mode takes effect on next mount")
except Exception as exc:
    print("rootfs policy ERROR: " + str(exc) + "; " +
          " ".join(k + "=" + v for k, v in changed.items()), file=sys.stderr)
    if backup:
        print("restore evidence: " + str(backup) +
              "; restore fstab atomically from fstab, previous tune2fs defaults and live commit from before.json; data mode requires next mount", file=sys.stderr)
    sys.exit(1)
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
PY
