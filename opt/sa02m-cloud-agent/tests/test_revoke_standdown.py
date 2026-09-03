"""The cloud-agent revoke stand-down (contract §4; mirror of the Alice binding
reset in e15b44d).

The dangerous direction is the INVERSE: a stand-down that fires on a network
blip would erase the binding of a working board. So the absence assertions
(network / 5xx / DNS / unrecognised 403 never stand down; the wipe touches the
two identity files and nothing else) outnumber the positive ones on purpose.

F1 (threat-model): the heartbeat stays send-only. Its call site never captures
api_post's result (pinned by regex in test_agent.py); what the agent learns is
the (status, error) of a NON-200 response left on a side channel, and a 200
body is never read for it — pinned here.

`SA02M_AGENT_PATH` overrides the agent file under test, the same override
idiom the repo's dev harnesses use (`<NAME>_SRC`) — it is how the red-on-old-
code proof runs this file against a saved pre-change copy.
"""
import importlib.util
import inspect
import io
import json
import os
import sys
import urllib.error

import pytest

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.environ.get("SA02M_AGENT_PATH") or os.path.join(AGENT_DIR, "sa02m-cloud-agent.py")

_spec = importlib.util.spec_from_file_location("sa02m_cloud_agent_under_test", AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)

with open(AGENT_PATH, encoding="utf-8") as _f:
    AGENT_SOURCE = _f.read()


class _Budget(BaseException):
    """Raised (not Exception — the loops swallow those) when the code under
    test will not terminate: a stand-down that never fires must FAIL by name,
    never hang the suite."""


class _SystemctlRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kw):
        self.calls.append(tuple(args))
        return 0


@pytest.fixture
def cloud_fs(tmp_path, monkeypatch):
    """An enrolled board on a temp filesystem: agent.conf, secret, frpc.toml,
    status file; systemctl and the serial/version readers stubbed."""
    etc = tmp_path / "etc"
    run = tmp_path / "run"
    etc.mkdir()
    run.mkdir()
    conf = etc / "agent.conf"
    monkeypatch.setattr(agent, "DEVICE_SECRET_FILE", str(etc / "device_secret"))
    monkeypatch.setattr(agent, "FRPC_CONFIG", str(etc / "frpc.toml"))
    monkeypatch.setattr(agent, "PAIR_REQUEST_FILE", str(etc / "pair_request"))
    monkeypatch.setattr(agent, "ACTIVATION_TOKEN_FILE", str(etc / "activation_token"))
    monkeypatch.setattr(agent, "STATUS_FILE", str(run / "status.json"))
    monkeypatch.setattr(agent, "CURSOR_FILE", str(run / "frpc.cursor"), raising=False)
    getattr(agent, "PENDING_STAND_DOWN", {}).update({"cls": "", "reason": ""})
    getattr(agent, "_MEM_CURSOR", {}).update({"cursor": ""})
    getattr(agent, "_SINCE_FROM", {}).update({"at": getattr(agent, "AGENT_STARTED_AT", "")})
    getattr(agent, "_WARNED", set()).clear()
    monkeypatch.setattr(agent, "get_serial", lambda: "SN1")
    monkeypatch.setattr(agent, "get_fw_version", lambda: "1.0.6.26")
    systemctl = _SystemctlRecorder()
    monkeypatch.setattr(agent, "_systemctl", systemctl)
    cfg = agent.load_config("/nonexistent")
    cfg["cloud"]["api_url"] = "https://bench.local/api/v1"
    cfg["cloud"]["server_host"] = "bench.local"
    cfg["cloud"]["enrolled"] = "true"
    cfg["cloud"]["device_id"] = "sa02m-abc"
    cfg["cloud"]["heartbeat_interval"] = "10"
    cfg["device"]["serial"] = "SN1"
    agent.save_config(str(conf), cfg)
    (etc / "device_secret").write_text("s3cr3t\n")
    (etc / "frpc.toml").write_text("serverAddr = 'bench.local'\n")
    (etc / "unrelated.conf").write_text("keep = yes\n")
    return {"cfg": cfg, "conf": str(conf), "etc": etc, "run": run, "systemctl": systemctl}


