"""OAuth 2.0 Device Flow against the BSH cloud (RFC 8628) — no client secret.

With `token_store.py`, the ONLY module that handles token values (the
allow-list `test_token_sinks` pins). Everything else asks this module for an
`authorization_header()` and never sees a token under a name.

* Device authorization: POST DEVICE_AUTH_PATH (client_id, scope). The
  `device_code` is a credential too: it lives in memory only — `link.json`
  gets the user code and the verification URIs, never the device code.
* Token poll at the server's `interval` (fallback 5 s); `slow_down` adds 5 s
  (RFC 8628 §3.5); the lifetime is the server's `expires_in` (fallback: the
  shorter published value, 300 s) — never hard-coded (decision doc G7).
* Refresh REFRESH_BEFORE_S before expiry, at most TOKEN_REFRESH_PER_MIN per
  minute; `invalid_grant` ⇒ the token is revoked (store gets the marker).
* Every request is charged to the budget first (kind `token`); a 429 blocks
  the budget for Retry-After.
"""

from __future__ import annotations

import collections
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional

from . import constants as C
from .budget import Budget, BudgetExceeded
from .token_store import LoadResult, TokenSet, TokenStore
from .transport import HttpError, NetworkError, Transport

log = logging.getLogger("sa02m_homeconnect.oauth")

_USER_CODE_RE = re.compile(r"^[A-Za-z0-9-]{4,32}$")
# Hosts a verification URI may point at. The card opens it in a new tab, so a
# forged cloud answer must not turn the card into a link to anywhere. Whether
# BSH ever answers outside these is unverified until the bench run (G7).
VERIFY_DOMAINS = ("home-connect.com", "home-connect.cn", "singlekey-id.com")

POLL_PENDING = "pending"
POLL_SLOW_DOWN = "slow_down"
POLL_LINKED = "linked"
POLL_EXPIRED = "expired"
POLL_DENIED = "denied"
POLL_REJECTED = "rejected"


class NotLinked(Exception):
    """No usable token (none stored, revoked, or for another client id)."""


class TokenRevoked(Exception):
    """The server refused the refresh token (`invalid_grant`)."""


class ClientRejected(Exception):
    """The server does not know this client id (`invalid_client`)."""


class RefreshThrottled(Exception):
    """TOKEN_REFRESH_PER_MIN reached; retry after `wait_s`."""

    def __init__(self, wait_s: float) -> None:
        self.wait_s = wait_s
        super().__init__("token refresh throttled for %.0f s" % wait_s)


class InvalidResponse(Exception):
    """The cloud answered 2xx with a body that fails validation."""


def _valid_verify_uri(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 512:
        return False
    parts = urllib.parse.urlsplit(value)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == d or host.endswith("." + d) for d in VERIFY_DOMAINS)


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(max(low, min(high, value)))


@dataclass(repr=False)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_at: int
    interval: int

    def __repr__(self) -> str:
        return "<DeviceAuthorization expires_at=%d interval=%d (codes redacted)>" % (
            self.expires_at, self.interval)

    def public_view(self) -> Dict[str, Any]:
        """What link.json and the card may carry — never the device code."""
        return {
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "verification_uri_complete": self.verification_uri_complete,
            "expires_at": self.expires_at,
        }


