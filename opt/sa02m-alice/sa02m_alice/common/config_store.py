"""Load/save Alice config files (INI client/server + JSON devices)."""

from __future__ import annotations

import configparser
import contextlib
import json
import os
import stat as stat_module
import tempfile
import threading
from typing import Any, Dict, Iterator, Optional, Tuple

from . import constants as C

try:
    import fcntl
except ImportError:  # Windows dev host — the daemon and the CGI run on Linux
    fcntl = None  # type: ignore[assignment]

# Per-thread nesting depth of devices_lock(): flock(2) is per open file
# description, so a second open+flock from the SAME thread would block on
# itself. Nesting inside one writer is not a design, but a helper calling a
# helper must not deadlock the daemon over it.
_LOCK_DEPTH = threading.local()


# Never carried over from an existing conf (see _atomic_write).
_SPECIAL_BITS = stat_module.S_ISUID | stat_module.S_ISGID | stat_module.S_ISVTX
# The installer's provisioned modes (scripts/06-alice.sh): the two confs the
# www-data CGI writes are 0660, every other conf 0640, all group www-data.
_WEB_GROUP = "www-data"
_RW_CONF_NAMES = ("sa02m-alice-client.conf", "sa02m-alice-devices.conf")


class UnsafeConfPath(OSError):
    """A conf name root must not read or write through (planted link, FIFO…).

    The message names the path and the reason only — never the content, which
    for a planted link is some other file's (the install log is panel-readable).
    """


def _web_gid() -> Optional[int]:
    try:
        import grp
        return grp.getgrnam(_WEB_GROUP).gr_gid
    except (ImportError, KeyError):
        return None  # Windows dev host / no www-data group


def _set_mode(fd: int, tmp: str, mode: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)
    else:  # Windows dev host before Python 3.13: no fchmod, and no www-data to race
        os.chmod(tmp, mode)


def _check_parent(directory: str) -> bool:
    """False when `directory` is absent; UnsafeConfPath when it is a link."""
    try:
        pst = os.lstat(directory)
    except FileNotFoundError:
        return False
    if stat_module.S_ISLNK(pst.st_mode) or not stat_module.S_ISDIR(pst.st_mode):
        raise UnsafeConfPath("refusing %s: the directory is a symlink or not a directory" % directory)
    return True


def _open_conf_for_read(path: str) -> Optional[int]:
    """fd of `path` for reading, None when it is absent, else UnsafeConfPath.

    Root reads these confs from a www-data-writable directory: a planted
    symlink or hard link would hand root another file's bytes, and a parse
    error quoting them lands in the panel-readable install log (web-service-ctl
    appends the writer's stderr there). O_NOFOLLOW + fstat decide on the very
    inode that is read; O_NONBLOCK keeps a planted FIFO from hanging the open.
    """
    if not _check_parent(os.path.dirname(path) or "."):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) \
        | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        if os.path.islink(path):  # O_NOFOLLOW answered ELOOP
            raise UnsafeConfPath("refusing to read %s: it is a symlink" % path) from None
        raise
    st = os.fstat(fd)
    if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise UnsafeConfPath("refusing to read %s: not a regular singly-linked file" % path)
    return fd


def _read_text(path: str) -> Optional[str]:
    fd = _open_conf_for_read(path)
    if fd is None:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()