def _status(fs):
    with io.open(os.path.join(str(fs["run"]), "status.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _conf(fs):
    return agent.load_config(fs["conf"])


# =====================================================================
# 1. Refusal classifier — per string, and what is NOT a refusal
# =====================================================================
@pytest.mark.parametrize("error,cls", [
    ("device revoked", "revoked"),
    ("unknown device", "unlinked"),
    ("invalid credential", "unknown"),
    ("device identity required", "unknown"),
    ("Device Revoked", "revoked"),  # case-insensitive
])
def test_classifier_heartbeat_403_strings(error, cls):
    assert agent.classify_refusal(403, error) == cls


@pytest.mark.parametrize("status,error", [
    (0, None),                      # network error / timeout / DNS
    (500, "internal error"),        # 5xx
    (502, None),
    (503, "device revoked"),        # the string alone is not enough
    (429, "too many requests"),
    (401, "device revoked"),
    (403, "something new"),         # unrecognised 403 → never a stand-down
    (403, None),
    (200, None),
])
def test_classifier_never_calls_transport_failures_a_refusal(status, error):
    assert agent.classify_refusal(status, error) == ""


@pytest.mark.parametrize("marker,cls", [
    ("device revoked", "revoked"),
    ("subdomain not enrolled", "unlinked"),
    ("device not enrolled", "unlinked"),
    ("no credential issued", "unlinked"),
    ("credential mismatch", "unknown"),
    ("device identity required", "unknown"),
    ("invalid credential", "unknown"),
])
def test_classifier_frps_markers(marker, cls):
    assert agent.classify_frps_marker(marker) == cls


def test_frps_marker_subdomain_not_enrolled_is_in_the_journal_marker_list():
    # The ONLY signal of a detach under FRP_IDENTITY_MODE=grace — mandatory.
    assert "subdomain not enrolled" in agent.FRPC_REJECT_MARKERS


def test_every_journal_marker_has_a_class_and_vice_versa():
    assert set(agent.FRPC_REJECT_MARKERS) == set(agent.FRPS_REFUSALS)


# =====================================================================
# 2. Threshold — three consecutive of ONE class; any success resets
# =====================================================================
def test_threshold_is_three():
    assert agent.REFUSAL_STANDDOWN_COUNT == 3


def test_tracker_fires_on_third_consecutive_same_class():
    t = agent.RefusalTracker()
    assert t.note_refusal("revoked") is False
    assert t.note_refusal("revoked") is False
    assert t.note_refusal("revoked") is True


def test_tracker_resets_on_success():
    t = agent.RefusalTracker()
    t.note_refusal("revoked")
    t.note_refusal("revoked")
    t.note_success()
    assert t.note_refusal("revoked") is False
    assert t.count == 1


def test_tracker_class_change_restarts_the_count():
    t = agent.RefusalTracker()
    t.note_refusal("revoked")
    t.note_refusal("revoked")
    assert t.note_refusal("unlinked") is False
    assert t.count == 1 and t.cls == "unlinked"


def test_tracker_ignores_an_empty_class():
    t = agent.RefusalTracker()
    t.note_refusal("revoked")
    assert t.note_refusal("") is False
    assert t.count == 1


# =====================================================================
# 3. Stand-down — what is wiped, what survives, what is written
# =====================================================================
def test_stand_down_wipes_binding_and_writes_marker_and_status(cloud_fs):
    fs = cloud_fs
    rc = agent.stand_down(fs["cfg"], fs["conf"], "revoked", "device revoked")
    assert rc == "repair"
    assert not os.path.exists(agent.DEVICE_SECRET_FILE)
    assert not os.path.exists(agent.FRPC_CONFIG)
    cfg = _conf(fs)
    assert cfg["cloud"]["enrolled"] == "false"
    assert cfg["cloud"]["device_id"] == ""
    assert cfg["cloud"]["unlinked_reason"] == "revoked"
    assert cfg["cloud"]["unlinked_at"].endswith("Z")
    st = _status(fs)
    assert st["state"] == "revoked"
    assert st["reason"] == "device revoked"
    assert st["reason_class"] == "revoked"
    assert st["unlinked_at"] == cfg["cloud"]["unlinked_at"]
    assert "tunnel" not in st and "last_heartbeat" not in st
    assert ("stop", agent.FRPC_UNIT) in fs["systemctl"].calls
    assert ("disable", agent.FRPC_UNIT) in fs["systemctl"].calls


def test_stand_down_keeps_configuration_not_identity(cloud_fs):
    fs = cloud_fs
    before_unrelated = (fs["etc"] / "unrelated.conf").read_bytes()
    agent.stand_down(fs["cfg"], fs["conf"], "unlinked", "subdomain not enrolled")
    cfg = _conf(fs)
    assert cfg["cloud"]["api_url"] == "https://bench.local/api/v1"
    assert cfg["cloud"]["server_host"] == "bench.local"
    assert cfg["cloud"]["heartbeat_interval"] == "10"
    assert cfg["device"]["serial"] == "SN1"
    assert (fs["etc"] / "unrelated.conf").read_bytes() == before_unrelated
    # Only the two identity files are in the clear-list, and nothing outside etc.
    assert sorted(os.path.basename(p) for p in agent.binding_files()) == ["device_secret", "frpc.toml"]
    assert sorted(os.listdir(str(fs["etc"]))) == ["agent.conf", "unrelated.conf"]


@pytest.mark.parametrize("cls,state", [("revoked", "revoked"), ("unlinked", "unlinked"), ("unknown", "unlinked")])
def test_stand_down_status_state_by_class(cloud_fs, cls, state):
    agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], cls, "x")
    assert _status(cloud_fs)["state"] == state


def test_stand_down_is_idempotent_and_reports_absent(cloud_fs):
    agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], "revoked", "device revoked")
    wiped = agent.wipe_cloud_binding()
    assert wiped == {"removed": [], "absent": ["device_secret", "frpc.toml"]}


