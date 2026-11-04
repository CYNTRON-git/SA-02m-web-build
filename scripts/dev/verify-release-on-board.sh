#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# verify-release-on-board.sh — PASS/FAIL verification of a DEPLOYED release,
# run ON A BOARD over SSH. Graduated from the one-off verify-41.sh that
# verified 1.0.6.41 on bench 1.135/1.136 (web-diagnostic-tools.md standing
# rule 4: a recipe worth keeping lands in scripts/ + a catalog row).
#
# It is NOT a quality-registry row and must not become one: the checks require
# the installed system (/var/www, systemd, installed scenario components, the
# archive DB), so in CI it could only skip — and a check that passes because it
# tested nothing is the defect quality-gate-rigor.md exists to stop. Its bash
# is covered by the `shellcheck` row (review beat, CI-authoritative), which
# sweeps scripts/dev/. Fixture isolation is also tested locally; deployed
# composition still requires the board run.
#
# Non-vacuity: off a board (no /var/www/network_config) it EXITS 2 without
# printing a single PASS, so a wrong invocation can never read as green. Every
# check that cannot run says SKIP, and skips are counted and printed
# separately from passes.
#
# Version-agnostic by construction: the expected version comes from the
# argument or from the board's own VERSION file, the HTTP per-run cap is read
# from its one home (sa02m_rules.store.HTTP_PER_RUN_MAX) instead of a literal,
# and nothing here pins a release-specific asset. The 1.0.6.41 run carried one
# release-specific check (a `--font-mono` token grep in the served CSS); that
# does not generalise and is replaced here by check 12, the 0-byte served
# asset sweep — the same torn-write class as check 3, which is what the
# release's atomic-install work was about.
#
# Usage:  bash verify-release-on-board.sh [X.Y.Z.W]
#         scp scripts/dev/verify-release-on-board.sh root@<board>:/tmp/ && \
#           ssh root@<board> 'bash /tmp/verify-release-on-board.sh 1.0.6.42'
# Exit:   0 = no FAIL, 1 = at least one FAIL, 2 = not runnable here.
# ═══════════════════════════════════════════════════════════════════════════
set -u

WEB_ROOT=/var/www/network_config

case "${1:-}" in
    -h|--help)
        sed -n '2,33p' "$0"
        exit 0
        ;;
esac

if [ ! -d "$WEB_ROOT" ]; then
    echo "verify-release-on-board.sh: $WEB_ROOT is absent — this harness reads a"
    echo "RUNNING board (systemd, the live daemons, the archive DB) and has"
    echo "nothing to verify here. Run it on the board over SSH."
    exit 2
fi

VER=${1:-}
if [ -z "$VER" ]; then
    VER=$(tr -d '\r' < "$WEB_ROOT/VERSION" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1)
fi
if [ -z "$VER" ]; then
    echo "verify-release-on-board.sh: no version given and $WEB_ROOT/VERSION is unreadable"
    exit 2
fi

P=0; F=0; S=0
ok()   { echo "PASS $1"; P=$((P + 1)); }
no()   { echo "FAIL $1"; F=$((F + 1)); }
skip() { echo "SKIP $1"; S=$((S + 1)); }

# A python block prints its own PASS/FAIL/SKIP lines; tally them so the summary
# counts every check, not only the shell ones.
#
# Non-vacuity: a block that DIES — an import error, an absent daemon, a
# traceback — prints no verdict line at all, so a plain tally would add zero and
# the run would exit 0 having verified nothing. That is the shape this whole
# harness exists to catch, so the caller passes the number of verdict lines it
# expects and a shortfall becomes a FAIL naming the block.
tally() {
    _p=$(printf '%s\n' "$1" | grep -c '^PASS ')
    _f=$(printf '%s\n' "$1" | grep -c '^FAIL ')
    _s=$(printf '%s\n' "$1" | grep -c '^SKIP ')
    P=$((P + _p)); F=$((F + _f)); S=$((S + _s))
    if [ -n "${2:-}" ] && [ $((_p + _f + _s)) -lt "$2" ]; then
        no "${3:-python block}: printed $((_p + _f + _s)) verdict line(s), expected $2 — it died before reporting"
    fi
}

# Mutating checks below use private stores and one in-process loopback listener.
# SIGKILL may leave a private temporary directory, never live scenario rows or
# a detached listener. The Python worker watches this shell's process identity.
VERIFY_OWNER_PID=$$
VERIFY_OWNER_START=$(awk '{print $22}' "/proc/$$/stat")

