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

import os
import re
import secrets
import time
from collections import namedtuple
from typing import Dict, Iterable, List, Optional, Tuple

from . import constants as C
from .fsutil import fsync_dir, is_regular, unlink_quietly
from .media import close_all, store_dirs

Clip = namedtuple("Clip", "id medium size mtime")

ID_RE = re.compile(C.ID_RE)
_CLIP_NAME = re.compile(C.ID_RE[:-1] + re.escape(C.CLIP_SUFFIX) + "$")
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
                elif entry.startswith(C.PROBE_PREFIX):
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
            return unlink_quietly(fds[1], rec_id + C.CLIP_SUFFIX)
        finally:
            close_all(*fds)

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