def _deny_secret(monkeypatch, flag=None):
    """Make os.unlink refuse the device secret while `flag["on"]` (default:
    always) — a flag rather than monkeypatch.undo(), which would also undo
    the fixture's temp paths."""
    real_unlink = os.unlink
    flag = flag if flag is not None else {"on": True}

    def deny(path, *a, **kw):
        if flag["on"] and os.path.basename(path) == "device_secret":
            raise PermissionError(13, "denied", path)
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", deny)
    return flag


def test_failed_wipe_is_an_explicit_error_state_never_done(cloud_fs, monkeypatch):
    # Low 2: the binding is still on disk — the card must say «Ошибка отвязки»,
    # not «Подключено» (a restart loop) and not «отвязано».
    _deny_secret(monkeypatch)
    rc = agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], "revoked", "device revoked")
    assert rc == "unlink_failed"
    assert os.path.exists(agent.DEVICE_SECRET_FILE)
    assert _conf(cloud_fs)["cloud"]["enrolled"] == "true"  # nothing recorded as unlinked
    st = _status(cloud_fs)
    assert st["state"] == "unlink_failed"
    assert st["reason"] == "wipe_failed" and "denied" in st["detail"]
    assert st["reason_class"] == "revoked"
    assert st["refusal"] == "device revoked"
    assert agent.PENDING_STAND_DOWN == {"cls": "revoked", "reason": "device revoked"}


def test_retry_wipe_loop_finishes_the_stand_down_once_the_wipe_succeeds(cloud_fs, monkeypatch):
    flag = _deny_secret(monkeypatch)
    assert agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], "revoked", "device revoked") == "unlink_failed"
    sleeps = {"n": 0}

    def fake_sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] == 2:
            flag["on"] = False           # the permission problem goes away
        if sleeps["n"] > 5:
            raise _Budget("retry loop never finished")

    rc = agent.retry_wipe_loop(cloud_fs["cfg"], cloud_fs["conf"], sleep=fake_sleep)
    assert rc == "repair"
    assert sleeps["n"] == 2              # one failed retry (error state kept), then success
    assert not os.path.exists(agent.DEVICE_SECRET_FILE)
    assert not os.path.exists(agent.FRPC_CONFIG)
    assert agent.PENDING_STAND_DOWN == {"cls": "", "reason": ""}
    st = _status(cloud_fs)
    assert st["state"] == "revoked" and st["reason"] == "device revoked"
    assert _conf(cloud_fs)["cloud"]["enrolled"] == "false"


def test_stand_down_uses_the_injected_runner(cloud_fs):
    rec = _SystemctlRecorder()
    agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], "revoked", "r", systemctl=rec)
    assert rec.calls == [("stop", agent.FRPC_UNIT), ("disable", agent.FRPC_UNIT)]
    assert cloud_fs["systemctl"].calls == []


# =====================================================================
# 4. The active loop — heartbeat 403 ×3 → stand-down (RED on the old code)
# =====================================================================
def _run_active_loop(fs, monkeypatch, responses, *, ticks=40):
    """Drive active_loop with scripted heartbeat answers. `responses` is a
    list of (status, body) per heartbeat, repeating the last one. The clock
    advances 10 s per sleep so one heartbeat fires per iteration; a budget of
    `ticks` sleeps raises _Budget — the loop must have returned by then."""
    clock = {"t": 1000.0}
    answers = {"i": 0}

    def fake_time():
        return clock["t"]

    def fake_sleep(_s):
        clock["t"] += 10.0
        answers["sleeps"] = answers.get("sleeps", 0) + 1
        if answers["sleeps"] > ticks:
            raise _Budget("active_loop did not stand down within %d ticks" % ticks)

    def fake_api_post(url, payload, timeout=15):
        status, body = responses[min(answers["i"], len(responses) - 1)]
        answers["i"] += 1
        if hasattr(agent, "_note_heartbeat"):
            agent._note_heartbeat(url, status, agent._refusal_error(body) if status != 200 else None)
        return status, body

    monkeypatch.setattr(agent.time, "time", fake_time)
    monkeypatch.setattr(agent.time, "sleep", fake_sleep)
    monkeypatch.setattr(agent, "api_post", fake_api_post)
    monkeypatch.setattr(agent, "ensure_frpc_running", lambda: "running")
    monkeypatch.setattr(agent, "frpc_reject_reason", lambda unit=None: "")
    monkeypatch.setattr(agent, "collect_telemetry", lambda prev=None: ({"uptime_s": 1}, None))
    monkeypatch.setattr(agent, "load_device_secret", lambda path=None: "s3cr3t")
    sig = inspect.signature(agent.active_loop)
    if len(sig.parameters) >= 2:
        return agent.active_loop(fs["cfg"], fs["conf"])
    return agent.active_loop(fs["cfg"])  # pre-change signature


def test_three_heartbeat_refusals_stand_the_board_down(cloud_fs, monkeypatch):
    rc = _run_active_loop(cloud_fs, monkeypatch, [(403, {"error": "device revoked"})])
    assert rc == "repair"
    assert _status(cloud_fs)["state"] == "revoked"
    assert not os.path.exists(agent.DEVICE_SECRET_FILE)


