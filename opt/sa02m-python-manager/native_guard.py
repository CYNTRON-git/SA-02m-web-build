"""Reject changed native targets between preparation and a consumer restart."""
import json
from pathlib import Path
import stat


def stamp(path):
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid or meta.st_mode & 0o022:
        raise ValueError(f"unsafe native target {path}")
    # A golden filesystem copy can renumber inodes. Revalidate exact bytes in
    # that case; the common path only needs local filesystem metadata.
    return [meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns]


def check(prefix):
    data = json.loads((prefix.parent/"native-guard.json").read_bytes())
    names=data["all"] + data.get(prefix.name, [])
    parents={p for name in names for p in Path(name).parents}
    for path in parents:
        meta=path.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid or meta.st_mode & 0o022:
            raise ValueError(f"unsafe native source directory {path}")
    for name in names:
        path = Path(name)
        current = stamp(path)
        saved = data["signatures"][name]
        if current != saved["stat"]:
            import hashlib
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != saved["sha256"]:
                raise ValueError(f"native runtime changed: {path}")
