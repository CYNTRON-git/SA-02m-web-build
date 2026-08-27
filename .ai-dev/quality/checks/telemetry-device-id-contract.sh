#!/usr/bin/env bash
# telemetry-device-id-contract — the "no consumer was missed" gate for 1.0.6.21.
#
# The defect this closes was invisible to every other row. The telemetry service
# derived its MQTT device id by gluing the hostname behind a fixed prefix, while
# every other consumer named the board directly — so the service published and
# subscribed on a subtree nobody else used. Nothing failed: no syntax error, no
# dead unit. The board's own beeper/DO/alarm-LED commands were simply never
# received, and a stale retained value made the app show the opposite of the
# hardware. The failure is TEXTUAL (a consumer still naming the old id) and
# STRUCTURAL (two packages deriving the id independently), so the gate has both
# halves:
#
#   A  no legacy id literal survives anywhere in the tracked tree, outside the
#      files whose job is to record history verbatim.
#   B  the two independent derivations — opt/sa02m-modbus-mqtt/sa02m_telemetry.py
#      and opt/sa02m-alice/sa02m_alice/config/topics.py, which cannot import each
#      other at runtime — are RUN over a matrix and must agree. Without this,
#      the conscious duplication in topics.py is a silent-drift seam of exactly
#      the kind that produced the ghost binding in the first place.
#   C  the wiring the other two cannot see: the startup journal line naming the
#      live id (the probe the next person needs when a binding looks dead), the
#      clear actually being called from run(), and its two ownership gates —
#      loopback broker AND our own retained meta/driver marker. Every board
#      carries the same hostname, so the legacy id is the SAME STRING on every
#      board: on a shared external broker an unguarded clear would wipe a
#      neighbour's live telemetry subtree.
#
# Non-vacuous, both directions: a dead sweep, a failed import, a matrix that
# collapses to one id, or a pattern that stops matching its own known-bad
# fixture FAILS rather than passing on zero matches.
#
# RED/GREEN proof recipe (re-runnable): restore one legacy literal in any
# tracked file (A fails), skew one derivation's fallback constant (B fails),
# delete the loopback guard or the startup log line (C fails).
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || { echo "telemetry-device-id-contract: cannot cd to repo root"; exit 1; }

TEL_PY="opt/sa02m-modbus-mqtt/sa02m_telemetry.py"
ALICE_PY="opt/sa02m-alice/sa02m_alice/config/topics.py"
FAIL=0
fail() { echo "FAIL: $*"; FAIL=1; }
ok()   { echo "  ok: $*"; }

for f in "$TEL_PY" "$ALICE_PY"; do
    [ -f "$f" ] || { echo "FAIL: missing $f"; exit 1; }
done

PY=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "telemetry-device-id-contract: no working python interpreter"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── A. No legacy id literal survives ─────────────────────────────────────────
# Three narrow patterns, each naming the form it catches. They are narrow ON
# PURPOSE: `sa02m-` is a legitimate prefix elsewhere in this tree, and a gate
# that cried wolf on those would be turned off.
#   1  the frozen literal ids (covers both vendor hostnames)
#   2  the controller TOPIC form, whatever placeholder shape it is written in —
#      /devices/sa02m-{hostname}, /devices/sa02m-<hostname>, /devices/sa02m-SA-02
#   3  the prose/config form that states the id IS the glued hostname
# Deliberate non-flags, all verified present in this tree:
#   * /devices/sa02m-bridge — the bridge STATUS device, a real and unchanged id;
#   * sa02m-<serial> / "sa02m-" + get_serial() (opt/sa02m-cloud-agent,
#     docs/contracts/cloud-enrollment.md) — the CLOUD ENROLLMENT id, a different
#     namespace keyed on the cpuinfo serial, deliberately not hostname-derived;
#   * sa02m-${STAMP} / sa02m-${PROFILE} (tools/) — image artifact filenames.
# The producer's own regression (re-gluing a prefix under any variable name,
# e.g. f"sa02m-{h}") is NOT chased by a regex here — pin B catches it
# behaviourally, by asserting the derived id never carries the legacy prefix.
PAT1='sa02m-SA-02'
PAT2='/devices/sa02m-'
PAT3='sa02m-[<{(]?[$]?[{(]?[A-Za-z_]{0,12}[Hh][Oo][Ss][Tt][Nn][Aa][Mm][Ee]'
BRIDGE_ID='/devices/sa02m-bridge'