def test_two_refusals_then_success_do_not_stand_down(cloud_fs, monkeypatch):
    seq = [(403, {"error": "device revoked"}), (403, {"error": "device revoked"}), (200, {"ok": True})] * 4
    with pytest.raises(_Budget):
        _run_active_loop(cloud_fs, monkeypatch, seq, ticks=14)
    assert os.path.exists(agent.DEVICE_SECRET_FILE)
    assert _status(cloud_fs)["state"] == "active"


@pytest.mark.parametrize("answer", [(0, None), (500, {"error": "boom"}), (503, None), (403, {"error": "new reason"})])
def test_transport_failures_never_stand_down(cloud_fs, monkeypatch, answer):
    with pytest.raises(_Budget):
        _run_active_loop(cloud_fs, monkeypatch, [answer], ticks=12)
    assert os.path.exists(agent.DEVICE_SECRET_FILE)
    assert os.path.exists(agent.FRPC_CONFIG)
    assert _conf(cloud_fs)["cloud"]["enrolled"] == "true"


class _FakeJournal:
    """A real journal's shape: `--since` returns EVERY line of the current
    unit run (the window the pre-cursor reader used — it never advances while
    the unit keeps running), `--after-cursor N` returns only lines after N,
    and `--show-cursor` appends `-- cursor: <index of the last line>`.
    `systemctl show` answers empty (the old reader's window fallback)."""

    def __init__(self, lines=None, footer=True, refuse_cursor=False):
        self.lines = list(lines or [])
        self.calls = []
        self.footer = footer              # False: journalctl prints no `-- cursor:` line
        self.refuse_cursor = refuse_cursor  # True: --after-cursor exits non-zero (stale)

    def __call__(self, args, **kw):
        class R:
            pass
        r = R()
        r.returncode = 0
        self.calls.append(list(args))
        if args[0] == "systemctl":
            r.stdout = ""
            return r
        if "--after-cursor" in args:
            if self.refuse_cursor:
                r.returncode = 1
                r.stdout, r.stderr = "", "Failed to seek to cursor"
                return r
            start = int(args[args.index("--after-cursor") + 1]) + 1
            lines = self.lines[start:]
        else:
            lines = list(self.lines)
        out = list(lines)
        if self.footer and "--show-cursor" in args and self.lines:
            out.append("-- cursor: %d" % (len(self.lines) - 1))
        r.stdout = "\n".join(out) + ("\n" if out else "")
        return r


def _drive_frps_ticks(fs, monkeypatch, journal, on_tick, ticks):
    """active_loop with a running tunnel, healthy heartbeats and the fake
    journal; `on_tick(n)` runs before each watchdog tick; `ticks` is the
    budget (raises _Budget when exceeded)."""
    clock = {"t": 1000.0}
    n = {"tick": 0}

    def fake_sleep(_s):
        clock["t"] += 61.0
        n["tick"] += 1
        if n["tick"] > ticks:
            raise _Budget("%d ticks without stand-down" % ticks)
        on_tick(n["tick"])

    monkeypatch.setattr(agent.time, "time", lambda: clock["t"])
    monkeypatch.setattr(agent.time, "sleep", fake_sleep)
    monkeypatch.setattr(agent.subprocess, "run", journal)
    monkeypatch.setattr(agent, "api_post", lambda url, payload, timeout=15: (200, {"ok": True}))
    monkeypatch.setattr(agent, "ensure_frpc_running", lambda: "running")
    monkeypatch.setattr(agent, "collect_telemetry", lambda prev=None: ({"uptime_s": 1}, None))
    monkeypatch.setattr(agent, "load_device_secret", lambda path=None: "")
    return agent.active_loop(fs["cfg"], fs["conf"])


MARK = "frpc: [dev-sa02m-abc] start error: subdomain not enrolled"


def test_frps_three_new_marker_lines_stand_down(cloud_fs, monkeypatch):
    """frps refuses the proxies but the login succeeded: the unit stays active
    and the heartbeat (grace, no 403) is fine — only the journal knows. Three
    NEW refusal lines on three ticks ⇒ stand-down."""
    journal = _FakeJournal([MARK])
    rc = _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: journal.lines.append(MARK), ticks=10)
    assert rc == "repair"
    st = _status(cloud_fs)
    assert st["state"] == "unlinked" and st["reason"] == "subdomain not enrolled"


def test_frps_one_marker_line_counts_once_across_ticks(cloud_fs, monkeypatch):
    # B1: ONE refusal line, three consecutive ticks ⇒ exactly one refusal
    # counted, no stand-down (the pre-cursor reader re-read it every tick).
    journal = _FakeJournal([MARK])
    with pytest.raises(_Budget):
        _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: None, ticks=5)
    assert os.path.exists(agent.DEVICE_SECRET_FILE)
    assert _conf(cloud_fs)["cloud"]["enrolled"] == "true"
    reads = [c for c in journal.calls if c[0] == "journalctl"]
    assert len(reads) >= 4
    assert "--since" in reads[0] and "--after-cursor" in reads[1]


