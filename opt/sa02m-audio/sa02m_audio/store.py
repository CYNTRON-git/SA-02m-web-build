"""Clips on a medium: ids, the `.part` → rename write, listing, retention,
deletion (contract §Хранение).

Layout per medium: `<store>/tmp/<id>.wav.part` while recording, then
`<store>/rec/<id>.wav` (rename inside one filesystem — atomic). The directory
listing IS the index: no index file to drift. Only names matching the id
grammar AND regular single-link files count; anything else in `rec/` (a
planted symlink, a FIFO, a stray name) is neither listed nor counted.

Retention judges each PRESENT medium on its own against the same conf caps:
older than `keep_hours`, then oldest first while over `keep_files` or
`keep_mb`. Age and order come from the file's mtime (ties by id).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat as stat_module
import time
from collections import namedtuple
from typing import Dict, Iterable, List, Optional, Tuple

from . import constants as C
from .fsutil import fsync_dir, is_regular, unlink_quietly
from .media import close_all, store_dirs

Clip = namedtuple("Clip", "id medium size mtime")

ID_RE = re.compile(C.ID_RE)
_CLIP_NAME = re.compile(C.ID_RE[:-1] + re.escape(C.CLIP_SUFFIX) + "$")
_META_NAME = re.compile(C.ID_RE[:-1] + re.escape(C.META_SUFFIX) + "$")
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# The sidecar's keys and their check: everything else is dropped on read.
META_KEYS = ("duration_ms", "peak_dbfs", "upload")


def clean_meta(raw: object) -> Dict[str, object]:
    """A sidecar's values that pass their grammar; {} for anything else (a
    sidecar on a removable medium can be planted by anyone)."""
    out: Dict[str, object] = {}
    if not isinstance(raw, dict):
        return out
    d = raw.get("duration_ms")
    if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= C.MAX_DURATION_HARD_S * 1000:
        out["duration_ms"] = d
    p = raw.get("peak_dbfs")
    if isinstance(p, int) and not isinstance(p, bool) and C.PEAK_FLOOR_DBFS <= p <= 0:
        out["peak_dbfs"] = p
    u = raw.get("upload")
    if isinstance(u, str) and u in C.UPLOAD_STATES:
        out["upload"] = u
    return out
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def new_id(now: Optional[float] = None) -> str:
    """`YYYYMMDDTHHMMSSZ-<6 hex>` in UTC; the random tail keeps it unique
    under a wrong or repeated clock."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    return "%s-%s" % (stamp, secrets.token_hex(3))


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


class Part:
    """An open `.part` and the store descriptors it was created under. The
    descriptors pin the filesystem from start to finalize: a lazily unmounted
    stick is still reachable through them, which is why the caller re-checks
    presence before `commit`."""

    def __init__(self, medium: str, rec_id: str, fds: Tuple[int, ...], fd: int) -> None:
        self.medium = medium
        self.id = rec_id
        self.store_fd, self.rec_fd, self.tmp_fd = fds
        self.fd = fd
        self.name = rec_id + C.PART_SUFFIX
        self.closed = False
        # Bytes copied from the write descriptor before it is closed. Upload
        # sends these, never a later open of the clip's name.
        self._clip_bytes: Optional[bytes] = None

    def commit(self) -> int:
        """rename into rec/ + fsync the directory; returns the clip size."""
        st = is_regular(self.tmp_fd, self.name)
        if st is None:
            raise OSError("part vanished")
        os.rename(self.name, self.id + C.CLIP_SUFFIX,
                  src_dir_fd=self.tmp_fd, dst_dir_fd=self.rec_fd)
        fsync_dir(self.rec_fd)
        return int(st.st_size)

    def abandon(self) -> None:
        unlink_quietly(self.tmp_fd, self.name)

    def remember_written(self, fd: int) -> None:
        """Copy the file this descriptor has just written.

        `pread` does not move the write offset. The copy is what an upload
        sends: opening `rec/<id>.wav` by name later can see a different file
        (a removable medium is writable by anyone holding it). A short read
        keeps nothing — a partial WAV is not uploaded."""
        pread = getattr(os, "pread", None)
        if pread is None:
            return
        try:
            st = os.fstat(fd)
            if (not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1
                    or st.st_size <= 0 or st.st_size > C.DOWNLOAD_MAX_BYTES):
                return
            chunks = []
            offset = 0
            left = st.st_size
            while left > 0:
                chunk = pread(fd, min(65536, left), offset)
                if not chunk:
                    return
                chunks.append(chunk)
                offset += len(chunk)
                left -= len(chunk)
            self._clip_bytes = b"".join(chunks)
        except OSError:
            return

    def read_clip(self, max_bytes: int) -> Optional[bytes]:
        """The bytes `remember_written` copied, or None.

        Never opens the clip name. None also when that copy is missing or
        larger than `max_bytes` (the upload then fails closed)."""
        data = self._clip_bytes
        if data is None or len(data) > max_bytes:
            return None
        return data

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            close_all(self.store_fd, self.rec_fd, self.tmp_fd)


