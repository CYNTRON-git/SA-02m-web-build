"""Load/save Alice config files (INI client/server + JSON devices)."""

from __future__ import annotations

import configparser
import json
import os
import stat as stat_module
import tempfile
from typing import Any, Dict, Tuple

from . import constants as C


def _atomic_write(path: str, data: str, mode: int = 0o640) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # Preserve the existing file's mode/owner across the replace: writers run
    # as BOTH root (client/config services, web-service-ctl) and www-data (the
    # CGI) — without this a root write leaves the conf root:root 0640 and the
    # www-data web layer can no longer read or write it.
    st = None
    try:
        st = os.stat(path)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".alice-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            if not data.endswith("\n"):
                fh.write("\n")
        if st is not None:
            os.chmod(tmp, stat_module.S_IMODE(st.st_mode))
            if hasattr(os, "chown"):  # absent on Windows dev hosts
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except OSError:
                    pass  # non-root writer keeps its own uid; group suffices
        else:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_ini(path: str, defaults: Dict[str, Dict[str, str]]) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(defaults)
    if os.path.exists(path):
        cfg.read(path, encoding="utf-8")
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
            }
        },
    )


def client_enabled(cfg: configparser.ConfigParser | None = None) -> bool:
    c = cfg or default_client_cfg()
    return c.getboolean("client", "client_enabled", fallback=False)


def set_client_enabled(enabled: bool) -> None:
    cfg = default_client_cfg()
    if not cfg.has_section("client"):
        cfg.add_section("client")
    cfg.set("client", "client_enabled", "true" if enabled else "false")
    save_ini(C.CLIENT_CONF, cfg)


def unlinked_at(cfg: configparser.ConfigParser | None = None) -> str:
    """The durable «cloud unlinked us» marker, or "" when the board is normal.

    Home of the key: C.KEY_UNLINKED_AT in the [client] section — see the
    constant for why it may not live under VAR_DIR.
    """
    c = cfg or default_client_cfg()
    return (c.get("client", C.KEY_UNLINKED_AT, fallback="") or "").strip()


def set_unlinked_at(value: str | None) -> None:
    """Set the marker (a timestamp string) or clear it with None/"".

    Writes through save_ini/_atomic_write so the root/www-data mode dance is
    preserved — never hand-roll a writer for this file.
    """
    cfg = default_client_cfg()
    if not cfg.has_section("client"):
        cfg.add_section("client")
    if value:
        cfg.set("client", C.KEY_UNLINKED_AT, str(value))
    elif cfg.has_option("client", C.KEY_UNLINKED_AT):
        cfg.remove_option("client", C.KEY_UNLINKED_AT)
    else:
        return  # nothing to clear — do not rewrite the file for a no-op
    save_ini(C.CLIENT_CONF, cfg)


def empty_devices() -> Dict[str, Any]:
    return {"rooms": [], "devices": []}


def load_devices(path: str | None = None) -> Dict[str, Any]:
    p = path or C.DEVICES_CONF
    if not os.path.exists(p):
        return empty_devices()
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return empty_devices()
    data.setdefault("rooms", [])
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


def cert_paths_present() -> bool:
    return os.path.isfile(C.CERT_FILE) and os.path.isfile(C.KEY_FILE)