def test_frps_marker_then_clean_tick_resets_the_counter(cloud_fs, monkeypatch):
    # B1: marker, clean tick, marker, marker ⇒ the clean tick reset the count,
    # so the two later markers reach 2, not 3 — no stand-down.
    journal = _FakeJournal([MARK])

    def on_tick(t):
        if t in (2, 3):          # NEW lines on ticks 3 and 4; tick 2 is clean
            journal.lines.append(MARK)

    with pytest.raises(_Budget):
        _drive_frps_ticks(cloud_fs, monkeypatch, journal, on_tick, ticks=5)
    assert os.path.exists(agent.DEVICE_SECRET_FILE)


def test_journal_reader_uses_a_persisted_cursor_and_no_line_limit(cloud_fs, monkeypatch):
    journal = _FakeJournal([MARK])
    monkeypatch.setattr(agent.subprocess, "run", journal)
    assert agent.frpc_reject_reason() == "subdomain not enrolled"
    first = journal.calls[-1]
    assert "--since" in first and "--show-cursor" in first and "-n" not in first
    assert first[first.index("--since") + 1] == agent.AGENT_STARTED_AT
    assert agent._read_cursor() == "0"
    assert agent.frpc_reject_reason() == ""        # nothing new since the cursor
    second = journal.calls[-1]
    assert "--after-cursor" in second and second[second.index("--after-cursor") + 1] == "0"
    journal.lines.append("frpc: login to server failed: device revoked")
    assert agent.frpc_reject_reason() == "device revoked"
    assert agent._read_cursor() == "1"


def test_journal_unreadable_is_not_a_refusal_and_keeps_the_cursor(cloud_fs, monkeypatch):
    agent._save_cursor("7")

    def boom(*a, **kw):
        raise OSError("no journalctl")

    monkeypatch.setattr(agent.subprocess, "run", boom)
    assert agent.frpc_reject_reason() == ""
    assert agent._read_cursor() == "7"


# ── B1 (round 9): inability to obtain or save the cursor is a CLEAN tick ──
def test_cursor_save_failure_is_a_clean_tick_never_a_recount(cloud_fs, monkeypatch):
    # /run unwritable: the save raises. RED on the round-8 agent, which
    # swallowed it and fell back to the fixed --since window every tick.
    real_replace = os.replace

    def deny_cursor(src, dst):
        if dst == agent.CURSOR_FILE:
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(agent.os, "replace", deny_cursor)
    journal = _FakeJournal([MARK])
    with pytest.raises(_Budget):
        _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: None, ticks=6)
    assert os.path.exists(agent.DEVICE_SECRET_FILE)
    assert _conf(cloud_fs)["cloud"]["enrolled"] == "true"
    reads = [c for c in journal.calls if c[0] == "journalctl"]
    # One --since, then --after-cursor from the in-memory cursor — never the
    # fixed window again. While the save keeps failing EVERY tick is clean by
    # decision (the detach signal is not counted until /run is writable
    # again — the safe direction, warned once in the journal).
    assert "--since" in reads[0]
    assert all("--after-cursor" in c for c in reads[1:])


def test_journal_without_cursor_footer_is_a_clean_tick(cloud_fs, monkeypatch):
    # journalctl output with body lines but no `-- cursor:` — RED on the
    # round-8 agent (no save ⇒ --since every tick ⇒ recount).
    journal = _FakeJournal([MARK], footer=False)
    with pytest.raises(_Budget):
        _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: journal.lines.append(MARK), ticks=6)
    assert os.path.exists(agent.DEVICE_SECRET_FILE)


def test_footerless_read_is_clean_and_the_window_is_rearmed(cloud_fs, monkeypatch):
    # Round 10: a read that yields no cursor re-arms the window — the next
    # tick reads AGAIN (never "once per process life"); still clean each time.
    journal = _FakeJournal([MARK], footer=False)
    monkeypatch.setattr(agent.subprocess, "run", journal)
    assert agent.frpc_reject_reason() == ""            # no cursor came back → clean
    first = journal.calls[-1]
    assert "--since" in first
    assert agent.frpc_reject_reason() == ""            # read again, same window, still clean
    second = journal.calls[-1]
    assert "--since" in second and second[second.index("--since") + 1] == first[first.index("--since") + 1]
    assert len([c for c in journal.calls if c[0] == "journalctl"]) == 2


def test_stale_cursor_is_dropped_and_the_next_read_starts_from_now(cloud_fs, monkeypatch):
    agent._save_cursor("s=old")
    journal = _FakeJournal([MARK], refuse_cursor=True)
    monkeypatch.setattr(agent.subprocess, "run", journal)
    assert agent.frpc_reject_reason() == ""            # refused cursor → clean, dropped
    assert agent._read_cursor() == ""
    assert agent._SINCE_FROM["at"]                     # re-armed from now
    journal.refuse_cursor = False
    journal.lines = []                                 # nothing since "now"
    assert agent.frpc_reject_reason() == ""
    assert "--since" in journal.calls[-1]


