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