class OAuthClient:
    def __init__(
        self,
        transport: Transport,
        budget: Budget,
        store: TokenStore,
        *,
        client_id: str,
        host: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._budget = budget
        self._store = store
        self._client_id = client_id
        self._host = host
        self._clock = clock
        self._lock = threading.RLock()
        self._tokens: Optional[TokenSet] = None
        self._refreshes: Deque[float] = collections.deque()
        self._refresh_failures = 0
        self._next_refresh_try = 0.0
        self.revoked_at = 0
        self.problem = ""

    # ── state ────────────────────────────────────────────────────────────
    def load(self) -> LoadResult:
        """Read the store. Tokens issued for another client id or host are
        unusable: they are removed (the integrator changed the application)."""
        result = self._store.load()
        with self._lock:
            self._tokens = None
            self.revoked_at = result.revoked_at
            self.problem = result.problem
            tokens = result.tokens
            if tokens is not None and (tokens.client_id != self._client_id or tokens.host != self._host):
                log.warning("stored Home Connect sign-in belongs to another client id or host — "
                            "removed, link again")
                self._store.clear()
                tokens = None
            self._tokens = tokens
        return result

    @property
    def linked(self) -> bool:
        with self._lock:
            return self._tokens is not None

    @property
    def expires_at(self) -> int:
        with self._lock:
            return self._tokens.expires_at if self._tokens else 0

    def authorization_header(self) -> str:
        with self._lock:
            if self._tokens is None:
                raise NotLinked()
            return "Bearer " + self._tokens.access_token

    # ── requests ─────────────────────────────────────────────────────────
    def _token_request(self, path: str, form: Dict[str, str]) -> Any:
        self._budget.charge(C.KIND_TOKEN)
        try:
            return self._transport.request("POST", path, form=form,
                                           headers={"Accept": "application/json"})
        except HttpError as exc:
            if exc.status == 429:
                self._budget.block_for(exc.retry_after_s(self._clock()) or C.RETRY_AFTER_DEFAULT_S)
            raise

    def start_device_flow(self) -> DeviceAuthorization:
        try:
            data = self._token_request(C.DEVICE_AUTH_PATH,
                                       {"client_id": self._client_id, "scope": C.SCOPES})
        except HttpError as exc:
            if exc.error_key in ("invalid_client", "unauthorized_client"):
                raise ClientRejected(exc.error_key) from None
            raise
        if not isinstance(data, dict):
            raise InvalidResponse("device authorization is not an object")
        device_code = data.get("device_code")
        user_code = data.get("user_code")
        verify = data.get("verification_uri")
        complete = data.get("verification_uri_complete") or ""
        if not (isinstance(device_code, str) and 8 <= len(device_code) <= 1024):
            raise InvalidResponse("device_code missing or malformed")
        if not (isinstance(user_code, str) and _USER_CODE_RE.match(user_code)):
            raise InvalidResponse("user_code missing or malformed")
        if not _valid_verify_uri(verify):
            raise InvalidResponse("verification_uri refused")
        if complete and not _valid_verify_uri(complete):
            raise InvalidResponse("verification_uri_complete refused")
        now = int(self._clock())
        ttl = _clamp_int(data.get("expires_in"), C.DEFAULT_DEVICE_CODE_TTL_S,
                         C.DEVICE_CODE_TTL_MIN_S, C.DEVICE_CODE_TTL_MAX_S)
        interval = _clamp_int(data.get("interval"), C.DEFAULT_POLL_INTERVAL_S,
                              C.POLL_INTERVAL_MIN_S, C.POLL_INTERVAL_MAX_S)
        log.info("Home Connect device flow started: code valid %d s, poll every %d s", ttl, interval)
        return DeviceAuthorization(device_code, user_code, verify, complete, now + ttl, interval)

    def _tokens_from(self, data: Any, previous: Optional[TokenSet]) -> TokenSet:
        if not isinstance(data, dict):
            raise InvalidResponse("token response is not an object")
        access = data.get("access_token")
        refresh = data.get("refresh_token") or (previous.refresh_token if previous else None)
        if not (isinstance(access, str) and 0 < len(access) <= 8192):
            raise InvalidResponse("access token missing or malformed")
        if not (isinstance(refresh, str) and 0 < len(refresh) <= 8192):
            raise InvalidResponse("refresh token missing or malformed")
        now = int(self._clock())
        # 24 h per the public docs; the answer's own value wins.
        ttl = _clamp_int(data.get("expires_in"), 86400, 60, 30 * 86400)
        scope = data.get("scope") if isinstance(data.get("scope"), str) else (
            previous.scope if previous else C.SCOPES)
        return TokenSet(
            access_token=access,
            refresh_token=refresh,
            expires_at=now + ttl,
            scope=scope[:256],
            host=self._host,
            client_id=self._client_id,
            linked_at=previous.linked_at if previous else now,
        )

    def poll(self, auth: DeviceAuthorization) -> str:
        """One token-endpoint poll for `auth`. Returns a POLL_* outcome; on
        POLL_LINKED the tokens are stored. The caller waits `auth.interval`."""
        if self._clock() >= auth.expires_at:
            return POLL_EXPIRED
        try:
            data = self._token_request(C.TOKEN_PATH, {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": auth.device_code,
                "client_id": self._client_id,
            })
        except HttpError as exc:
            key = exc.error_key
            if key == "authorization_pending":
                return POLL_PENDING
            if key == "slow_down":
                auth.interval = min(C.POLL_INTERVAL_MAX_S, auth.interval + C.SLOW_DOWN_STEP_S)
                return POLL_SLOW_DOWN
            if key in ("expired_token", "invalid_grant"):
                return POLL_EXPIRED
            if key == "access_denied":
                return POLL_DENIED
            if key in ("invalid_client", "unauthorized_client"):
                return POLL_REJECTED
            raise
        tokens = self._tokens_from(data, None)
        self._store.save(tokens)
        with self._lock:
            self._tokens = tokens
            self.revoked_at = 0
            self.problem = ""
        log.info("Home Connect account linked")
        return POLL_LINKED

    def needs_refresh(self) -> bool:
        with self._lock:
            return self._tokens is not None and \
                self._clock() >= self._tokens.expires_at - C.REFRESH_BEFORE_S

    def refresh(self) -> None:
        """Refresh the access token now. Raises TokenRevoked, RefreshThrottled,
        BudgetExceeded, NetworkError, HttpError."""
        with self._lock:
            if self._tokens is None:
                raise NotLinked()
            now = self._clock()
            while self._refreshes and now - self._refreshes[0] >= 60:
                self._refreshes.popleft()
            if len(self._refreshes) >= C.TOKEN_REFRESH_PER_MIN:
                raise RefreshThrottled(60 - (now - self._refreshes[0]))
            self._refreshes.append(now)
            previous = self._tokens
            form = {
                "grant_type": "refresh_token",
                "refresh_token": previous.refresh_token,
                "client_id": self._client_id,
            }
        try:
            data = self._token_request(C.TOKEN_PATH, form)
        except HttpError as exc:
            if exc.error_key == "invalid_grant":
                self._revoke()
                raise TokenRevoked() from None
            raise
        tokens = self._tokens_from(data, previous)
        self._store.save(tokens)
        with self._lock:
            self._tokens = tokens
        log.info("Home Connect access token refreshed (valid %d s)", tokens.expires_at - int(self._clock()))

    def ensure_fresh(self) -> None:
        """Refresh when due. While the current token is still valid a failed
        refresh is logged and retried after a doubling pause (the one home of
        that backoff) instead of failing the caller's request; once the token
        has expired the failure is raised. TokenRevoked always raises."""
        if not self.needs_refresh():
            return
        now = self._clock()
        still_valid = now < self.expires_at
        if now < self._next_refresh_try:
            if still_valid:
                return
            raise RefreshThrottled(self._next_refresh_try - now)
        try:
            self.refresh()
        except RefreshThrottled:
            if still_valid:
                return
            raise
        except (NetworkError, HttpError, BudgetExceeded, InvalidResponse) as exc:
            delay = min(C.REFRESH_RETRY_MAX_S, C.REFRESH_RETRY_MIN_S * (2 ** self._refresh_failures))
            self._refresh_failures += 1
            self._next_refresh_try = now + delay
            if not still_valid:
                raise
            log.warning("token refresh failed (%s) — current token still valid, retry in %.0f s",
                        exc, delay)
            return
        self._refresh_failures = 0
        self._next_refresh_try = 0.0

    def _revoke(self) -> None:
        now = int(self._clock())
        with self._lock:
            self._tokens = None
            self.revoked_at = now
        try:
            self._store.mark_revoked(now)
        except OSError as exc:
            log.error("could not replace the revoked token file: %s", exc.strerror or exc)
        log.warning("Home Connect refused the refresh token — access revoked, link again")
