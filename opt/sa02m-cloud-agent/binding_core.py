"""Binding reset — ONE home, both doors.

WHAT THIS IS. "The side that issued this board's cloud identity says the board
is no longer bound, therefore erase that identity, stop presenting the board as
bound, and stand by for a new pairing." That behaviour is written once, here.
WHICH side said it — the Yandex/Alice gateway or the CYNTRON fleet cloud — is a
PARAMETER (a `SourceSpec` handed in by the door), never a second implementation
and never a branch in this file.

WHERE IT LIVES. Authoritative source: ``opt/sa02m-cloud-agent/binding_core.py``.
Byte-identical copy: ``opt/sa02m-alice/sa02m_alice/common/binding_core.py``.
Edit the source and copy it; the quality row ``binding-reset-parity`` (build
beat) keeps them equal and refuses a per-door branch inside this file, because
the realistic drift is not two diverging files but one file that grows a branch
and quietly becomes two implementations sharing a name. Every door difference is
DATA on the descriptor. That ban is textual over the WHOLE file, prose included
— so this prose never quotes the forbidden form.

WHY COPIES AND NOT A SHARED INSTALLED MODULE — one home, not repeated here:
``docs/decisions/binding-reset-one-home.md`` (the install asymmetry, the
rejected third install path, and the precedent this repo already set).

STDLIB ONLY. Nothing project-local may be imported here: the two trees have no
common package, and an import of one door's module would break the other door.
Every door fact — paths, callables, unit names, threshold — arrives on the spec.
That is what makes byte-identity achievable at all.

THE THRESHOLD IS NOT "HOW MANY STRIKES". It says whether the source delivers
EVIDENCE or a VERDICT, and the two doors carry different numbers on purpose:

  * the fleet-cloud door reads evidence — a 403 reason string, a marker in the
    frpc journal — on a channel with no command semantics; three consecutive of
    one class is what turns evidence into a verdict;
  * the Alice door receives a verdict — ``controller_unlink``, delivered once on
    the verified mTLS session, where authenticity is the channel's and never the
    payload's. Making it wait for three would mean waiting forever: the gateway
    disconnects right after sending it, so a second event never arrives in the
    same session.

A reader who takes "the threshold is shared" literally and hard-codes 3 ships
the 1.0.6.27 bug back.

THE DANGEROUS DIRECTION IS FAIL-OPEN, and everything here leans the other way. A
board is never unbound by DOUBT — a network blip, a timeout, a 5xx, a DNS
failure, an unrecognised 403, an unreadable journal, an unsavable cursor, a
gateway outage. Every ambiguity resolves to "no refusal". The wipe's own error
path leans the same way from the opposite side: an ``OSError`` RAISES, the board
reports ``unlink_failed`` and keeps retrying, and a binding that could not be
erased never reads as erased.

NON-GOAL, stated because it is exactly the wire a helpful editor would add: the
fleet control profile (``sa02m-cloud-control.service``, the smart-home client's
second profile) does NOT stand down. It holds no identity of its own — it
authenticates with the agent's ``device_id`` + ``device_secret``, and the
agent's own stand-down erases those. Two writers to one binding is the
over-count class reborn, with two processes racing to erase one identity. Its
behaviour on a refusal is unchanged: write ``error`` with the fleet's reason and
back off on the existing ladder.

BLAST RADIUS. A stand-down may erase the identity issued by the source that
refused it, and nothing else. Each door's ``wipe`` enumerates its own files; the
boundary is asserted byte-identically by each door's suite. There is no
recursive delete here and no path literal at all.
"""

import collections
import logging
import os
import subprocess
import time

log = logging.getLogger("sa02m.binding")

