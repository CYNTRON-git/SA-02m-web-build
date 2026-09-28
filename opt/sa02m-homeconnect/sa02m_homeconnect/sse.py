"""The Home Connect event stream: `GET /api/homeappliances/events` (SSE).

Two parts:

* `SseParser` — a pure line parser (text/event-stream): `event:`, `data:`
  (joined with newlines), `id:`, comments; a blank line dispatches. Event
  types: KEEP-ALIVE, STATUS, EVENT, NOTIFY, CONNECTED, DISCONNECTED, PAIRED,
  DEPAIRED. Some events arrive without `id`; it is filled from `data.haId`
  (or the first item's `haId`). Line and event sizes are capped — an
  oversized stream is a protocol error (reconnect), never unbounded memory.
* `EventStream` — the reader thread: connect (charged to the budget as
  `sse`), read with a SSE_READ_TIMEOUT_S socket deadline (the server sends a
  keep-alive about every 55 s, so a silent socket past 120 s is dead),
  reconnect with exponential backoff 60 s → 30 min ± jitter. The backoff
  resets only after a connection stayed up SSE_BACKOFF_MAX_S, so a server
  that accepts and drops cannot turn the reconnect into a call storm.
  Events and up/down transitions go to the daemon's queue; the thread never
  touches MQTT or status itself.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import constants as C
from .api_client import backoff_delay
from .budget import Budget, BudgetExceeded
from .oauth import NotLinked, OAuthClient, RefreshThrottled, TokenRevoked
from .transport import HttpError, NetworkError, Transport

log = logging.getLogger("sa02m_homeconnect.sse")


class ProtocolError(Exception):
    pass


@dataclass
class SseEvent:
    type: str
    id: str
    data: Any


class SseParser:
    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._type = ""
        self._id = ""
        self._data: list = []
        self._size = 0
        self._seen = False

    def feed_line(self, line: str) -> Optional[SseEvent]:
        """One line WITHOUT its terminator. Returns an event on a blank line."""
        if len(line) > C.SSE_MAX_LINE:
            raise ProtocolError("SSE line longer than %d" % C.SSE_MAX_LINE)
        if line == "":
            if not self._seen:
                return None
            event = self._build()
            self._reset()
            return event
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        self._seen = True
        if field == "event":
            self._type = value.strip().upper()
        elif field == "data":
            self._size += len(value) + 1
            if self._size > C.SSE_MAX_EVENT:
                raise ProtocolError("SSE event larger than %d" % C.SSE_MAX_EVENT)
            self._data.append(value)
        elif field == "id":
            self._id = value.strip()
        # `retry` and unknown fields are ignored (RFC: the client picks its own backoff).
        return None

    def _build(self) -> SseEvent:
        raw = "\n".join(self._data)
        data: Any = None
        if raw.strip():
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
        event_id = self._id
        if not event_id and isinstance(data, dict):
            ha = data.get("haId")
            if not isinstance(ha, str):
                items = data.get("items")
                if isinstance(items, list):
                    ha = next((i.get("haId") for i in items
                               if isinstance(i, dict) and isinstance(i.get("haId"), str)), None)
            if isinstance(ha, str):
                event_id = ha
        return SseEvent(self._type or "MESSAGE", event_id[:128], data)


# Messages the stream thread puts on the daemon queue: ("event", SseEvent),
# ("up", None), ("down", reason: str), ("unlinked", reason: str).
STREAM_UP = "up"
STREAM_DOWN = "down"
STREAM_EVENT = "event"
STREAM_UNLINKED = "unlinked"


class EventStream:
    def __init__(
        self,
        transport: Transport,
        budget: Budget,
        oauth: OAuthClient,
        out: "queue.Queue[Any]",
        *,
        stop: Optional[threading.Event] = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        read_timeout: float = C.SSE_READ_TIMEOUT_S,
        backoff_min: float = C.SSE_BACKOFF_MIN_S,
        backoff_max: float = C.SSE_BACKOFF_MAX_S,
    ) -> None:
        self._transport = transport
        self._budget = budget
        self._oauth = oauth
        self._out = out
        self.stop_event = stop or threading.Event()
        self._clock = clock
        self._rng = rng
        self._read_timeout = read_timeout
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._resp: Any = None
        self._thread: Optional[threading.Thread] = None
        self.connects = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="hc-sse", daemon=True)
        self._thread.start()

    def stop(self, join_s: float = 3.0) -> None:
        self.stop_event.set()
        resp = self._resp
        if resp is not None:
            _shutdown_response(resp)
        if self._thread is not None:
            self._thread.join(join_s)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── the reader loop ──────────────────────────────────────────────────
    def _emit(self, kind: str, payload: Any) -> None:
        try:
            self._out.put_nowait((kind, payload))
        except queue.Full:
            log.error("event queue full — dropping a %s message", kind)

    def _connect(self) -> Any:
        self._oauth.ensure_fresh()
        self._budget.charge(C.KIND_SSE)
        self.connects += 1
        headers = {"Accept": C.ACCEPT_SSE, "Authorization": self._oauth.authorization_header()}
        return self._transport.open_stream(C.EVENTS_PATH, headers=headers,
                                           read_timeout=self._read_timeout)

    def _read(self, resp: Any) -> None:
        parser = SseParser()
        while not self.stop_event.is_set():
            raw = resp.readline(C.SSE_MAX_LINE + 2)
            if not raw:
                raise EOFError("stream closed by the server")
            if len(raw) > C.SSE_MAX_LINE + 1 and not raw.endswith(b"\n"):
                raise ProtocolError("SSE line longer than %d" % C.SSE_MAX_LINE)
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            event = parser.feed_line(line)
            if event is not None:
                self._emit(STREAM_EVENT, event)

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # a reader bug must end loudly, not silently
            log.exception("event stream reader failed")
            self._emit(STREAM_DOWN, type(exc).__name__)

    def _run(self) -> None:
        attempt = 0
        refreshed = False
        while not self.stop_event.is_set():
            wait_s: float
            try:
                resp = self._connect()
            except BudgetExceeded as exc:
                self._emit(STREAM_DOWN, exc.reason)
                wait_s = max(1.0, min(300.0, exc.until - self._clock()))
                if self.stop_event.wait(wait_s):
                    return
                continue
            except (NotLinked, TokenRevoked) as exc:
                self._emit(STREAM_UNLINKED, type(exc).__name__)
                return
            except RefreshThrottled as exc:
                self._emit(STREAM_DOWN, "refresh_throttled")
                if self.stop_event.wait(max(1.0, exc.wait_s)):
                    return
                continue
            except HttpError as exc:
                if exc.status == 401 and not refreshed:
                    refreshed = True
                    try:
                        self._oauth.refresh()
                    except (TokenRevoked, NotLinked) as inner:
                        self._emit(STREAM_UNLINKED, type(inner).__name__)
                        return
                    except Exception as inner:  # budget / network / throttle: back off below
                        log.warning("token refresh after a stream 401 failed: %s", inner)
                    else:
                        continue
                if exc.status == 429:
                    self._budget.block_for(exc.retry_after_s(self._clock()) or C.RETRY_AFTER_DEFAULT_S)
                self._emit(STREAM_DOWN, "http_%d" % exc.status)
                wait_s = backoff_delay(attempt, self._backoff_min, self._backoff_max, self._rng)
                attempt += 1
                log.warning("event stream refused (HTTP %d %s) — retry in %.0f s", exc.status,
                            exc.error_key, wait_s)
                if self.stop_event.wait(wait_s):
                    return
                continue
            except NetworkError as exc:
                self._emit(STREAM_DOWN, "network")
                wait_s = backoff_delay(attempt, self._backoff_min, self._backoff_max, self._rng)
                attempt += 1
                log.warning("event stream unreachable (%s) — retry in %.0f s", exc, wait_s)
                if self.stop_event.wait(wait_s):
                    return
                continue
            refreshed = False
            self._resp = resp
            up_since = self._clock()
            self._emit(STREAM_UP, None)
            log.info("Home Connect event stream connected")
            reason = "closed"
            try:
                self._read(resp)
            except (socket.timeout, TimeoutError):
                reason = "keepalive_timeout"
            except (EOFError, ProtocolError, OSError, ValueError) as exc:
                reason = type(exc).__name__
            finally:
                self._resp = None
                try:
                    resp.close()
                except Exception:
                    pass
            if self.stop_event.is_set():
                return
            if self._clock() - up_since >= self._backoff_max:
                attempt = 0
            self._emit(STREAM_DOWN, reason)
            wait_s = backoff_delay(attempt, self._backoff_min, self._backoff_max, self._rng)
            attempt += 1
            log.warning("event stream lost (%s) — reconnect in %.0f s", reason, wait_s)
            if self.stop_event.wait(wait_s):
                return


def _shutdown_response(resp: Any) -> None:
    """Unblock a readline() in the reader thread by shutting the socket down.
    The reader then sees EOF and closes the response itself — closing it from
    this thread would race http.client's own bookkeeping."""
    try:
        sock = resp.fp.raw._sock  # http.client.HTTPResponse → SocketIO → socket
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