# Sweep a LIST of files in three grep invocations rather than three per file —
# a per-file loop over ~700 tracked files costs ~40 s on a Windows dev box, and
# a build-beat row that slow gets skipped by hand. `-H` forces the filename
# prefix even for a one-file list. The bridge status device — a real, unchanged
# id — is filtered out of the topic-form hits.
sweep_list() {
    {
        xargs -d '\n' -a "$1" grep -HnI  -e "$PAT1" 2>/dev/null
        xargs -d '\n' -a "$1" grep -HnIF -e "$PAT2" 2>/dev/null | grep -vF "$BRIDGE_ID"
        xargs -d '\n' -a "$1" grep -HnIE -e "$PAT3" 2>/dev/null
    } | sort -u
}

sweep_file() {
    printf '%s\n' "$1" > "$TMP/one.lst"
    sweep_list "$TMP/one.lst"
}

# Blank (keeping line numbers) the migration subsection of the topic canon,
# anchored on the version in its heading — the section's whole job is to name
# the old id. A renamed heading makes the mask miss and A1 fails loudly with the
# section's own lines; a section that stops naming the old id fails A2.
mask_migration_section() {
    awk '
      /^### .*1\.0\.6\.21/            { inmig = 1; print ""; next }
      inmig && (/^---[[:space:]]*$/ || /^## /) { inmig = 0 }
      inmig                           { print ""; next }
                                      { print }
    ' "$1"
}

# Allow-list: files whose job is to record history verbatim. The check script
# itself lives under .ai-dev/, which is excluded wholesale.
is_allowed() {
    case "$1" in
        CHANGELOG.md|docs/bugs/BUGLOG.md|.ai-dev/*) return 0 ;;
        *) return 1 ;;
    esac
}

echo "A. legacy id literal sweep"

# A0 — pattern self-test, both directions: each pattern must still fire on its
# own known-bad fixture, and must NOT fire on the legitimate look-alikes. A
# regex quietly broken by a later edit would otherwise turn the sweep into a
# silent pass; one quietly widened would get the gate disabled.
BAD="$TMP/known-bad.txt"
GOOD="$TMP/known-good.txt"
printf '%s\n' '/devices/sa02m-SA-02/controls/beeper' > "$BAD"
printf '%s\n' 'f"sa02m-{hostname}"' 'sa02m-<hostname> — id телеметрии' >> "$BAD"
printf '%s\n' '/devices/sa02m-bridge/controls/devices_online' \
              'sa02m-<serial> cloud enrollment' \
              'RAW_IMG="$WORK/sa02m-${STAMP}-raw.img"' \
              '/devices/SA-02m/controls/beeper' > "$GOOD"
grep -q "$PAT1" "$BAD" || fail "A0: pattern 1 no longer matches its known-bad fixture"
grep -qF "$PAT2" "$BAD" || fail "A0: pattern 2 no longer matches its known-bad fixture"
grep -Eq "$PAT3" "$BAD" || fail "A0: pattern 3 no longer matches its known-bad fixture"
if sweep_file "$GOOD" | grep -q .; then
    fail "A0: a pattern widened onto a legitimate look-alike:"
    sweep_file "$GOOD" | sed 's/^/      /'
fi
[ "$FAIL" -eq 0 ] && ok "patterns detect the known-bad forms and spare the look-alikes"

# A1 — the sweep itself, over TRACKED files only (an untracked __pycache__ or a
# scratch dir is not a shipped consumer). docs/MQTT_TOPICS.md carries the
# migration subsection, whose JOB is to name the old id (the old→new mapping and
# the manual clear command) — that one section is masked; the rest of the topic
# canon stays under the gate. A2 keeps that exclusion honest.
LIST="$TMP/files.lst"
: > "$LIST"
while IFS= read -r f; do
    is_allowed "$f" && continue
    [ -f "$f" ] || continue
    [ "$f" = "docs/MQTT_TOPICS.md" ] && continue   # swept masked, below
    printf '%s\n' "$f" >> "$LIST"
done < <(git ls-files 2>/dev/null)

HITS="$TMP/hits.txt"
sweep_list "$LIST" > "$HITS"
SCANNED=$(wc -l < "$LIST")
# The topic canon is swept as its masked copy, re-labelled with its real path.
mask_migration_section docs/MQTT_TOPICS.md > "$TMP/masked.md"
sweep_file "$TMP/masked.md" | sed "s|^$TMP/masked.md:|docs/MQTT_TOPICS.md:|" >> "$HITS"
SCANNED=$((SCANNED + 1))

if [ "$SCANNED" -lt 500 ]; then
    fail "A1: only $SCANNED files swept — the sweep is not seeing the tree (expected >=500)"
else
    ok "swept $SCANNED tracked files"
fi

if [ -s "$HITS" ]; then
    fail "A1: a legacy telemetry device id survives — every consumer must name the board directly:"
    sed 's/^/      /' "$HITS"
else
    ok "no legacy id literal outside the history files"
fi

# A2 — every exclusion must stay honest, i.e. must still be excusing something
# real. A stale exclusion is an unnoticed blind spot: it would keep passing long
# after the thing it covers is gone, and cover a genuine regression the day one
# lands in that file.
if sweep_file CHANGELOG.md | grep -q .; then
    ok "CHANGELOG.md still carries the historical id (exclusion live, not stale)"
else
    fail "A2: CHANGELOG.md no longer matches — the history exclusion is stale or the sweep is blind"
fi

mask_migration_section docs/MQTT_TOPICS.md > "$TMP/masked.md"
UNMASKED=$(sweep_file docs/MQTT_TOPICS.md | wc -l)
MASKED=$(sweep_file "$TMP/masked.md" | wc -l)
if [ "$UNMASKED" -le "$MASKED" ]; then
    fail "A2: the migration-section mask excuses nothing (unmasked=$UNMASKED masked=$MASKED) — the section was renamed, moved or no longer documents the old id"
else
    ok "the migration section still documents the old id ($((UNMASKED - MASKED)) line(s) masked)"
fi

# ── B. The two derivations agree ─────────────────────────────────────────────
echo "B. telemetry and alice-picker id derivations agree"
GATE_CONF="$TMP/sa02m_telemetry.conf" "$PY" - "$TMP" <<'PYEOF'
import os
import sys
import types

tmp = sys.argv[1]
conf = os.path.join(tmp, "sa02m_telemetry.conf")
open(conf, "w").close()
# Both modules bind their conf path at import time — set it BEFORE importing.
os.environ["SA02M_TELEMETRY_CONF"] = conf
os.environ.pop("SA02M_TELEMETRY_DEVICE_ID", None)

# sa02m_telemetry.py calls sys.exit() at import when paho is absent.
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _mqtt = types.ModuleType("paho.mqtt")
    _client = types.ModuleType("paho.mqtt.client")
    _client.Client = object
    _client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _mqtt
    _mqtt.client = _client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _mqtt
    sys.modules["paho.mqtt.client"] = _client

sys.path.insert(0, os.path.abspath("opt/sa02m-modbus-mqtt"))
sys.path.insert(0, os.path.abspath("opt/sa02m-alice"))
import socket

try:
    import sa02m_telemetry as tel
    from sa02m_alice.config import topics
except Exception as e:                                  # a failed import FAILS
    print("FAIL: B: cannot import a derivation: %r" % (e,))
    raise SystemExit(1)

for name, mod in (("sa02m_telemetry.get_device_id", getattr(tel, "get_device_id", None)),
                  ("topics._controller_device_id", getattr(topics, "_controller_device_id", None))):
    if not callable(mod):
        print("FAIL: B: %s is missing" % name)
        raise SystemExit(1)

# (hostname, env override, conf body)
MATRIX = [
    ("SA-02m", None, ""),
    ("boiler-room", None, ""),
    ("SA-02m", "pinned-id", ""),
    ("SA-02m", None, "SA02M_TELEMETRY_DEVICE_ID=from-conf\n"),
    ("SA-02m", None, '# integrator pin\nSA02M_TELEMETRY_DEVICE_ID = "quoted-id"  \n'),
    ("SA-02m", "bad/id", ""),          # invalid override -> both fall through
    ("SA-02m", "a+b", ""),             # would widen a subscribe into a wildcard
    ("bad/host", None, ""),            # invalid hostname -> both fall back
    ("", None, ""),                    # no hostname at all -> both fall back
]

rc = 0
seen = set()
real_gethostname = socket.gethostname
for hostname, env, conf_body in MATRIX:
    with open(conf, "w", encoding="utf-8") as fh:
        fh.write(conf_body)
    if env is None:
        os.environ.pop("SA02M_TELEMETRY_DEVICE_ID", None)
    else:
        os.environ["SA02M_TELEMETRY_DEVICE_ID"] = env
    socket.gethostname = lambda h=hostname: h
    try:
        a = tel.get_device_id()
        b = topics._controller_device_id()
    finally:
        socket.gethostname = real_gethostname
    if a != b:
        print("FAIL: B: derivations disagree for hostname=%r env=%r conf=%r: "
              "telemetry=%r alice=%r" % (hostname, env, conf_body, a, b))
        rc = 1
        continue
    if not a or not tel.DEVICE_ID_RE.match(a):
        print("FAIL: B: derived id %r is empty or outside the allow-list" % (a,))
        rc = 1
        continue
    if a.startswith(tel.LEGACY_ID_PREFIX):
        print("FAIL: B: derived id %r still carries the legacy prefix" % (a,))
        rc = 1
        continue
    seen.add(a)

# The picker's real output, not just its helper: every builtin must carry the
# live id and the never-empty guarantee must survive.
socket.gethostname = lambda: "SA-02m"
os.environ.pop("SA02M_TELEMETRY_DEVICE_ID", None)
open(conf, "w").close()
try:
    offered = topics.list_mqtt_topics()["topics"]
finally:
    socket.gethostname = real_gethostname
expected = ["/devices/SA-02m/controls/%s" % c for c in topics.CONTROLLER_CONTROLS]
missing = [t for t in expected if t not in offered]
if missing:
    print("FAIL: B: the picker does not offer the derived controller topics: %s" % missing)
    rc = 1
if not offered:
    print("FAIL: B: the picker is empty — the never-empty guarantee is gone")
    rc = 1
ghosts = [t for t in offered if t.startswith("/devices/" + tel.LEGACY_ID_PREFIX)]
if ghosts:
    print("FAIL: B: the picker still offers legacy-prefixed controller topics: %s" % ghosts)
    rc = 1

# Non-vacuity: the matrix must really have exercised distinct resolutions —
# two derivations both stubbed to a constant would otherwise "agree" forever.
if len(MATRIX) < 8 or len(seen) < 3:
    print("FAIL: B: matrix collapsed (%d cases, %d distinct ids) — not a real "
          "agreement test" % (len(MATRIX), len(seen)))
    rc = 1
if rc == 0:
    print("  ok: %d cases, %d distinct ids, picker offers the derived topics"
          % (len(MATRIX), len(seen)))
raise SystemExit(rc)
PYEOF
[ $? -eq 0 ] || FAIL=1

# ── C. The wiring the sweep and the matrix cannot see ────────────────────────
echo "C. startup probe + clear call site + ownership gates"

# Every pin below reads the body of the function that must carry the guard,
# never the whole file: a constant still defined at module level, a function
# NAME matching its own pattern, or a WARN message quoting the marker it no
# longer compares — each of those passed a whole-file grep with the guard
# gutted, and each was caught only by mutation.
py_func_body() {     # $1 = module-level function name (literal match, no regex)
    awk -v fn="$1" '
        index($0, "def " fn "(") == 1 { f = 1; print; next }
        f && /^def /                  { f = 0 }
        f                             { print }
    ' "$TEL_PY"
}

py_method_body() {   # $1 = class method name (one indent level)
    awk -v fn="$1" '
        index($0, "    def " fn "(") == 1 { f = 1; print; next }
        f && /^    def /                  { f = 0 }
        f && /^[^ ]/                      { f = 0 }
        f                                 { print }
    ' "$TEL_PY"
}

RUN_BODY="$TMP/run_body.txt"
py_method_body run > "$RUN_BODY"
if [ ! -s "$RUN_BODY" ]; then
    fail "C: could not extract run() from $TEL_PY — the gate cannot see its wiring"
else
    grep -q 'telemetry device id' "$RUN_BODY" \
        || fail "C: run() no longer logs the live device id — the one always-on probe for a dead binding"
    grep -q '_clear_legacy_retained' "$RUN_BODY" \
        || fail "C: run() no longer calls the legacy retained clear"
    # ORDER, not just presence (review finding B1): _on_connect subscribes to
    # controls/*/on as soon as the broker answers, so any work between connect()
    # and a ready self._hw is a window in which a beeper/DO command is accepted
    # and dropped. The clear sleeps 3 s per legacy id — ordered before init_hw()
    # it stretched that window from <1 s to 6-14 s.
    #
    # CODE ONLY: the comment block above those very two lines discusses this
    # ordering, so a `#` in front of the real init_hw() would leave a matching
    # line behind and — with a naive first-match — keep this pin green while the
    # window reopened in full (review finding A9). Comments are stripped first:
    # a whole-line `#`, and anything from a whitespace-preceded ` #` to end of line (quote-blind: a `#` inside a string truncates too — fail-closed, it can only remove a call, never invent one).
    # Scope is already run()'s body, so a call in another method cannot satisfy
    # it. The last init_hw must precede the first clear: an init_hw ADDED after
    # the clear fails, and both on one line (`a(); b()`) fails too — fail-closed
    # in both directions, which for a one-line ordering guard is the right way
    # to be wrong.
    RUN_CODE="$TMP/run_code.txt"
    sed -e 's/^[[:space:]]*#.*$//' -e 's/[[:space:]]#.*$//' "$RUN_BODY" > "$RUN_CODE"
    HW_LINE=$(grep -n 'self\.init_hw()' "$RUN_CODE" | tail -1 | cut -d: -f1)
    CLEAR_LINE=$(grep -n 'self\._clear_legacy_retained()' "$RUN_CODE" | head -1 | cut -d: -f1)
    if [ -z "$HW_LINE" ] || [ -z "$CLEAR_LINE" ]; then
        fail "C: run() no longer calls both init_hw() and the clear in CODE (a commented-out call does not count) — cannot check their order"
    elif [ "$HW_LINE" -ge "$CLEAR_LINE" ]; then
        fail "C: run() calls the legacy clear BEFORE init_hw() — that reopens the 6-14 s window in which a beeper/DO command is accepted and silently dropped"
    fi
    # Scoped to the callback that must carry it, not the whole file — the same
    # mention-vs-structure rule the cap and marker pins already follow.
    HW_CB="$TMP/hw_cb.txt"
    py_method_body _make_hw_cb > "$HW_CB"
    if [ ! -s "$HW_CB" ]; then
        fail "C: could not extract _make_hw_cb — the gate cannot see whether a dropped command leaves a trace"
    else
        grep -q 'command dropped' "$HW_CB" \
            || fail "C: the not-ready hardware branch is silent again — a dropped command must leave a trace"
    fi
    [ "$FAIL" -eq 0 ] && ok "run() logs the id, inits HW before the clear, and never drops a command silently"
fi

CLEAR_ALL="$TMP/clear_all.txt"
CLEAR_ONE="$TMP/clear_one.txt"
py_func_body clear_legacy_retained > "$CLEAR_ALL"
py_func_body _clear_one_legacy     > "$CLEAR_ONE"

if [ ! -s "$CLEAR_ALL" ] || [ ! -s "$CLEAR_ONE" ]; then
    fail "C: could not extract the clear functions from $TEL_PY — the gate cannot see their guards"
else
    grep -q '_broker_is_loopback(broker)' "$CLEAR_ALL" \
        || fail "C: the clear no longer consults the loopback gate — a shared broker would let this board wipe a neighbour's live subtree"
    # Asserted INSIDE the resolver, not as a mention: `is_loopback` also occurs
    # in the function NAME and at the call site, so a whole-file grep stayed
    # green with the body replaced by `return True` (found by review mutation).
    LOOPBACK_BODY="$TMP/loopback_body.txt"
    py_func_body _broker_is_loopback > "$LOOPBACK_BODY"
    if [ ! -s "$LOOPBACK_BODY" ]; then
        fail "C: could not extract _broker_is_loopback — the gate cannot see whether it resolves anything"
    else
        grep -qF 'ip_address(value).is_loopback' "$LOOPBACK_BODY" \
            || fail "C: the loopback test no longer resolves the address — a bare hostname would pass as local and the clear would fire on a broker this board cannot prove is its own"
    fi
    # Pinned as a COMPARISON, not as a mention: the WARN message this branch
    # emits itself names `meta/driver` and TELEMETRY_DRIVER, so a mention-grep
    # stayed green with the whole check gutted (found by mutation). Prose can
    # name the marker; only code can compare against it.
    grep -qE '!=[[:space:]]*TELEMETRY_DRIVER' "$CLEAR_ONE" \
        || fail "C: the retained meta/driver ownership proof is gone — the clear would fire on a subtree it cannot prove is its own"
    grep -qF '_payload_text(collected.get(' "$CLEAR_ONE" \
        || fail "C: the ownership proof no longer reads the collected driver marker"
    grep -qE '>=[[:space:]]*LEGACY_CLEAR_MAX_TOPICS' "$CLEAR_ONE" \
        || fail "C: the collection cap comparison is gone — a runaway tree can stall startup"
    grep -q 'msg, "retain"' "$CLEAR_ONE" \
        || fail "C: the retained-only filter is gone — a LIVE message would be erased"
    [ "$FAIL" -eq 0 ] && ok "loopback gate, driver-marker proof, retain filter and cap all applied in the clear"
fi

if [ "$FAIL" -ne 0 ]; then
    echo "telemetry-device-id-contract: FAILED"
    exit 1
fi
echo "telemetry-device-id-contract: OK"