def _atomic_write(path: str, data: str, mode: Optional[int] = None) -> None:
    """Replace `path` with `data` atomically, touching only our own temp's fd.

    Writers run as BOTH root (client/config services, web-service-ctl) and
    www-data (the CGI) in /etc/sa02m-alice — root:www-data 0770, no sticky
    bit — so www-data can plant any name there, and chmod/chown BY NAME on the
    temp let a rename+symlink swap make root re-own an arbitrary file. Hence:
    the temp is created O_EXCL|O_NOFOLLOW (mkstemp), its mode and owner are set
    through the fd (fchmod/fchown), and the only by-name act is the rename,
    which never follows a link. A symlinked or non-regular destination, or a
    symlinked parent, is refused before anything is created.

    Owner and mode are kept from an existing regular, singly-linked conf (a
    root write must not lock the www-data web layer out), never its
    setuid/setgid/sticky bits; anything else — a new conf, a hard link to a
    foreign file — gets `mode` (default: the installer's mode for this conf
    name) and group www-data. Same shape as atomic_install in
    etc/sa02m-restore-backup.sh. Pinned by tests/test_config_store_race.py.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    if not _check_parent(directory):
        raise FileNotFoundError(2, "conf directory vanished", directory)
    st = None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        pass
    if st is not None:
        if stat_module.S_ISLNK(st.st_mode):
            raise UnsafeConfPath("refusing to write %s: it is a symlink (remove it and retry)" % path)
        if not stat_module.S_ISREG(st.st_mode):
            raise UnsafeConfPath("refusing to write %s: it exists and is not a regular file" % path)
        if st.st_nlink != 1:
            st = None  # a hard link lends nothing; the replace breaks it
    if mode is None:
        mode = 0o660 if os.path.basename(path) in _RW_CONF_NAMES else 0o640
    payload = data if data.endswith("\n") else data + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".alice-", dir=directory)
    try:
        try:
            if st is not None:
                _set_mode(fd, tmp, stat_module.S_IMODE(st.st_mode) & ~_SPECIAL_BITS)
                if hasattr(os, "fchown"):  # absent on Windows dev hosts
                    try:
                        os.fchown(fd, st.st_uid, st.st_gid)
                    except OSError:
                        pass  # non-root writer keeps its own uid; group suffices
            else:
                _set_mode(fd, tmp, mode)
                gid = _web_gid()
                if gid is not None and hasattr(os, "fchown"):
                    try:
                        os.fchown(fd, -1, gid)
                    except OSError:
                        pass  # not a member: the mode still applies
            view = memoryview(payload.encode("utf-8"))
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def load_ini(path: str, defaults: Dict[str, Dict[str, str]]) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(defaults)
    try:
        text = _read_text(path)
    except UnsafeConfPath:
        raise
    except OSError:
        text = None  # configparser.read() skipped an unreadable file the same way
    if text is not None:
        cfg.read_string(text, source=path)
    return cfg


def save_ini(path: str, cfg: configparser.ConfigParser) -> None:
    buf = []
    for section in cfg.sections():
        buf.append("[%s]" % section)
        for key, value in cfg.items(section):
            buf.append("%s = %s" % (key, value))
        buf.append("")
    _atomic_write(path, "\n".join(buf).rstrip() + "\n")


def default_client_cfg() -> configparser.ConfigParser:
    return load_ini(
        C.CLIENT_CONF,
        {
            "client": {
                "client_enabled": "false",
                # Gates the cloud profile exactly as client_enabled gates the
                # Yandex one (sa02m-cloud-control.service exits 0 while false).
                "cloud_control_enabled": "false",
                "log_level": "INFO",
                "mqtt_host": C.DEFAULT_MQTT_HOST,
                "mqtt_port": str(C.DEFAULT_MQTT_PORT),
            }
        },
    )


def default_server_cfg() -> configparser.ConfigParser:
    return load_ini(
        C.SERVER_CONF,
        {
            "gateway": {
                "wss_url": C.DEFAULT_GATEWAY_WSS,
                "http_url": C.DEFAULT_GATEWAY_HTTP,
                "sio_path": C.SIO_PATH,
                # Cloud profile: the control entry on the fleet cloud. The
                # token endpoint follows the cloud agent's api_url unless
                # pinned here (empty = derive).
                "cloud_control_url": C.DEFAULT_CLOUD_CONTROL_URL,
                "cloud_token_url": "",
            }
        },
    )


def client_enabled(cfg: configparser.ConfigParser | None = None) -> bool:
    c = cfg or default_client_cfg()
    return c.getboolean("client", "client_enabled", fallback=False)


def cloud_control_enabled(cfg: configparser.ConfigParser | None = None) -> bool:
    c = cfg or default_client_cfg()
    return c.getboolean("client", "cloud_control_enabled", fallback=False)


def profile_enabled(profile: str, cfg: configparser.ConfigParser | None = None) -> bool:
    """The enable flag that gates `profile` — one flag per unit, same shape."""
    if profile == C.PROFILE_CLOUD:
        return cloud_control_enabled(cfg)
    return client_enabled(cfg)


def _set_client_flag(key: str, enabled: bool) -> None:
    cfg = default_client_cfg()
    if not cfg.has_section("client"):
        cfg.add_section("client")
    cfg.set("client", key, "true" if enabled else "false")
    save_ini(C.CLIENT_CONF, cfg)


def set_client_enabled(enabled: bool) -> None:
    _set_client_flag("client_enabled", enabled)


def set_cloud_control_enabled(enabled: bool) -> None:
    _set_client_flag("cloud_control_enabled", enabled)


def unlink_marker(cfg: configparser.ConfigParser | None = None) -> Tuple[str, str, str]:
    """The durable «the cloud unlinked us» marker as (stamp, class, reason).

    ("", "", "") when the board is normal. Read on every outer-loop pass and at
    start: /run is tmpfs, so this INI is what carries the explanation across a
    reboot — and it is also how the client learns that the CGI (another
    process) ran the local «Отвязать».
    """
    c = cfg or default_client_cfg()
    def _get(key: str) -> str:
        return (c.get("client", key, fallback="") or "").strip()
    return (_get(C.KEY_UNLINKED_AT), _get(C.KEY_UNLINKED_REASON),
            _get(C.KEY_UNLINKED_REASON_TEXT))


def set_unlink_marker(stamp: str, cls: str, reason: str) -> None:
    """Write the three marker keys.

    Through save_ini/_atomic_write so the root/www-data mode dance is preserved
    — both the root client and the www-data CGI write this file, and a
    hand-rolled writer would strip one of them of access.
    """
    cfg = default_client_cfg()
    if not cfg.has_section("client"):
        cfg.add_section("client")
    cfg.set("client", C.KEY_UNLINKED_AT, str(stamp))
    cfg.set("client", C.KEY_UNLINKED_REASON, str(cls))
    cfg.set("client", C.KEY_UNLINKED_REASON_TEXT, str(reason))
    save_ini(C.CLIENT_CONF, cfg)


def clear_unlink_marker() -> None:
    """Drop the marker — the board is being bound again."""
    cfg = default_client_cfg()
    if not cfg.has_section("client"):
        return
    dropped = False
    for key in C.UNLINK_MARKER_KEYS:
        if cfg.has_option("client", key):
            cfg.remove_option("client", key)
            dropped = True
    if not dropped:
        return  # nothing to clear — do not rewrite the file for a no-op
    save_ini(C.CLIENT_CONF, cfg)


@contextlib.contextmanager
def devices_lock(path: Optional[str] = None) -> Iterator[None]:
    """Advisory exclusive lock around a load→modify→save of the device document.

    Three processes write `sa02m-alice-devices.conf`: the CGI (www-data),
    both client units (Socket.IO rename/rooms/groups) and the Yandex unit's
    auto-provisioner. `_atomic_write` makes each replace atomic, not the
    read-modify-write around it — two writers interleaving lost the earlier
    edit silently. Every writer wraps its critical section in this.

    The lock is `flock(LOCK_EX)` on the document's DIRECTORY fd, opened
    read-only: `/etc/sa02m-alice` is 0770 root:www-data (scripts/06-alice.sh),
    so root and www-data can both open it and no lock file has to be created
    with permissions that suit both. flock on a directory is ordinary Linux
    behaviour. Same-thread nesting is a no-op (see `_LOCK_DEPTH`); a missing
    directory (fresh host before the first save's makedirs) or a host without
    `fcntl` (Windows dev box) yields without locking — advisory, never a
    reason the write cannot happen.
    """
    depth = getattr(_LOCK_DEPTH, "n", 0)
    fd = None
    if fcntl is not None and depth == 0:
        directory = os.path.dirname(path or C.DEVICES_CONF) or "."
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            fd = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                fd = None
    _LOCK_DEPTH.n = depth + 1
    try:
        yield
    finally:
        _LOCK_DEPTH.n = depth
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def empty_devices() -> Dict[str, Any]:
    return {"rooms": [], "groups": [], "devices": []}


def load_devices(path: str | None = None) -> Dict[str, Any]:
    p = path or C.DEVICES_CONF
    text = _read_text(p)
    if text is None:
        return empty_devices()
    data = json.loads(text)
    if not isinstance(data, dict):
        return empty_devices()
    data.setdefault("rooms", [])
    data.setdefault("groups", [])
    data.setdefault("devices", [])
    return data


def save_devices(data: Dict[str, Any], path: str | None = None) -> None:
    p = path or C.DEVICES_CONF
    _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def gateway_urls(cfg: configparser.ConfigParser | None = None) -> Tuple[str, str, str]:
    c = cfg or default_server_cfg()
    wss = c.get("gateway", "wss_url", fallback=C.DEFAULT_GATEWAY_WSS).strip()
    http = c.get("gateway", "http_url", fallback=C.DEFAULT_GATEWAY_HTTP).strip().rstrip("/")
    path = c.get("gateway", "sio_path", fallback=C.SIO_PATH).strip() or C.SIO_PATH
    return wss, http, path


def cloud_control_urls(cfg: configparser.ConfigParser | None = None) -> Tuple[str, str]:
    """(control wss URL, token mint URL) for the cloud profile.

    The token URL is derived from the cloud agent's own `api_url` so a bench
    pointed at another host mints from that host too; `cloud_token_url` in
    the server conf pins it explicitly.
    """
    c = cfg or default_server_cfg()
    wss = c.get("gateway", "cloud_control_url", fallback=C.DEFAULT_CLOUD_CONTROL_URL).strip()
    token = c.get("gateway", "cloud_token_url", fallback="").strip()
    if not token:
        token = cloud_agent_api_url().rstrip("/") + C.CLOUD_TOKEN_PATH
    return wss or C.DEFAULT_CLOUD_CONTROL_URL, token


def cloud_agent_api_url(path: str | None = None) -> str:
    """`[cloud] api_url` from the cloud agent's conf; its default when absent."""
    p = path or C.CLOUD_AGENT_CONF
    cfg = configparser.ConfigParser()
    try:
        cfg.read(p, encoding="utf-8")
    except (OSError, configparser.Error):
        return C.DEFAULT_CLOUD_API_URL
    return cfg.get("cloud", "api_url", fallback=C.DEFAULT_CLOUD_API_URL).strip() or C.DEFAULT_CLOUD_API_URL


def cert_paths_present() -> bool:
    return os.path.isfile(C.CERT_FILE) and os.path.isfile(C.KEY_FILE)


def controller_sn() -> str:
    """This board's identity string: the cloud agent's serial, else the first
    16 chars of the machine-id, else a constant.

    Read by the gateway handshake header (`X-Controller-SN`), the pairing
    claim, and the Alice id of an exposed scene — one home, so those three
    can never disagree about which board this is. A cloned machine-id is the
    imaging contract's problem (docs/contracts/image-identity-reset.md).
    """
    for path in (C.CLOUD_AGENT_CONF, "/etc/machine-id"):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            continue
        if path == C.CLOUD_AGENT_CONF:
            for line in text.splitlines():
                if line.strip().startswith("serial"):
                    val = line.split("=", 1)[-1].strip().strip("\"'")
                    if val:
                        return val
            continue
        if text:
            return text[:16]
    return "sa02m"
