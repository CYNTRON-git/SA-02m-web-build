#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# verify-release-on-board.sh — PASS/FAIL verification of a DEPLOYED release,
# run ON A BOARD over SSH. Graduated from the one-off verify-41.sh that
# verified 1.0.6.41 on bench 1.135/1.136 (web-diagnostic-tools.md standing
# rule 4: a recipe worth keeping lands in scripts/ + a catalog row).
#
# It is NOT a quality-registry row and must not become one: every check reads
# the running system (/var/www, systemd, the live sa02m-rules daemon, the
# archive DB), so in CI it could only skip — and a check that passes because it
# tested nothing is the defect quality-gate-rigor.md exists to stop. Its bash
# is covered by the `shellcheck` row (review beat, CI-authoritative), which
# sweeps scripts/dev/; its BEHAVIOUR is only ever proven on a board.
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
tally() {
    P=$((P + $(printf '%s\n' "$1" | grep -c '^PASS ')))
    F=$((F + $(printf '%s\n' "$1" | grep -c '^FAIL ')))
    S=$((S + $(printf '%s\n' "$1" | grep -c '^SKIP ')))
}

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
# (sa02m-stand-api owns :8765 on a stand board) — either owner is a PASS.
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

# 5 + 6 + 7 + 8 the scenario sandbox, against the LIVE daemon.
# A throwaway LAN listener so the per-run HTTP cap is counted at the far end,
# not inferred from the error message alone.
if [ ! -d /opt/sa02m-rules ]; then
    skip "5-8 scenario sandbox: /opt/sa02m-rules is not installed on this board"
else
    LOG=/tmp/verify-listener.log
    rm -f "$LOG"
    setsid python3 -m http.server 9998 --bind 0.0.0.0 > "$LOG" 2>&1 < /dev/null &
    LPID=$!
    sleep 2
    OUT=$(python3 - "${LANIP:-127.0.0.1}" <<'PY'
import sys, os, json, time
sys.path.insert(0, "/opt/sa02m-rules")
from sa02m_rules import store, http_guard

LAN = sys.argv[1]
P = store.DEFAULT_PATH
CAP = store.HTTP_PER_RUN_MAX  # one home: store.charge_http

def cmd(b):
    return store.apply_command(b, P)

def row(sid):
    d = store.load(P)
    return next((s for s in d["scenarios"] if s.get("id") == sid), None)

def mk(sid, name, code):
    return cmd({"id": sid, "name": name, "type": "code",
                "enabled": True, "code": code}).get("ok")

def journal_count():
    j = store.journal_path(P)
    if not os.path.exists(j):
        return 0
    try:
        return len(json.load(open(j)).get("runs") or [])
    except Exception:
        return 0

def run(sid, wait=45):
    mt0 = os.path.getmtime(P)
    n0 = journal_count()
    before = (row(sid) or {}).get("last_run")
    cmd({"id": sid, "run_now": True})
    t0 = time.time()
    while time.time() - t0 < wait:
        r = row(sid)
        if r and r.get("last_run") and r.get("last_run") != before:
            break
        time.sleep(1)
    time.sleep(7)  # the runs journal flushes on a 5 s cadence
    return row(sid), os.path.getmtime(P) == mt0, journal_count() - n0

made = []
try:
    mk("vtmo", "verify timeout", "while True:\n    pass\n")
    made.append("vtmo")
    r, mt_same, dn = run("vtmo")
    err = (r or {}).get("last_error", "")
    alive = os.system("systemctl is-active --quiet sa02m-rules") == 0
    print(("PASS" if (err == "timeout" and alive) else "FAIL") +
          " 5 sandbox deadline: last_error=%r daemon_active=%s" % (err, alive))
    print(("PASS" if (mt_same and dn >= 1) else "FAIL") +
          " 7 runs journal +%d, scenarios.json mtime unchanged=%s" % (dn, mt_same))

    cases = [("http://127.0.0.1:9/x", False, "loopback"),
             ("http://[2002:7f00:1::]:9/x", False, "6to4 wrapping 127.0.0.1"),
             ("http://[::ffff:127.0.0.1]:9/x", False, "ipv4-mapped loopback"),
             ("http://169.254.169.254/x", False, "link-local metadata"),
             ("http://%s:9999/x" % LAN, True, "own LAN address")]
    bad = []
    for url, allow, label in cases:
        refused = http_guard.check_url(url, resolve=True)
        if bool(refused) == allow:
            bad.append("%s -> %s" % (label, refused or "ALLOWED"))
    print(("PASS" if not bad else "FAIL") + " 6 http target policy (5 cases): " +
          ("loopback/6to4/mapped/link-local refused, LAN allowed"
           if not bad else "; ".join(bad)))

    # No try/except in the scenario: the sandbox exposes no builtins, so the
    # request past the cap raises HttpRefused and it lands in last_error —
    # that IS the assertion.
    code = ("for i in range(%d):\n"
            "    Http.get('http://%s:9998/p' + str(i))\n" % (CAP * 2 + 4, LAN))
    mk("vcap", "verify http cap", code)
    made.append("vcap")
    r, _, _ = run("vcap", wait=60)
    err = (r or {}).get("last_error", "")
    print(("PASS" if "cap" in err else "FAIL") +
          " 8 http per-run cap: last_error=%r (the listener below must read %d hits)"
          % (err, CAP))
    print("INFO 8-cap %d" % CAP)
finally:
    for sid in made:
        cmd({"id": sid, "delete": True})
PY
)
    printf '%s\n' "$OUT"
    tally "$OUT"
    kill "$LPID" 2>/dev/null

    CAP=$(printf '%s\n' "$OUT" | sed -n 's/^INFO 8-cap //p' | head -1)
    HITS=$(grep -c 'GET /p[0-9]' "$LOG" 2>/dev/null | tr -d '\r\n')
    if [ -z "$CAP" ]; then
        skip "8b listener hit count: the sandbox block did not report the cap"
    elif [ "$HITS" = "$CAP" ]; then
        ok "8b listener received exactly $CAP requests before the cap"
    else
        no "8b listener received $HITS requests (expected $CAP)"
    fi