LANIP=$(ip -4 addr show scope global 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
echo "== board $(hostname) ${LANIP:-<no LAN address>} — verifying $VER =="

# 1 web version: VERSION == APP_VERSION == every ?v= in the served markup
V=$(tr -d '\r' < "$WEB_ROOT/VERSION" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1)
A=$(grep -oE "APP_VERSION *= *['\"][0-9.]+" "$WEB_ROOT/static/js/app.js" 2>/dev/null | grep -oE '[0-9.]+$')
SKEW=$(grep -hoE '\?v=[0-9.]+' "$WEB_ROOT/index.html" "$WEB_ROOT/login.html" 2>/dev/null \
       | grep -v "?v=$VER" | sort -u | head -3)
if [ "$V" = "$VER" ] && [ "$A" = "$VER" ] && [ -z "$SKEW" ]; then
    ok "1 web version $VER (VERSION + APP_VERSION + every ?v=)"
else
    no "1 web version: VERSION=$V APP_VERSION=$A skew='$SKEW'"
fi

# 2 core units active. A unit the board does not ship is not a failure — only a
# shipped unit that is not running is.
BAD=""
for u in nginx mosquitto sa02m-rules sa02m-alice sa02m-devices-logger sa02m-modbus-mqtt sa02m-flasher; do
    systemctl list-unit-files "$u.service" --no-legend 2>/dev/null | grep -q . || continue
    s=$(systemctl is-active "$u" 2>/dev/null)
    [ "$s" = active ] || BAD="$BAD $u=$s"
done
# The devices API is served by sa02m-devices-api, or by the stand's gunicorn
# (sa02m-stand-api on a stand board, bound to the same socket since 1.0.6.65) — either owner is a PASS.
DA=$(systemctl is-active sa02m-devices-api 2>/dev/null)
DS=$(systemctl is-active sa02m-stand-api 2>/dev/null)
[ "$DA" = active ] || [ "$DS" = active ] || BAD="$BAD devices-api=$DA stand-api=$DS"
echo "INFO 2b devices API owner: sa02m-devices-api=$DA sa02m-stand-api=$DS"
if [ -z "$BAD" ]; then ok "2 core units active"; else no "2 core units:$BAD"; fi

# 3 no 0-byte unit fragment — systemd reads an empty unit as MASKED (8D
# bench-136 reset, the defect the atomic install helpers exist for).
Z=$(find /etc/systemd/system -maxdepth 2 -type f -name '*.service' -size 0 2>/dev/null | head -5)
if [ -z "$Z" ]; then ok "3 no 0-byte unit fragment under /etc/systemd/system"; else no "3 empty unit fragment: $Z"; fi

# 4 the update runner's version stamp matches what is deployed
RS=$(tr -d '\r\n ' < /var/lib/sa02m-update/runner.version 2>/dev/null)
if [ "$RS" = "$VER" ]; then ok "4 runner.version stamp = $VER"; else no "4 runner.version = '$RS' (expected $VER)"; fi

# 5–9 execute INSTALLED components in an isolated store, not the live daemon.
# Live service readiness is check 2. No MQTT publisher or production store is used.
if [ ! -d /opt/sa02m-rules ]; then
    skip "5-9 isolated scenario components: /opt/sa02m-rules is not installed"
else
    OUT=$(python3 - "$VERIFY_OWNER_PID" "$VERIFY_OWNER_START" <<'ISOLATED_FIXTURES'
# BEGIN ISOLATED FIXTURES
import contextlib
import http.server
import json
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, "/opt/sa02m-rules")
sys.path.insert(0, "/opt/sa02m-alice")


def watch_parent(pid, start_ticks):
    """A parent-owned lifetime, including SIGKILL; no finally assumption."""
    fd = os.pidfd_open(pid)
    try:
        fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
        if fields[19] != start_ticks:
            raise RuntimeError('verification parent identity changed')
    except BaseException:
        os.close(fd)
        raise
    def wait():
        select.select([fd], [], [])
        os._exit(125)
    threading.Thread(target=wait, daemon=True).start()
    return fd


@contextlib.contextmanager
def private_environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_fixture_checks(phase=lambda *_args: None):
    from sa02m_rules import store, engine, http_guard
    def verdict(number, passed, message):
        print(('PASS' if passed else 'FAIL') + ' ' + str(number) + ' isolated ' + message, flush=True)

    with tempfile.TemporaryDirectory(prefix='sa02m-release-check-') as directory:
        root = Path(directory)
        path = str(root / 'scenarios.json')
        devices = root / 'devices.json'
        devices.write_text(json.dumps({'devices': [{'id': 'fixture-device', 'type': 'devices.types.light',
            'capabilities': [{'type': 'devices.capabilities.on_off', 'mqtt': '/devices/fixture/controls/on'}]}]}))
        allow = root / 'http-allow.json'
        allow.write_text('{"hosts":[]}')
        with private_environment({'SA02M_ALICE_DEVICES': str(devices), 'SA02M_RULES_HTTP_ALLOW': str(allow),
                                  'no_proxy': '127.0.0.1', 'NO_PROXY': '127.0.0.1'}):
            store.save(store.empty_doc(), path)
            phase('store', directory)
            def command(body):
                result = store.apply_command(body, path)
                if not result.get('ok'):
                    raise RuntimeError('fixture command refused: %r' % result)
                return result
            def publish(*_args):
                raise RuntimeError('verification fixture attempted a device write')
            command({'id': 'vtmo', 'name': 'verify timeout', 'type': 'code', 'enabled': True,
                     'code': 'while True:\n    pass\n'})
            runtime = engine.Engine(publish, path, pub_state=lambda *_args: None)
            before = Path(path).read_bytes(), Path(path).stat().st_mtime_ns
            started = time.time()
            record = runtime.run_now('vtmo') or {}
            verdict(5, record.get('error') == 'timeout', 'sandbox deadline: error=%r' % record.get('error'))
            journal = json.loads(Path(store.journal_path(path)).read_text())
            found = any(r.get('id') == 'vtmo' and r.get('ts', 0) >= started - 1 for r in journal.get('runs', []))
            unchanged = before == (Path(path).read_bytes(), Path(path).stat().st_mtime_ns)
            verdict(7, found and unchanged, 'runs journal retains run; scenario bytes/mtime unchanged=%s' % unchanged)

            cases = [('http://127.0.0.1:9/x', False), ('http://[2002:7f00:1::]:9/x', False),
                     ('http://[::ffff:127.0.0.1]:9/x', False), ('http://169.254.169.254/x', False),
                     ('http://192.168.1.135:9999/x', True)]
            verdict(6, all(bool(http_guard.check_url(url, resolve=True)) != allowed for url, allowed in cases),
                    'default HTTP policy (5 literal-address checks; no network request)')

            token = uuid.uuid4().hex
            hits = []
            class Counter(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if not self.path.startswith('/' + token + '/'):
                        self.send_error(404)
                        return
                    hits.append(self.path)
                    self.send_response(200)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                def log_message(self, *_args):
                    pass
            server = http.server.HTTPServer(('127.0.0.1', 0), Counter)
            listener = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
            listener.start()
            try:
                port = server.server_address[1]
                phase('listener', port)
                # A private allow-list permits only our loopback listener for the cap test.
                allow.write_text('{"hosts":["127.0.0.1"]}')
                cap = store.HTTP_PER_RUN_MAX
                code = "for i in range(%d):\n    Http.get('http://127.0.0.1:%d/%s/' + str(i))\n" % (cap * 2 + 4, port, token)
                command({'id': 'vcap', 'name': 'verify HTTP cap', 'type': 'code', 'enabled': True, 'code': code})
                runtime = engine.Engine(publish, path, pub_state=lambda *_args: None)
                record = runtime.run_now('vcap') or {}
                verdict(8, 'cap' in record.get('error', ''), 'HTTP per-run cap: error=%r' % record.get('error'))
                verdict('8b', len(hits) == cap, 'loopback listener received %d requests (expected %d)' % (len(hits), cap))
            finally:
                server.shutdown()
                server.server_close()
                listener.join(timeout=1)

            try:
                from sa02m_alice.config import scene_devices
            except ImportError:
                print('SKIP 9 isolated scene projection: Alice component not installed', flush=True)
            else:
                rooms = [{'id': 'r1', 'name': 'verify room'}]
                board = 'fixture-board'
                def project():
                    return scene_devices.exposed_scene_devices(scene_devices.load_rules_doc(path), rooms, board)
                before = len(project())
                command({'id': 'vscn', 'name': 'verify scene', 'type': 'scene', 'enabled': True,
                         'alice_expose': True, 'captured_from': {'room_id': 'r1'},
                         'action': [{'kind': 'set', 'device': 'fixture-device', 'cap': 'on_off', 'value': True}]})
                devices = project()
                mine = next((d for d in devices if str(d.get('id', '')).endswith('-vscn')), {})
                good = (len(devices) == before + 1 and mine.get('type') == scene_devices.SCENE_DEVICE_TYPE
                        and mine.get('room_id') == 'r1' and (mine.get('capabilities') or [{}])[0].get('mqtt')
                        == scene_devices.scene_run_topic('vscn'))
                verdict(9, good, 'stored scene projects to virtual device (never executed)')
            phase('complete', directory)


if __name__ == '__main__':
    owner = watch_parent(int(sys.argv[1]), sys.argv[2])
    def cancelled(_signum, _frame):
        raise SystemExit(130)
    signal.signal(signal.SIGINT, cancelled)
    signal.signal(signal.SIGTERM, cancelled)
    run_fixture_checks()
# END ISOLATED FIXTURES
ISOLATED_FIXTURES
)
    printf '%s\n' "$OUT"
    tally "$OUT" 6 "checks 5-9 (isolated installed components)"
fi

# 10 the Carel archive is wide, the rollback copy is there, a read is quick
OUT=$(python3 - <<'PY'
import sqlite3, glob, time, os
pats = ["/var/lib/sa02m-devices/*.db", "/var/lib/sa02m-stand/*.db",
        "/mnt/*/sa02m-devices/*.db", "/media/*/sa02m-devices/*.db",
        "/media/*/*.db"]
paths = []
for pat in pats:
    paths += sorted(glob.glob(pat))
paths = [p for p in paths if os.path.basename(p).startswith("devices_history")]
if not paths:
    print("SKIP 10 Carel wide table: no devices_history db on this board")
else:
    p = paths[0]
    c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    tabs = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "carel_samples" not in tabs:
        print("SKIP 10 Carel wide table: no carel_samples in %s" % p)
    else:
        cols = [r[1] for r in c.execute("PRAGMA table_info(carel_samples)")]
        wide = len(cols) > 4 and "metric" not in cols
        n = c.execute("SELECT count(*) FROM carel_samples").fetchone()[0]
        n1 = (c.execute("SELECT count(*) FROM carel_samples_v1").fetchone()[0]
              if "carel_samples_v1" in tabs else -1)
        t0 = time.time()
        c.execute("SELECT * FROM carel_samples ORDER BY ts DESC LIMIT 5000").fetchall()
        dt = time.time() - t0
        print(("PASS" if (wide and dt <= 10.0) else "FAIL") +
              " 10 Carel wide table: %d cols, %d rows (v1 keeps %d), 5000-row read %.2fs (<=10s)"
              % (len(cols), n, n1, dt))
PY
)
printf '%s\n' "$OUT"
tally "$OUT" 1 "check 10 (Carel wide archive)"

# 11 the runtime watchdog is back after the install (the installer holds it off
# during a run; a board left with it disabled is the regression)
WD=$(busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1 \
     org.freedesktop.systemd1.Manager RuntimeWatchdogUSec 2>/dev/null | awk '{print $2}')
POL=$(grep -hE '^RuntimeWatchdogSec=' /etc/systemd/system.conf.d/sa02m-watchdog.conf 2>/dev/null | tail -1)
if [ -n "$WD" ] && [ "$WD" != "0" ]; then
    ok "11 runtime watchdog restored: RuntimeWatchdogUSec=$WD (policy $POL)"
else
    no "11 runtime watchdog = '${WD:-unread}' after install (policy $POL)"
fi

# 12 no 0-byte served asset — check 3's class applied to the web root: a torn
# copy leaves a named-but-empty file, and an empty JS bundle blanks the page.
ZW=$(find "$WEB_ROOT" -type f \( -name '*.js' -o -name '*.css' -o -name '*.html' \
     -o -name '*.svg' -o -name '*.cgi' -o -name 'VERSION' \) -size 0 2>/dev/null | head -5)
if [ -z "$ZW" ]; then
    ok "12 no 0-byte served asset under $WEB_ROOT"
else
    no "12 empty served asset(s): $(printf '%s' "$ZW" | tr '\n' ' ')"
fi

# 13 the offline-update post-checks of the INSTALLED tree, when that install
# was --log'ged here. The wrapper (scripts/offline-full-update.sh) prints its
# PASS/FAIL table to stdout and, with --log PATH, appends it there; the
# runbooks name that file /root/install-offline-<ver>[-<sha7>].wrapper.log
# (docs/deployment.md «Офлайн-вариант»; the -<sha7> follows the archive name).
# The log accumulates across re-runs: the LAST «ИТОГ:» line is the verdict.
# Several logs of this version may sit in /root: the NEWEST by mtime is judged
# and named. It counts only if it was last written no earlier than the newest
# delivery of the tree — otherwise it is the table of an EARLIER install (bench
# 136, 2026-10-24: a 2026-10-22 log PASSed a later install it never saw) and
# the check SKIPs, as for an absent log: nothing here records THIS install.
# The delivery anchor is the newer mtime of runner.version — every install
# site writes it with `install` (fresh mtime), inside install.sh before the
# wrapper appends its table — and the deployed VERSION, which a pscp www-only
# copy moves without stamping. `-<ver>-*` and not `-<ver>*`: 1.0.7.1 must not
# pick up 1.0.7.10's log. An interactive run keeps its table on the terminal
# only — nothing to read. (Until 1.0.6.54 this read /root/offline-<ver>.out,
# which nothing writes: a check that could only ever SKIP.)
mtime_of() { date -r "$1" '+%F %T' 2>/dev/null || echo 'mtime unread'; }
OUTF=""
for f in "/root/install-offline-$VER.wrapper.log" /root/install-offline-"$VER"-*.wrapper.log; do
    [ -f "$f" ] || continue
    if [ -z "$OUTF" ] || [ "$f" -nt "$OUTF" ]; then OUTF=$f; fi
done
ANCHOR=""
for f in /var/lib/sa02m-update/runner.version "$WEB_ROOT/VERSION"; do
    [ -f "$f" ] || continue
    if [ -z "$ANCHOR" ] || [ "$f" -nt "$ANCHOR" ]; then ANCHOR=$f; fi
done
if [ -z "$OUTF" ]; then
    skip "13 offline update post-checks: no /root/install-offline-${VER}[-<sha7>].wrapper.log (no --log'ged offline update of $VER here; an interactive run keeps its table on the terminal only)"
elif [ -z "$ANCHOR" ]; then
    skip "13 offline update post-checks: $OUTF ($(mtime_of "$OUTF")) found, but neither runner.version nor VERSION is readable — cannot tell whether it describes the installed tree"
elif [ "$OUTF" -ot "$ANCHOR" ]; then
    skip "13 offline update post-checks: newest log $OUTF ($(mtime_of "$OUTF")) is older than $ANCHOR ($(mtime_of "$ANCHOR")) — it records an earlier install, not the tree on this board"
else
    VERDICT=$(grep -E 'ИТОГ: (PASS|FAIL)' "$OUTF" 2>/dev/null | tail -1)
    case "$VERDICT" in
        *'ИТОГ: PASS'*) ok "13 offline update post-checks ($OUTF, $(mtime_of "$OUTF")): ${VERDICT#*ИТОГ: }" ;;
        *'ИТОГ: FAIL'*) no "13 offline update post-checks ($OUTF, $(mtime_of "$OUTF")): ${VERDICT#*ИТОГ: }" ;;
        *) no "13 offline update post-checks: $OUTF ($(mtime_of "$OUTF")) has no «ИТОГ:» line (the wrapper is still running or died before its table)" ;;
    esac