# ── The door descriptor ───────────────────────────────────────────────────────
# Every field is data or a callable the door supplies. This file reads them; it
# never asks which door it is serving.
#
#   name             reported and logged, never compared
#   threshold        evidence (>1) vs verdict (1) — see the module docstring
#   wipe             () -> {"removed": [...], "absent": [...]}; raises OSError
#                    when a file could not be erased (never reports success)
#   stop_tunnel      () -> None; a no-op is honest where a door has no separate
#                    transport unit
#   read_marker      () -> (stamp, cls, reason); ("", "", "") when this door
#                    considers itself bound or has no marker
#   write_marker     (stamp, cls, reason) -> None; durable, survives a reboot
#   clear_marker     () -> None; called when the board is bound again
#   write_pending    (cls, reason) -> None; durably record a CONFIRMED refusal
#                    whose wipe failed, for a retry that another PROCESS must
#                    run. A door whose retry runs in the process that failed
#                    supplies an honest no-op instead of a branch here.
#   read_pending     () -> (cls, reason); ("", "") when nothing is pending
#   write_status     (state, **kw) -> None; the door's own status file
#   state_for_class  (cls) -> one of `states`
#   states           the states THIS door may write; a state outside it is a
#                    programming error and raises here rather than reaching a
#                    card that has no label for it
#   classify         (evidence) -> refusal class, or "" for "not a refusal"
SourceSpec = collections.namedtuple("SourceSpec", (
    "name",
    "threshold",
    "wipe",
    "stop_tunnel",
    "read_marker",
    "write_marker",
    "clear_marker",
    "write_pending",
    "read_pending",
    "write_status",
    "state_for_class",
    "states",
    "classify",
))

# The state a failed wipe writes. Spelled out as a LITERAL at both call sites
# below as well: each door's status-enum contract test scans the sources for
# `write_status("…")` literals, and a constant would hide the state from the
# very check that proves the card has a label for it.
STATE_UNLINK_FAILED = "unlink_failed"

# (cls, reason) of a stand-down whose wipe failed — the door finishes it once
# the wipe succeeds. Module-level because the failure crosses the loop boundary.
# It does NOT cross a PROCESS boundary, which is why every door also gets a
# durable twin on the descriptor (`write_pending` / `read_pending`): the local
# unlink button runs in a CGI that exits the moment it answers, and the retry
# is then the client's to run.
PENDING_STAND_DOWN = {"cls": "", "reason": ""}


class RefusalTracker(object):
    """Counts CONSECUTIVE refusal EVENTS of one class; any success resets.

    An event is one refused heartbeat, one NEW refusal line in a journal (the
    cursor guarantees a line is fed here once, never re-read on the next tick),
    or one delivered unlink verdict. A refusal of a different class restarts the
    count at one (N refusals are only meaningful when they all say the same
    thing). On an evidence channel a network error or a 5xx is neither a refusal
    nor a success and leaves the count alone; a tick with no new refusal line IS
    a success.
    """

    def __init__(self, threshold):
        self.threshold = int(threshold)
        self.cls = ""
        self.count = 0

    def note_success(self):
        self.cls = ""
        self.count = 0

    def note_refusal(self, cls):
        """Record one refusal; True when the threshold is reached."""
        if not cls:
            return False
        if cls == self.cls:
            self.count += 1
        else:
            self.cls = cls
            self.count = 1
        return self.count >= self.threshold


def tracker_for(spec):
    """The counter this source's threshold calls for."""
    return RefusalTracker(spec.threshold)


def refusal_verdict(spec, tracker, evidence):
    """One piece of evidence (or one delivered verdict) → the class to stand
    down on, or "".

    Fail-closed by construction: what the door's own classifier does not
    recognise is not a refusal and does not touch the counter. Only the door
    knows what counts as a SUCCESS on its channel, so resetting the counter
    stays at the door's call site.
    """
    cls = spec.classify(evidence)
    if not cls:
        return ""
    return cls if tracker.note_refusal(cls) else ""


