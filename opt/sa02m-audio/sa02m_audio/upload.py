"""HTTP POST of a freshly recorded clip to the Operator's URL (contract
§Передача).

* WHAT: only the clip the daemon has just stored, by its in-memory id, as the
  bytes copied from the write descriptor before it was closed — never a file
  opened later by name (on a removable medium anyone can replace one).
* WHERE: `upload_url` from the conf. The URL policy is the rules engine's
  `sa02m_rules.http_guard` (decision M1, one home with the cloud half):
  http/https only, no `user:pass@`, loopback / link-local / reserved refused,
  judged after resolution and again for every redirect hop. The host urllib
  would dial (after percent-decoding) must be that checked host; a mismatch
  is `refused` and nothing is sent. That module not importable ⇒
  `unavailable`, nothing is sent, the file is kept.
* HOW: body = the WAV; `Content-Type: audio/wav`, `X-SA02M-Recording-Id`,
  `X-SA02M-Duration-Ms`, `X-SA02M-Peak-Dbfs`, and `Authorization: Bearer <t>`
  when a token is set. Success = HTTP 2xx. A 2xx whose Content-Type is
  application/json may carry absolute commands (voice.commands_from_body);
  any other body is still success and carries no commands. Each attempt is
  bounded by
  UPLOAD_TIMEOUT_S; attempts at UPLOAD_DELAYS_S (0, 10, 60 s), then `failed`
  and the file stays. A refused URL is `refused` at once (no retry). Only a
  307/308 redirect is followed (the body is re-sent), at most
  UPLOAD_MAX_REDIRECTS hops, each re-judged; any other 3xx is a failed attempt.
  TLS verification is always on.
* ONE AT A TIME: a worker thread, a queue of UPLOAD_QUEUE_MAX; a clip that does
  not fit is `failed` at once. The worker only hands results back to the
  daemon's main thread, which owns every publish and file decision.
* SECRETS: the token never reaches a log, an event or MQTT; the log names the
  URL's host only.
"""

from __future__ import annotations

import importlib
import logging
import queue
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import constants as C
from . import voice

log = logging.getLogger("sa02m_audio.upload")


@dataclass
class UploadJob:
    rec_id: str
    medium: str
    data: bytes
    duration_ms: int
    peak_dbfs: int
    url: str
    token: str


@dataclass
class UploadResult:
    rec_id: str
    medium: str
    state: str
    detail: str = ""
    # The recording's own measurements, rewritten into the sidecar when the
    # attempt ends. None only for a result built without a job.
    duration_ms: Optional[int] = None
    peak_dbfs: Optional[int] = None
    # Absolute commands from a JSON 2xx, already checked for shape. () when
    # the reply was not a command document. The daemon applies them.
    commands: tuple = ()


class Attempt(tuple):
    """(state, detail) plus `.commands`. Two-item unpack keeps old callers."""

    def __new__(cls, state: str, detail: str, commands: tuple = ()):
        return tuple.__new__(cls, (state, detail))

    def __init__(self, state: str, detail: str, commands: tuple = ()) -> None:
        self.commands = tuple(commands)


def load_guard(rules_dir: Optional[str] = None) -> Optional[Any]:
    """sa02m_rules.http_guard, or None (rules not installed / broken)."""
    path = rules_dir or C.RULES_DIR
    if path not in sys.path:
        sys.path.append(path)
    try:
        guard = importlib.import_module("sa02m_rules.http_guard")
    except Exception as exc:  # any failure to import is "unavailable"
        log.error("URL policy sa02m_rules.http_guard not importable: %s", type(exc).__name__)
        return None
    if not callable(getattr(guard, "check_url", None)):
        log.error("sa02m_rules.http_guard has no check_url — treated as unavailable")
        return None
    return guard


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "?")[:80]
    except ValueError:
        return "?"


def _dial_host(req_host: str) -> str:
    """Host urllib will resolve: userinfo and port stripped, lowercased.

    `Request` percent-decodes the host (`http://127%2e0%2e0%2e1/` is dialed
    as `127.0.0.1`). The URL policy judged `urlsplit().hostname`, which does
    not decode. Those two strings must be the same host or the connection
    is not the one that was checked.
    """
    host = req_host or ""
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end > 1 else host
    elif ":" in host:
        host = host.split(":", 1)[0]
    return host.lower()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # every hop is judged here, never followed by urllib


def default_opener() -> Any:
    return urllib.request.build_opener(_NoRedirect())