class Store:
    def __init__(self, media: Iterable[C.Medium] = C.MEDIA) -> None:
        self.media: Dict[str, C.Medium] = {m.name: m for m in media}

    def open_part(self, name: str, rec_id: str) -> Part:
        """Create `tmp/<id>.wav.part` (O_EXCL, never through a link). The
        file descriptor is the caller's to close; the Part owns the rest."""
        if not valid_id(rec_id):
            raise ValueError("bad id")
        fds = store_dirs(self.media[name], create=True)
        try:
            fd = os.open(rec_id + C.PART_SUFFIX,
                         os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                         0o640, dir_fd=fds[2])
        except BaseException:
            close_all(*fds)
            raise
        return Part(name, rec_id, fds, fd)

    def _dirs(self, name: str, sub: str) -> Optional[Tuple[int, ...]]:
        """(store fd, `sub` fd) of an existing store, else None."""
        try:
            return store_dirs(self.media[name], create=False, subdirs=(sub,))
        except OSError:
            return None

    def cleanup_parts(self, name: str) -> int:
        """Delete every `.part` (and stray probe) in tmp/; returns the number
        of parts — a part left over means a recording was cut short."""
        fds = self._dirs(name, C.TMP_SUBDIR)
        if fds is None:
            return 0
        removed = 0
        try:
            for entry in os.listdir(fds[1]):
                if entry.endswith(C.PART_SUFFIX):
                    if unlink_quietly(fds[1], entry):
                        removed += 1
                elif entry.startswith(C.PROBE_PREFIX) or entry.endswith(C.META_TMP_SUFFIX):
                    unlink_quietly(fds[1], entry)
        except OSError:
            pass
        finally:
            close_all(*fds)
        return removed

    def list_clips(self, name: str) -> List[Clip]:
        fds = self._dirs(name, C.REC_SUBDIR)
        if fds is None:
            return []
        clips = []
        try:
            for entry in os.listdir(fds[1]):
                if not _CLIP_NAME.match(entry):
                    continue
                st = is_regular(fds[1], entry)
                if st is None:
                    continue
                clips.append(Clip(entry[:-len(C.CLIP_SUFFIX)], name, int(st.st_size),
                                  float(st.st_mtime)))
        except OSError:
            pass
        finally:
            close_all(*fds)
        clips.sort(key=lambda c: (c.mtime, c.id))
        return clips

    def _unlink_clip(self, name: str, rec_id: str) -> bool:
        if not valid_id(rec_id):
            return False
        fds = self._dirs(name, C.REC_SUBDIR)
        if fds is None:
            return False
        try:
            gone = unlink_quietly(fds[1], rec_id + C.CLIP_SUFFIX)
            unlink_quietly(fds[1], rec_id + C.META_SUFFIX)
            return gone
        finally:
            close_all(*fds)

    def write_meta(self, name: str, rec_id: str, meta: Dict[str, object]) -> bool:
        """Replace `rec/<id>.json` atomically (temp in tmp/, rename into rec/,
        same filesystem). Written only next to an existing clip: a sidecar
        never outlives or precedes its WAV."""
        if not valid_id(rec_id):
            return False
        body = json.dumps(clean_meta(meta), sort_keys=True).encode("ascii")
        try:
            fds = store_dirs(self.media[name], create=False)
        except OSError:
            return False
        tmp = rec_id + C.META_TMP_SUFFIX
        try:
            if is_regular(fds[1], rec_id + C.CLIP_SUFFIX) is None:
                return False
            unlink_quietly(fds[2], tmp)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                         0o640, dir_fd=fds[2])
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, rec_id + C.META_SUFFIX, src_dir_fd=fds[2], dst_dir_fd=fds[1])
            fsync_dir(fds[1])
            return True
        except OSError:
            unlink_quietly(fds[2], tmp)
            return False
        finally:
            close_all(*fds)

    def read_meta(self, name: str, rec_id: str) -> Dict[str, object]:
        """The sidecar's checked values, {} when absent or not trusted (a
        link, a FIFO, a hard link, oversize, not JSON)."""
        if not valid_id(rec_id):
            return {}
        fds = self._dirs(name, C.REC_SUBDIR)
        if fds is None:
            return {}
        try:
            fd = os.open(rec_id + C.META_SUFFIX, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
                         dir_fd=fds[1])
        except OSError:
            close_all(*fds)
            return {}
        try:
            st = os.fstat(fd)
            if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > C.META_MAX_BYTES:
                return {}
            raw = os.read(fd, C.META_MAX_BYTES + 1)
            return clean_meta(json.loads(raw.decode("ascii")))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        finally:
            os.close(fd)
            close_all(*fds)

    def sweep_orphan_meta(self, name: str) -> int:
        """Delete sidecars whose clip is gone (deleted by hand, a torn run)."""
        fds = self._dirs(name, C.REC_SUBDIR)
        if fds is None:
            return 0
        removed = 0
        try:
            names = set(os.listdir(fds[1]))
            for entry in names:
                if _META_NAME.match(entry) and entry[:-len(C.META_SUFFIX)] + C.CLIP_SUFFIX not in names:
                    if unlink_quietly(fds[1], entry):
                        removed += 1
        except OSError:
            pass
        finally:
            close_all(*fds)
        return removed

    def retention(self, name: str, *, keep_files: int, keep_mb: int, keep_hours: int,
                  now: Optional[float] = None) -> List[str]:
        """Apply the caps to one medium; returns the deleted ids."""
        now = time.time() if now is None else now
        clips = self.list_clips(name)
        deleted = []
        keep = []
        for clip in clips:
            if now - clip.mtime > keep_hours * 3600:
                if self._unlink_clip(name, clip.id):
                    deleted.append(clip.id)
                    continue
            keep.append(clip)
        total = sum(c.size for c in keep)
        while keep and (len(keep) > keep_files or total > keep_mb * C.MB):
            clip = keep.pop(0)
            total -= clip.size
            if self._unlink_clip(name, clip.id):
                deleted.append(clip.id)
        self.sweep_orphan_meta(name)
        return deleted

    def delete(self, present: Iterable[str], target: str) -> List[str]:
        """`target` = an id or `all`, searched across the present media."""
        deleted = []
        for name in present:
            for clip in self.list_clips(name):
                if target == C.DELETE_ALL or clip.id == target:
                    if self._unlink_clip(name, clip.id):
                        deleted.append(clip.id)
        return deleted

    def summary(self, name: str) -> Tuple[int, int, str]:
        """(files, bytes, newest id) of one medium."""
        clips = self.list_clips(name)
        newest = max((c.id for c in clips), default="")
        return len(clips), sum(c.size for c in clips), newest