# ── B1 (round 10): every no-cursor exit re-arms the window — journalctl on every tick ──
class _FlakyFirstJournal(_FakeJournal):
    """The first read fails (rc≠0) or answers an EMPTY window; afterwards a
    real journal with a footer. Models an agent restart on a quiet, connected
    frpc, followed by a genuine detach."""

    def __init__(self, lines=None, first="rc1"):
        super().__init__(lines)
        self.first = first
        self.reads = 0

    def __call__(self, args, **kw):
        if args[0] == "journalctl":
            self.reads += 1
            if self.reads == 1:
                class R:
                    pass
                r = R()
                if self.first == "rc1":
                    r.returncode, r.stdout, r.stderr = 1, "", "journal not ready"
                else:  # empty --since window: no lines, no footer
                    r.returncode, r.stdout, r.stderr = 0, "", ""
                self.calls.append(list(args))
                return r
        return super().__call__(args, **kw)


def test_first_read_failure_then_repeating_refusal_stands_down(cloud_fs, monkeypatch):
    # RED on the round-9 agent: the rc≠0 first read spent the one-shot window
    # and the reader never called journalctl again.
    journal = _FlakyFirstJournal([], first="rc1")
    rc = _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: journal.lines.append(MARK), ticks=12)
    assert rc == "repair"
    assert _status(cloud_fs)["state"] == "unlinked"


def test_empty_since_window_then_repeating_refusal_stands_down(cloud_fs, monkeypatch):
    # The ordinary case: agent restart, frpc connected and quiet → empty
    # window, no footer. A detach afterwards must still be seen.
    journal = _FlakyFirstJournal([], first="empty")
    rc = _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: journal.lines.append(MARK), ticks=12)
    assert rc == "repair"


def test_journalctl_is_invoked_on_every_tick(cloud_fs, monkeypatch):
    journal = _FakeJournal([])  # empty forever: every read is an empty window
    with pytest.raises(_Budget):
        _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: None, ticks=6)
    reads = [c for c in journal.calls if c[0] == "journalctl"]
    assert len(reads) >= 6, "journalctl must run on every tick, never once per process life"
    assert os.path.exists(agent.DEVICE_SECRET_FILE)


def test_rearm_never_recounts_a_footerless_line(cloud_fs, monkeypatch):
    # The recount guard the re-arm relies on: the same footerless line seen
    # on every tick is never counted (each tick is clean), so no stand-down.
    journal = _FakeJournal([MARK], footer=False)
    with pytest.raises(_Budget):
        _drive_frps_ticks(cloud_fs, monkeypatch, journal, lambda t: None, ticks=8)
    assert len([c for c in journal.calls if c[0] == "journalctl"]) >= 8
    assert os.path.exists(agent.DEVICE_SECRET_FILE)


# ── B2 (round 9): the stand-down marker is identity, cleared everywhere ───
def test_finalize_enrollment_clears_the_stand_down_marker(cloud_fs, monkeypatch):
    cfg = cloud_fs["cfg"]
    cfg["cloud"]["enrolled"] = "false"
    cfg["cloud"]["unlinked_at"] = "2026-09-03T00:10:00Z"
    cfg["cloud"]["unlinked_reason"] = "revoked"
    cfg["cloud"]["unlinked_reason_text"] = "device revoked"
    monkeypatch.setattr(agent, "write_frpc_config", lambda frpc, path=None: None)
    monkeypatch.setattr(agent, "save_device_secret", lambda secret, path=None: None)
    monkeypatch.setattr(agent, "ensure_frpc_running", lambda: "running")
    resp = {"frpc": {"server_addr": "bench.local", "token": "t", "device_secret": "n3w"}}
    assert agent.finalize_enrollment(resp, cfg, cloud_fs["conf"], "sa02m-new") is True
    on_disk = _conf(cloud_fs)
    for key in agent.STAND_DOWN_MARKER_KEYS:
        assert not on_disk.has_option("cloud", key), key
    assert on_disk["cloud"]["enrolled"] == "true"
    assert agent.restore_stand_down_status(on_disk) is False


def test_reset_script_drops_the_stand_down_marker(tmp_path):
    """tools/imaging/reset-cloud-enrollment.sh, merge branch: extract its
    embedded python (the shipped block, path retargeted) and run it."""
    script_path = os.path.abspath(os.path.join(AGENT_DIR, "..", "..", "tools", "imaging", "reset-cloud-enrollment.sh"))
    with open(script_path, encoding="utf-8") as f:
        text = f.read()
    for key in agent.STAND_DOWN_MARKER_KEYS:
        assert key in text, "%s is not on the reset script's drop list" % key
    assert "/run/sa02m-cloud-frpc.cursor" in text, "the journal cursor is on the contract clear-list"
    block = text.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    conf = tmp_path / "agent.conf"
    conf.write_text("[cloud]\napi_url = https://bench.local/api/v1\nserver_host = bench.local\n"
                    "enrolled = false\ndevice_id =\nunlinked_at = 2026-09-03T00:10:00Z\n"
                    "unlinked_reason = revoked\nunlinked_reason_text = device revoked\n"
                    "[device]\nserial = SN1\n")
    block = block.replace('"/etc/sa02m-cloud/agent.conf"', repr(str(conf)))
    assert repr(str(conf)) in block
    exec(compile(block, "reset-cloud-enrollment.sh<python>", "exec"), {"__name__": "reset"})
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(str(conf))
    for key in agent.STAND_DOWN_MARKER_KEYS:
        assert not cfg.has_option("cloud", key), key
    assert cfg["cloud"]["api_url"] == "https://bench.local/api/v1"
    assert cfg["cloud"]["enrolled"] == "false"