def post_once(job: UploadJob, guard: Any, opener: Any, timeout: float) -> Attempt:
    """One attempt: state ok | refused | retry. `.commands` only on a JSON 2xx."""
    url = job.url
    for _hop in range(C.UPLOAD_MAX_REDIRECTS + 1):
        try:
            reason = guard.check_url(url, resolve=True)
        except Exception as exc:
            return Attempt("retry", "%s: %s" % (host_of(url), type(exc).__name__))
        if reason:
            return Attempt(C.UP_REFUSED, "%s: %s" % (host_of(url), reason))
        try:
            checked = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            return Attempt(C.UP_REFUSED, "%s: bad url" % host_of(url))
        commands: tuple = ()
        try:
            req = urllib.request.Request(url, data=job.data, method="POST")
            if _dial_host(req.host or "") != checked:
                return Attempt(C.UP_REFUSED, "%s: dial host was not the host that was checked" % host_of(url))
            req.add_header("Content-Type", "audio/wav")
            req.add_header("X-SA02M-Recording-Id", job.rec_id)
            req.add_header("X-SA02M-Duration-Ms", str(job.duration_ms))
            req.add_header("X-SA02M-Peak-Dbfs", str(job.peak_dbfs))
            if job.token:
                req.add_header("Authorization", "Bearer " + job.token)
            with opener.open(req, timeout=timeout) as resp:
                code = int(resp.status)
                ctype = resp.headers.get("Content-Type") or ""
                payload = resp.read(C.UPLOAD_RESPONSE_MAX)
                if 200 <= code < 300 and "application/json" in ctype.lower():
                    commands = voice.commands_from_body(payload, job.rec_id)
        except urllib.error.HTTPError as exc:
            try:
                code = int(exc.code)
                location = exc.headers.get("Location") if exc.headers else None
                try:
                    exc.read(C.UPLOAD_RESPONSE_MAX)
                except Exception:
                    pass
            finally:
                exc.close()
            if code in (307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                continue
        except Exception as exc:  # URLError, timeout, TLS, a URL Request rejects
            return Attempt("retry", "%s: %s" % (host_of(url), type(exc).__name__))
        if 200 <= code < 300:
            return Attempt(C.UP_OK, "%s: HTTP %d" % (host_of(url), code), commands)
        return Attempt("retry", "%s: HTTP %d" % (host_of(url), code))
    return Attempt("retry", "%s: too many redirects" % host_of(job.url))


class Uploader:
    """The worker. `deliver(UploadResult)` is called from the worker thread;
    the daemon only enqueues it."""

    def __init__(self, deliver: Callable[[UploadResult], Any], stop: threading.Event, *,
                 guard: Any, opener: Any = None, delays=C.UPLOAD_DELAYS_S,
                 timeout: float = C.UPLOAD_TIMEOUT_S) -> None:
        self._deliver = deliver
        self._stop = stop
        self._guard = guard
        self._opener = opener or default_opener()
        self._delays = tuple(delays)
        self._timeout = timeout
        self._jobs: "queue.Queue[UploadJob]" = queue.Queue(maxsize=C.UPLOAD_QUEUE_MAX)
        self._thread = threading.Thread(target=self._run, name="audio-upload", daemon=True)
        self._thread.start()

    def submit(self, job: UploadJob) -> str:
        """`pending`, or `failed` when the queue is full."""
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            log.warning("upload queue full — clip %s not uploaded", job.rec_id)
            return C.UP_FAILED
        return C.UP_PENDING

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self._deliver(self._attempts(job))

    def _result(self, job: UploadJob, state: str, detail: str,
                commands: tuple = ()) -> UploadResult:
        return UploadResult(job.rec_id, job.medium, state, detail,
                            job.duration_ms, job.peak_dbfs, commands)

    def _attempts(self, job: UploadJob) -> UploadResult:
        detail = ""
        previous = 0.0
        for n, at in enumerate(self._delays, 1):
            if self._stop.wait(max(0.0, at - previous)):
                return self._result(job, C.UP_FAILED, "daemon stopped")
            previous = at
            attempt = post_once(job, self._guard, self._opener, self._timeout)
            state, detail = attempt
            log.info("upload %s attempt %d/%d: %s", job.rec_id, n, len(self._delays), detail)
            if state in (C.UP_OK, C.UP_REFUSED):
                return self._result(job, state, detail, attempt.commands)
        return self._result(job, C.UP_FAILED, "%s after %d attempts" % (detail, len(self._delays)))