fi

# 9 a scenario exposed to Alice becomes a virtual device
if [ ! -d /opt/sa02m-alice ]; then
    skip "9 exposed scene: /opt/sa02m-alice is not installed on this board"
else
    OUT=$(python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/sa02m-alice")
sys.path.insert(0, "/opt/sa02m-rules")
try:
    from sa02m_alice.config import scene_devices
    from sa02m_rules import store
    P = store.DEFAULT_PATH
    ROOMS = [{"id": "r1", "name": "verify room"}]
    BOARD = scene_devices.board_key()

    def proj():
        return scene_devices.exposed_scene_devices(
            scene_devices.load_rules_doc(), ROOMS, BOARD)

    base = len(proj())
    store.apply_command({"id": "vscn", "name": "verify scene", "type": "scene",
                         "enabled": True, "alice_expose": True,
                         "captured_from": {"room_id": "r1"},
                         "action": [{"kind": "scenario", "id": "vscn2"}]}, P)
    devs = proj()
    mine = [d for d in devs if str(d.get("id", "")).endswith("-vscn")]
    d = mine[0] if mine else {}
    good = (len(devs) == base + 1
            and d.get("type") == scene_devices.SCENE_DEVICE_TYPE
            and str(d.get("id", "")).startswith(scene_devices.SCENE_ID_PREFIX)
            and d.get("room_id") == "r1"
            and (d.get("capabilities") or [{}])[0].get("mqtt")
            == scene_devices.scene_run_topic("vscn"))
    print(("PASS" if good else "FAIL") +
          " 9 an exposed scene becomes a device: id=%r type=%r room_id=%r topic=%r (%d -> %d)"
          % (d.get("id"), d.get("type"), d.get("room_id"),
             (d.get("capabilities") or [{}])[0].get("mqtt"), base, len(devs)))
    store.apply_command({"id": "vscn", "delete": True}, P)
except Exception as e:
    print("FAIL 9 scene_devices: %r" % (e,))
PY
)
    printf '%s\n' "$OUT"
    tally "$OUT"
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
tally "$OUT"

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

# 13 the offline-update post-checks of THIS version, when one was run here
OUTF=/root/offline-$VER.out
if [ ! -f "$OUTF" ]; then
    skip "13 offline update post-checks: $OUTF absent (no offline update run on this board)"
else
    NF=$(grep -c 'FAIL' "$OUTF" 2>/dev/null | head -1)
    NP=$(grep -c 'PASS' "$OUTF" 2>/dev/null | head -1)
    if [ "$NF" = "0" ] && [ "$NP" != "0" ]; then
        ok "13 offline update post-checks: $NP PASS, 0 FAIL"
    else
        no "13 offline update post-checks: $NP PASS, $NF FAIL"
    fi
fi

echo "== $VER on $(hostname): $P PASS / $F FAIL / $S SKIP =="
[ "$F" -eq 0 ] || exit 1
exit 0
