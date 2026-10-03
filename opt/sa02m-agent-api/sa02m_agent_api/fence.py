# -*- coding: utf-8 -*-
"""Paths under /opt/sa02m-user. A symlink component is a refusal, not a follow."""

import os
import re

USER_ROOT = os.environ.get("SA02M_USER_ROOT") or "/opt/sa02m-user"
UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
PIP_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_FILE = 256 * 1024


class FenceError(ValueError):
    pass


def resolve(rel, root=None):
    """Return an absolute path that stays under root. No absolute, no '..', no symlink."""
    base = os.path.abspath(root or USER_ROOT)
    if not isinstance(rel, str) or "\x00" in rel:
        raise FenceError("path")
    if rel.startswith("/") or rel.startswith("\\"):
        raise FenceError("path")
    cur = base
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise FenceError("path")
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise FenceError("symlink")
    real_base = os.path.realpath(base)
    real_cur = os.path.realpath(cur)
    try:
        common = os.path.commonpath([real_base, real_cur])
    except ValueError:
        raise FenceError("path")
    if common != real_base:
        raise FenceError("path")
    return cur


def read_text(rel, root=None, limit=MAX_FILE):
    path = resolve(rel, root)
    st = os.lstat(path)
    if not stat_is_reg(st):
        raise FenceError("not_a_file")
    if st.st_size > limit:
        raise FenceError("too_large")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(limit)


def stat_is_reg(st):
    import stat
    return stat.S_ISREG(st.st_mode)


def write_text(rel, text, root=None, limit=MAX_FILE):
    if not isinstance(text, str) or len(text.encode("utf-8")) > limit:
        raise FenceError("too_large")
    path = resolve(rel, root)
    parent = os.path.dirname(path)
    if os.path.islink(parent) or not os.path.isdir(parent):
        raise FenceError("parent")
    tmp = path + ".tmp"
    if os.path.islink(tmp) or os.path.islink(path):
        raise FenceError("symlink")
    if os.path.isdir(path):
        raise FenceError("is_dir")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o660)
        chown = getattr(os, "chown", None)
        if chown is not None:
            try:
                chown(tmp, -1, _group_gid())
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _group_gid():
    import grp
    try:
        return grp.getgrnam("sa02m-user").gr_gid
    except KeyError:
        return -1


def delete_path(rel, root=None):
    path = resolve(rel, root)
    if os.path.islink(path):
        raise FenceError("symlink")
    if os.path.isdir(path):
        raise FenceError("is_dir")
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def mkdir(rel, root=None):
    path = resolve(rel, root)
    if os.path.islink(path):
        raise FenceError("symlink")
    os.makedirs(path, mode=0o2770, exist_ok=True)
    return path


def list_dir(rel="", root=None):
    path = resolve(rel, root) if rel else os.path.abspath(root or USER_ROOT)
    if rel == "" and os.path.islink(path):
        raise FenceError("symlink")
    if not os.path.isdir(path) or os.path.islink(path):
        raise FenceError("not_a_dir")
    names = []
    for name in sorted(os.listdir(path)):
        child = os.path.join(path, name)
        kind = "link" if os.path.islink(child) else "dir" if os.path.isdir(child) else "file"
        names.append({"name": name, "kind": kind})
    return names