# ── M4 note: an unparseable 200 is neither a success nor a refusal ────────
def test_unparseable_200_is_neither_success_nor_refusal(monkeypatch):
    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda req, timeout: _Resp(b"<html>not json"))
    status, body = agent.api_post("https://c/api/v1/heartbeat", {"device_id": "d"})
    assert (status, body) == (0, None)
    assert agent.heartbeat_refusal() == {"status": 0, "error": None}
    assert agent.classify_refusal(0, None) == ""


# ── low 1: the durable marker is read back on start ──────────────────────
def test_restore_stand_down_status_rebuilds_the_status_file(cloud_fs):
    cfg = cloud_fs["cfg"]
    cfg["cloud"]["enrolled"] = "false"
    cfg["cloud"]["unlinked_at"] = "2026-09-03T00:10:00Z"
    cfg["cloud"]["unlinked_reason"] = "revoked"
    cfg["cloud"]["unlinked_reason_text"] = "device revoked"
    assert agent.restore_stand_down_status(cfg) is True
    st = _status(cloud_fs)
    assert st["state"] == "revoked" and st["reason"] == "device revoked"
    assert st["unlinked_at"] == "2026-09-03T00:10:00Z" and st["restored"] is True


def test_restore_is_a_no_op_without_a_marker_or_while_enrolled(cloud_fs):
    cfg = cloud_fs["cfg"]
    assert agent.restore_stand_down_status(cfg) is False          # enrolled
    cfg["cloud"]["enrolled"] = "false"
    assert agent.restore_stand_down_status(cfg) is False          # never stood down
    assert not os.path.exists(agent.STATUS_FILE)


def test_stand_down_persists_the_reason_text_for_the_restore(cloud_fs):
    agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], "unlinked", "subdomain not enrolled")
    cfg = _conf(cloud_fs)
    assert cfg["cloud"]["unlinked_reason_text"] == "subdomain not enrolled"
    os.unlink(agent.STATUS_FILE)  # "reboot": /run is gone
    assert agent.restore_stand_down_status(cfg) is True
    assert _status(cloud_fs)["state"] == "unlinked"


# =====================================================================
# 5. Re-pairing is reachable from stand-down
# =====================================================================
def test_standby_after_stand_down_keeps_the_status_and_takes_the_pair_trigger(cloud_fs, monkeypatch):
    agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], "revoked", "device revoked")
    seen = {"claim": 0}

    def fake_claim(cfg, path):
        seen["claim"] += 1
        return True  # the cloud handed a new identity

    polls = {"n": 0}

    def fake_sleep(_s):
        polls["n"] += 1
        if polls["n"] == 2:
            # The operator presses «Привязать заново» (cloud.cgi → trigger).
            open(agent.PAIR_REQUEST_FILE, "w").close()
        if polls["n"] > 20:
            raise _Budget("bootstrap_loop never saw the pairing trigger")

    monkeypatch.setattr(agent, "run_claim_flow", fake_claim)
    monkeypatch.setattr(agent.time, "sleep", fake_sleep)
    cfg = _conf(cloud_fs)
    assert not cfg["cloud"].getboolean("enrolled")
    # The card must keep saying revoked while waiting — never a bare standby.
    assert agent.bootstrap_loop(cfg, cloud_fs["conf"]) is True
    assert seen["claim"] == 1
    assert _status(cloud_fs)["state"] == "revoked"


def test_standby_without_a_stand_down_still_writes_standby(cloud_fs, monkeypatch):
    open(agent.PAIR_REQUEST_FILE, "w").close()
    monkeypatch.setattr(agent, "run_claim_flow", lambda cfg, path: True)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    assert agent.bootstrap_loop(cloud_fs["cfg"], cloud_fs["conf"]) is True
    assert _status(cloud_fs)["state"] == "standby"


# =====================================================================
# 5b. No live-only key survives into a non-active state (bench 1.135, 2026-09-03)
# =====================================================================
LIVE_KEYS = ("tunnel", "last_heartbeat", "identity")


def test_status_writer_drops_live_only_keys_outside_active(cloud_fs):
    # RED on the pre-fix writer: it passed any kwarg through.
    agent._write_status("already_claimed", device_id="d", serial="SN1",
                        tunnel="running", last_heartbeat=123, identity="present")
    st = _status(cloud_fs)
    assert st["state"] == "already_claimed"
    for key in LIVE_KEYS:
        assert key not in st, key


def test_status_writer_keeps_live_keys_in_active(cloud_fs):
    agent._write_status("active", device_id="d", serial="SN1", tunnel="running", last_heartbeat=123)
    st = _status(cloud_fs)
    assert st["tunnel"] == "running" and st["last_heartbeat"] == 123