fi

# 14 the journal is persistent: the Armbian RAM-log hooks are off (their cron
# moved the journal to /run every 15 min — header of etc/default/armbian-ramlog),
# nothing is left in the runtime journal, and the EFFECTIVE journald config
# syncs every minute (etc/systemd/sa02m-journald.conf). Both files arrive only
# with a full install of >=1.0.6.51 (install.sh / offline full update) or a
# golden image built after it — OTA carries neither — so on a board that has not
# had such an install this check FAILs, and a full install fixes it.
RL=$(grep -E '^ENABLED=' /etc/default/armbian-ramlog 2>/dev/null | tail -1)
NRUN=$(find /run/log/journal -name '*.journal' 2>/dev/null | wc -l | tr -d ' ')
SYNC=$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null \
       | grep -E '^[[:space:]]*SyncIntervalSec=' | tail -1 | tr -d ' ')
if [ "$RL" = "ENABLED=false" ] && [ "$NRUN" = 0 ] && [ "$SYNC" = "SyncIntervalSec=1m" ]; then
    ok "14 journal persistent: armbian-ramlog ENABLED=false, 0 runtime journal files, SyncIntervalSec=1m in force"
else
    no "14 journal persistent: armbian-ramlog '${RL:-unset}', $NRUN runtime journal file(s) under /run/log/journal, effective '${SYNC:-SyncIntervalSec unread}'"
fi

echo "== $VER on $(hostname): $P PASS / $F FAIL / $S SKIP =="
[ "$F" -eq 0 ] || exit 1
exit 0
