"""REST reads from the BSH cloud — READ-ONLY by construction.

The only verb this client has is GET (Operator decision Q-E): there is no
method that could send PUT/POST/DELETE to an appliance, and the scopes
requested (constants.SCOPES) carry no Control/Settings right either.

Every attempt is charged to the budget before it is sent (failures count
against BSH's 1000/day too). A 429 blocks every call for its Retry-After.
Only network errors and 5xx are retried, with exponential backoff and jitter,
at most API_RETRY_MAX times; no other 4xx is ever retried (a repeat fails the
same way and still costs a call). A 401 triggers one token refresh and one
repeat.
"""

from __future__ import annotations

import logging
import random
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from . import constants as C
from .budget import Budget
from .oauth import OAuthClient
from .transport import HttpError, NetworkError, Transport

log = logging.getLogger("sa02m_homeconnect.api")

HA_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class Stopped(Exception):
    """The daemon is stopping; a backoff wait was interrupted."""


def backoff_delay(attempt: int, base: float, cap: float, rng: Callable[[], float] = random.random) -> float:
    """base·2^attempt capped at `cap`, ± BACKOFF_JITTER."""
    delay = min(cap, base * (2 ** max(0, attempt)))
    jitter = delay * C.BACKOFF_JITTER
    return max(0.0, delay - jitter + 2 * jitter * rng())


class ApiClient:
    def __init__(
        self,
        transport: Transport,
        budget: Budget,
        oauth: OAuthClient,
        *,
        wait: Optional[Callable[[float], bool]] = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._transport = transport
        self._budget = budget
        self._oauth = oauth
        # wait(seconds) -> True when the daemon is stopping (Event.wait).
        self._wait = wait or (lambda s: (time.sleep(s), False)[1])
        self._clock = clock
        self._rng = rng

    def get(self, path: str) -> Any:
        """GET `path` (absolute, under /api/). Raises HttpError, NetworkError,
        BudgetExceeded, oauth.NotLinked / TokenRevoked, Stopped."""
        if not path.startswith("/api/"):
            raise ValueError("not an API path: %r" % path)
        attempt = 0
        refreshed = False
        while True:
            self._oauth.ensure_fresh()
            self._budget.charge(C.KIND_API)
            headers = {
                "Accept": C.ACCEPT_JSON,
                "Accept-Language": "en-GB",
                "Authorization": self._oauth.authorization_header(),
            }
            try:
                return self._transport.request("GET", path, headers=headers)
            except HttpError as exc:
                if exc.status == 429:
                    until = self._budget.block_for(
                        exc.retry_after_s(self._clock()) or C.RETRY_AFTER_DEFAULT_S)
                    log.warning("Home Connect rate limit (429) on %s — blocked until %d", path, until)
                    raise
                if exc.status == 401 and not refreshed:
                    refreshed = True
                    log.info("401 on %s — refreshing the access token once", path)
                    self._oauth.refresh()
                    continue
                # No 4xx is ever retried (400/403/404/405/406/409/415 among
                # them): a repeat fails the same way and still costs a call.
                if exc.status < 500 or attempt >= C.API_RETRY_MAX:
                    raise
                failure = "HTTP %d" % exc.status
            except NetworkError as exc:
                if attempt >= C.API_RETRY_MAX:
                    raise
                failure = str(exc)
            delay = backoff_delay(attempt, C.API_BACKOFF_BASE_S, C.API_BACKOFF_MAX_S, self._rng)
            attempt += 1
            log.warning("GET %s failed (%s) — retry %d/%d in %.1f s", path, failure, attempt,
                        C.API_RETRY_MAX, delay)
            if self._wait(delay):
                raise Stopped()

    # ── typed reads ──────────────────────────────────────────────────────
    def appliances(self) -> List[Dict[str, Any]]:
        data = self.get(C.APPLIANCES_PATH)
        items = (data.get("data") or {}).get("homeappliances") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [a for a in items if isinstance(a, dict) and isinstance(a.get("haId"), str)
                and HA_ID_RE.match(a["haId"])]

    def _ha_path(self, ha_id: str, suffix: str) -> str:
        if not HA_ID_RE.match(ha_id or ""):
            raise ValueError("refused haId")
        return "%s/%s%s" % (C.APPLIANCES_PATH, urllib.parse.quote(ha_id, safe=""), suffix)

    def status(self, ha_id: str) -> List[Dict[str, Any]]:
        data = self.get(self._ha_path(ha_id, "/status"))
        items = (data.get("data") or {}).get("status") if isinstance(data, dict) else None
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def active_program(self, ha_id: str) -> Optional[Dict[str, Any]]:
        """The active program, or None when the appliance reports none."""
        try:
            data = self.get(self._ha_path(ha_id, "/programs/active"))
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        prog = data.get("data") if isinstance(data, dict) else None
        return prog if isinstance(prog, dict) else None
