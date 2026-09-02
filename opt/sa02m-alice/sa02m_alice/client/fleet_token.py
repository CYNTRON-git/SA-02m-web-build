"""Cloud-profile identity + short-lived control token (stdlib urllib).

The cloud identity is the cloud agent's: `device_id` from
/etc/sa02m-cloud/agent.conf and the per-device secret file it wrote at
enrollment. This module never writes either, never persists a token and never
logs one — the token lives 10 min and is minted fresh on EVERY (re)connect
(docs/contracts/alice-mqtt-mapping.md §Profiles).

TLS trust is the system root store, the same trust the cloud agent's own
`api_post` uses — no client certificate on this profile.
"""

from __future__ import annotations

import configparser
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Tuple

from ..common import constants as C


class FleetTokenError(RuntimeError):
    """Token mint refused or unavailable; `state` names the status to publish.

    `error` (STATE_ERROR) carries the fleet's own reason (`revoked`,
    `invalid credential`); `offline` (STATE_OFFLINE) means cloud control is not
    enabled on the host (HTTP 503) — retry on the normal backoff either way.
    """

    def __init__(self, reason: str, state: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state


def read_cloud_identity(
    conf_path: Optional[str] = None, secret_path: Optional[str] = None
) -> Tuple[str, str]:
    """(device_id, device_secret) — either empty when its file is absent.

    `device_id` is `[cloud] device_id`, else the agent's bench convention
    `sa02m-<serial>` from `[device] serial` — the same two sources the cloud
    agent enrolls under, so the hub sees one identity from both units.
    """
    conf = conf_path or C.CLOUD_AGENT_CONF
    secret = secret_path or C.CLOUD_DEVICE_SECRET
    device_id = ""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(conf, encoding="utf-8")
        device_id = cfg.get("cloud", "device_id", fallback="").strip()
        if not device_id:
            serial = cfg.get("device", "serial", fallback="").strip()
            if serial:
                device_id = "sa02m-" + serial.lower()
    except (OSError, configparser.Error):
        device_id = ""
    try:
        with open(secret, encoding="utf-8") as fh:
            device_secret = fh.read().strip()
    except OSError:
        device_secret = ""
    return device_id, device_secret


def cloud_identity_present(
    conf_path: Optional[str] = None, secret_path: Optional[str] = None
) -> bool:
    device_id, device_secret = read_cloud_identity(conf_path, secret_path)
    return bool(device_id and device_secret)


Opener = Callable[..., Any]


def mint_control_token(
    token_url: str,
    device_id: str,
    device_secret: str,
    *,
    timeout: float = C.CLOUD_TOKEN_TIMEOUT_S,
    opener: Opener = urllib.request.urlopen,
) -> str:
    """POST {device_id, device_secret} → the JWT for X-Control-Token.

    Raises FleetTokenError on a refusal (403 / ok:false → `error` with the
    fleet's reason; 503 → `offline`). A transport failure propagates as the
    urllib exception so the caller's generic error path handles it like any
    other unreachable gateway.
    """
    body = json.dumps({"device_id": device_id, "device_secret": device_secret}).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            raise FleetTokenError("cloud control not enabled on the host", C.STATE_OFFLINE) from exc
        reason = _reason_from_body(exc.read()) or ("HTTP %s" % exc.code)
        raise FleetTokenError(reason, C.STATE_ERROR) from exc
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise FleetTokenError("token endpoint returned non-JSON", C.STATE_ERROR) from exc
    if not isinstance(data, dict) or not data.get("ok"):
        reason = (data.get("error") or data.get("message")) if isinstance(data, dict) else None
        raise FleetTokenError(str(reason or "token refused"), C.STATE_ERROR)
    token = str(data.get("token") or "")
    if not token:
        raise FleetTokenError("token endpoint returned no token", C.STATE_ERROR)
    return token


def _reason_from_body(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except (json.JSONDecodeError, AttributeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("error") or data.get("message") or "")


__all__ = [
    "FleetTokenError",
    "cloud_identity_present",
    "mint_control_token",
    "read_cloud_identity",
]
