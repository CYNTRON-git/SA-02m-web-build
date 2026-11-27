"""File identity and freshness shared by the worker and its stdlib-only CGI reader."""

import math
import os
import stat


CONFIG_PATH = os.environ.get("SA02M_MQTT_CONFIG", "/etc/sa02m-modbus-mqtt.yaml")
CONFIG_LIMIT = 1024 * 1024
ROSTER_LIMIT = 8 * CONFIG_LIMIT
BRIDGE_STALE_S = 60.0


def fresh(ts, now, seconds=BRIDGE_STALE_S):
    return type(ts) in (float, int) and 0 < ts <= now and now - ts <= seconds and math.isfinite(ts)


def revision(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def regular_revision(path, limit=CONFIG_LIMIT):
    st = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        raise ValueError("invalid file")
    return revision(st)


def read_regular(path, limit=CONFIG_LIMIT, private=False):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as reader:
        st = os.fstat(reader.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise ValueError("invalid file")
        if private and (st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600):
            raise ValueError("unsafe private file")
        data = reader.read(limit + 1)
        rev = revision(st)
        if len(data) > limit or regular_revision(path, limit) != rev:
            raise ValueError("file changed")
        return data, rev