# ── The wipe ──────────────────────────────────────────────────────────────────
def wipe_binding(paths):
    """Delete the identity files named by `paths()`. Idempotent; a missing file
    is reported as `absent`, never hidden; any other OSError is logged and
    RAISED — a binding that could not be erased must not read as erased.

    Only basenames are logged: these files hold a private key and a token.
    """
    removed, absent = [], []
    for path in paths():
        name = os.path.basename(path)
        try:
            os.unlink(path)
            removed.append(name)
        except FileNotFoundError:
            absent.append(name)
        except OSError as e:
            # A partial wipe: say what DID go before raising, or that record
            # is lost with the exception.
            log.error("stand-down: could not remove %s: %s (already removed: %s; absent: %s)",
                      path, e, ", ".join(removed) or "nothing", ", ".join(absent) or "nothing")
            raise
    return {"removed": removed, "absent": absent}


def stamp_now():
    """The durable marker's time format — ISO-8601 UTC, both doors."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _checked_state(spec, cls):
    state = spec.state_for_class(cls)
    if state not in spec.states:
        raise ValueError("source %r may not write state %r (declared: %s)"
                         % (spec.name, state, ", ".join(spec.states)))
    return state


def stand_down(spec, cls, reason):
    """The source refused this board: de-enrol locally.

    Stop the transport, erase the identity files, record the durable marker and
    write the status the card reads. Returns "repair" — the caller drops to its
    standby/wait path, where a new pairing is one button press away and needs no
    SSH — or "unlink_failed" when the wipe itself failed, which the caller
    retries (`retry_wipe`) while the card keeps saying so.
    """
    # Imperative, not past tense: this line is printed BEFORE the wipe is
    # attempted, and the run it describes may be the one where the erase fails.
    # «binding erased» belongs where the erase succeeded — finish_stand_down.
    log.error("%s refused this device (%s: %s) — standing down: stopping the "
              "transport and erasing the binding", spec.name, cls, reason)
    spec.stop_tunnel()
    try:
        wiped = spec.wipe()
    except OSError as e:
        # The binding is STILL on disk: say so — never "bound", never "erased".
        PENDING_STAND_DOWN.update({"cls": cls, "reason": reason})
        _persist_pending(spec, cls, reason)
        log.error("stand-down: binding NOT erased: %s", e)
        spec.write_status("unlink_failed", reason="wipe_failed", detail=str(e),
                          reason_class=cls, refusal=reason)
        return STATE_UNLINK_FAILED
    return finish_stand_down(spec, cls, reason, wiped)


def _persist_pending(spec, cls, reason):
    """Hand the failed wipe to whoever will retry it, across a process boundary.

    BEST EFFORT, and deliberately so: this runs on the path where writing to
    disk has just failed once already, so a second failure must not turn a
    reported error into a crash. The in-memory record above still stands for a
    door whose retry runs in this process; what is lost when this fails is only
    the hand-off to another process, and the caller is told so by reading the
    record back — never by this function claiming success.
    """
    try:
        spec.write_pending(cls, reason)
    except OSError as e:
        log.error("stand-down: the pending record could not be persisted (%s) — "
                  "the retry cannot be handed to another process", e)


def adopt_pending(spec):
    """Take over a stand-down another process recorded and could not finish.

    The local «Отвязать» runs in a CGI that answers and exits; if its wipe
    failed, the durable record is the ONLY channel to the long-running client,
    whose own module state knows nothing about it. True when this call adopted
    one. Never overrides a pending stand-down this process already owns.
    """
    if PENDING_STAND_DOWN["cls"]:
        return False
    cls, reason = spec.read_pending()
    if not cls:
        return False
    log.warning("adopting a stand-down recorded by another process (%s: %s) — "
                "its wipe failed and the binding is still on disk", cls, reason)
    PENDING_STAND_DOWN.update({"cls": cls, "reason": reason})
    return True


def finish_stand_down(spec, cls, reason, wiped):
    """Bookkeeping after a SUCCESSFUL wipe: durable marker, then status."""
    state = _checked_state(spec, cls)
    stamp = stamp_now()
    spec.write_marker(stamp, cls, reason)
    spec.write_status(state, reason=reason, reason_class=cls, unlinked_at=stamp)
    log.warning("binding erased: removed %s; already absent %s",
                ", ".join(wiped["removed"]) or "nothing",
                ", ".join(wiped["absent"]) or "nothing")
    return "repair"


def retry_wipe(spec):
    """ONE retry of a failed wipe. "repair" once it succeeds; "" while it keeps
    failing, with the error state re-written so the card does not go stale.

    The durable record is re-written on every failing retry, not only on the
    first: the write that persists it can itself be the thing that is failing
    (a read-only filesystem fails both), and a later tick may be the one that
    lands it. On success `finish_stand_down` overwrites the record with the
    completed marker, which is what makes `read_pending` go quiet.
    """
    cls, reason = PENDING_STAND_DOWN["cls"], PENDING_STAND_DOWN["reason"]
    try:
        wiped = spec.wipe()
    except OSError as e:
        log.error("stand-down retry: binding still NOT erased: %s", e)
        _persist_pending(spec, cls, reason)
        spec.write_status("unlink_failed", reason="wipe_failed", detail=str(e),
                          reason_class=cls, refusal=reason)
        return ""
    PENDING_STAND_DOWN.update({"cls": "", "reason": ""})
    return finish_stand_down(spec, cls, reason, wiped)


def retry_wipe_loop(spec, interval, sleep=time.sleep):
    """Keep the error state on the card and retry the wipe every `interval`
    until it succeeds, then finish the stand-down."""
    while True:
        sleep(interval)
        rc = retry_wipe(spec)
        if rc:
            return rc


def restore_stand_down_status(spec):
    """On start in the stand-down state, rebuild the status file from the
    durable marker (`/run` is tmpfs — after a reboot the card would otherwise
    show a bare "not connected" for a board whose binding was erased)."""
    stamp, cls, reason = spec.read_marker()
    if not stamp:
        return False
    state = _checked_state(spec, cls)
    spec.write_status(state, reason=reason or cls, reason_class=cls,
                      unlinked_at=stamp, restored=True)
    return True


# ── Evidence path — journal cursor ────────────────────────────────────────────
# Entered only by a door that reads EVIDENCE from a unit journal. A door that
# receives a verdict never comes here: its "count each piece exactly once" is
# bought by idempotence (the wipe reports `absent`, the marker write is a set,
# not an append), not by a cursor.
#
# In-memory twin of the cursor file. Within one process it is always the NEWER
# position (it moves before the file write), so read_cursor prefers it; the file
# matters only at process start. What this buys is "never re-COUNTED": with the
# file unwritable the process keeps reading from the twin, so a tick whose
# cursor could not be saved is clean and a line can never add a second count.
_MEM_CURSOR = {"cursor": ""}
# The `--since` window used whenever no cursor is at hand. It is spent by a read
# that yields a cursor and RE-ARMED by every read that does not (a failed
# journalctl, an empty or footerless answer, a dropped stale cursor) — so
# journalctl is invoked on every tick for the life of the process, never once.
# Recount-safe: every re-arm follows a tick that returned "" and reset the
# counter. Left empty at import; the door seeds it with its own start time.
_SINCE_FROM = {"at": ""}
_WARNED = set()


def _warn_once(key, msg, *args):
    if key in _WARNED:
        return
    _WARNED.add(key)
    log.warning(msg, *args)


def read_cursor(path):
    if _MEM_CURSOR["cursor"]:
        return _MEM_CURSOR["cursor"]
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def save_cursor(cursor, path):
    """Persist the cursor; the in-memory twin moves first. False on a failed
    write — the caller then treats the tick as CLEAN: inability to save the
    cursor must never turn into re-reading the same window."""
    _MEM_CURSOR["cursor"] = cursor
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(cursor)
        os.replace(tmp, path)
        return True
    except OSError as e:
        _warn_once("save:" + path, "journal cursor not saved (%s): %s — the tick counts as "
                   "clean; reads continue from the in-memory position, so nothing "
                   "is counted twice", path, e)
        return False


def drop_cursor(path):
    """A cursor journalctl refuses (its entry rotated out): drop it and re-arm
    the one-shot window from NOW — nothing already counted can be re-read."""
    _MEM_CURSOR["cursor"] = ""
    try:
        os.unlink(path)
    except OSError:
        pass
    _SINCE_FROM["at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def journal_reject_reason(unit, markers, cursor_file):
    """The server's refusal reason from NEW lines of `unit`'s journal, or "".

    The journal is read BY CURSOR: `--after-cursor <last line read>` with
    `--show-cursor` (the new cursor is saved to `cursor_file` and to memory),
    and the very first time by `--since <the door's start>`. Every refusal line
    is seen exactly once; a tick with no NEW refusal line is a success.

    FAIL-CLOSED: any inability to obtain or save the cursor means "no new
    refusals" — a clean tick. An answer without a `-- cursor:` footer is clean.
    An unsaved cursor is clean, and the next read continues from the in-memory
    position. BUT the journal is read on EVERY tick: the `--since` window is
    spent only by a read that returned a cursor, and every exit without one
    re-arms it — otherwise one unlucky first read would silence the detach
    signal until the process restarts. That gives no double count: every re-arm
    follows a tick that returned "" and reset the counter. There is no `-n`
    limit: on the cursor path the window is bounded anyway, and on the `--since`
    path every read is clean until a cursor appears.

    Honesty over convenience: a refusal is reported ONLY when the server stated
    one. An unreachable cloud, a dead network, a crashed transport or an
    unreadable journal are not refusals ("" — as in "no new refusals"; in the
    direction of erasing a binding that is never wrong).
    """
    cursor = read_cursor(cursor_file)
    since = ""
    if cursor:
        args = ["journalctl", "-u", unit, "--after-cursor", cursor,
                "--show-cursor", "--no-pager"]
    else:
        since = _SINCE_FROM["at"] or time.strftime("%Y-%m-%d %H:%M:%S")
        args = ["journalctl", "-u", unit, "--since", since,
                "--show-cursor", "--no-pager"]
        # Spent by THIS read; every exit below that yields no cursor puts it
        # back, so the next tick reads again (same window — nothing in it was
        # counted, so nothing can be counted twice).
        _SINCE_FROM["at"] = ""

    def _rearm():
        if not cursor:
            _SINCE_FROM["at"] = since

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except Exception as e:
        log.debug("journalctl unavailable: %s", e)
        _rearm()
        return ""
    if r.returncode != 0:
        if cursor:
            # A stale cursor fails on every tick and would silently kill the
            # only detach signal: drop it, read from NOW next time.
            log.warning("journalctl refused cursor %s (rc=%d) — dropping it, "
                        "next read starts from now", cursor, r.returncode)
            drop_cursor(cursor_file)
        else:
            log.warning("journalctl failed (rc=%d): %s", r.returncode,
                        (r.stderr or "").strip()[:200])
            _rearm()
        return ""
    body = []
    new_cursor = ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("-- cursor:"):
            new_cursor = line.split(":", 1)[1].strip()
        else:
            body.append(line)
    if not new_cursor:
        # An empty window (a connected, quiet transport — the ordinary case on
        # a restart) or a footerless answer: clean tick, read again next.
        if body:
            _warn_once("nofooter", "journalctl returned %d line(s) without a cursor "
                       "footer — counted as clean", len(body))
        _rearm()
        return ""
    if not save_cursor(new_cursor, cursor_file):
        return ""
    text = "\n".join(body).lower()
    # The LATEST refusal line wins when several new lines carry markers.
    found, at = "", -1
    for marker in markers:
        pos = text.rfind(marker)
        if pos > at:
            found, at = marker, pos
    return found