def test_already_claimed_status_carries_a_reason_and_a_time(cloud_fs, monkeypatch):
    monkeypatch.setattr(agent, "api_post", lambda url, payload, timeout=15: (409, {"error": "device already claimed"}))
    assert agent.run_claim_flow(cloud_fs["cfg"], cloud_fs["conf"]) is False
    st = _status(cloud_fs)
    assert st["state"] == "already_claimed"
    assert st["reason"] == "already claimed"
    assert st["reason_class"] == "already_claimed"
    assert isinstance(st["since"], int) and st["since"] > 0
    for key in LIVE_KEYS:
        assert key not in st


@pytest.mark.parametrize("scenario", ["already_claimed", "claim_failed", "pair_expired", "standby_cancel", "standby_loop", "stand_down"])
def test_every_non_active_writer_path_leaves_no_live_keys(cloud_fs, monkeypatch, scenario):
    """Drive the REAL writer paths after an `active` status is on disk, so a
    key carried forward would be visible."""
    agent._write_status("active", device_id="d", serial="SN1", tunnel="running", last_heartbeat=5, identity="present")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    if scenario == "already_claimed":
        monkeypatch.setattr(agent, "api_post", lambda *a, **k: (409, {"error": "x"}))
        agent.run_claim_flow(cloud_fs["cfg"], cloud_fs["conf"])
    elif scenario == "claim_failed":
        monkeypatch.setattr(agent, "api_post", lambda *a, **k: (0, None))
        agent.run_claim_flow(cloud_fs["cfg"], cloud_fs["conf"])
    elif scenario == "pair_expired":
        clock = {"t": 1000.0}
        monkeypatch.setattr(agent.time, "time", lambda: clock["t"])
        open(agent.PAIR_REQUEST_FILE, "w").close()

        def api(url, payload, timeout=15):
            if url.endswith("/claim"):
                return 200, {"claim_code": "AB12", "expires_in_s": 5, "poll_interval_s": 2}
            clock["t"] += 10.0  # the poll runs past the deadline
            return 200, {"state": "pending"}

        monkeypatch.setattr(agent, "api_post", api)
        agent.run_claim_flow(cloud_fs["cfg"], cloud_fs["conf"])
    elif scenario == "standby_cancel":
        open(agent.PAIR_REQUEST_FILE, "w").close()

        def api(url, payload, timeout=15):
            if url.endswith("/claim"):
                return 200, {"claim_code": "AB12", "expires_in_s": 900, "poll_interval_s": 2}
            os.unlink(agent.PAIR_REQUEST_FILE)  # cancelled from the web UI
            return 200, {"state": "pending"}

        monkeypatch.setattr(agent, "api_post", api)
        agent.run_claim_flow(cloud_fs["cfg"], cloud_fs["conf"])
    elif scenario == "standby_loop":
        polls = {"n": 0}

        def fake_sleep(_s):
            polls["n"] += 1
            if polls["n"] > 2:
                raise _Budget("standby")

        monkeypatch.setattr(agent.time, "sleep", fake_sleep)
        with pytest.raises(_Budget):
            agent.bootstrap_loop(cloud_fs["cfg"], cloud_fs["conf"])
    else:
        agent.stand_down(cloud_fs["cfg"], cloud_fs["conf"], "revoked", "device revoked")
    st = _status(cloud_fs)
    assert st["state"] != "active"
    for key in LIVE_KEYS:
        assert key not in st, (scenario, key)


# =====================================================================
# 6. F1 — the pins, kept literally, plus the new one
# =====================================================================
def test_heartbeat_call_site_still_captures_nothing():
    import re
    m = re.search(r"(\w+\s*=\s*)?api_post\(f?\"?\{?api_url\}?/heartbeat", AGENT_SOURCE)
    assert m and m.group(1) is None


def test_no_command_channel_symbols_still_absent():
    assert not hasattr(agent, "handle_command")
    assert "commands/pending" not in AGENT_SOURCE


def test_success_body_is_never_read_by_the_refusal_channel(monkeypatch):
    """A 200 with a body the cloud must never send: the channel records the
    status only. A future field on the 200 body cannot reach the agent."""
    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(agent.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b'{"ok": true, "action": "noop", "error": "device revoked"}'))
    agent.api_post("https://c/api/v1/heartbeat", {"device_id": "d"})
    assert agent.heartbeat_refusal() == {"status": 200, "error": None}
    assert agent.classify_refusal(200, "device revoked") == ""


def test_refusal_channel_reads_only_status_and_error_of_a_non_200(monkeypatch):
    def raise_403(req, timeout):
        raise urllib.error.HTTPError("https://c", 403, "forbidden", {},
                                     io.BytesIO(b'{"error": "device revoked", "action": "noop"}'))

    monkeypatch.setattr(agent.urllib.request, "urlopen", raise_403)
    agent.api_post("https://c/api/v1/heartbeat", {"device_id": "d"})
    assert agent.heartbeat_refusal() == {"status": 403, "error": "device revoked"}
    # Only heartbeat verdicts land on the channel.
    agent._note_heartbeat("https://c/api/v1/claim", 409, "already claimed")
    assert agent.heartbeat_refusal()["status"] == 403


def test_classifier_signature_is_status_and_error_only():
    assert list(inspect.signature(agent.classify_refusal).parameters) == ["status", "error"]
