#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-audio-trigger.sh — behavioural harness for the «Запись звука» card's
# privileged helper usr/local/sbin/sa02m-audio-web-trigger.sh (quality row
# `audio-trigger`; contract docs/contracts/audio-recorder.md §16; root via
# etc/sudoers.d/sa02m-audio).
#
# Method (the Home Connect harness's): the block between the helper's
# `>>> FUNCTIONS` / `<<< FUNCTIONS` markers is extracted from the SHIPPED file
# and run in a sandbox. Every path constant is re-pointed into a scratch dir;
# `systemctl`, `timeout` and `logger` are PATH shims — nothing here talks to
# systemd, needs root or touches a real /etc. The `timeout` shim marks its
# child, so a systemctl reached WITHOUT a timeout is recorded as UNBOUNDED
# (the web-code-rigor "timeouts everywhere" floor measured, not grepped).
#
# Cases:
#   A  extraction + non-vacuity (a failed extraction, a missing function or a
#      run with < 4 systemctl calls FAILS)
#   B  argv: unknown verb, extra argument, empty argv, the Home Connect verb
#      `unlink`, an action spelling, a shell-injection shape — non-zero, no
#      systemctl
#   C  not installed (no unit file) — non-zero, no systemctl
#   D  disable: stop + disable, all bounded; a recording left in the sandbox
#      store is untouched (F10: clips stay)
#   E  enable: unmask + enable + restart, all bounded
#   F  restart: conf enabled ⇒ restart; disabled ⇒ `skipped`, no systemctl;
#      an `enabled = true` OUTSIDE [recorder] does not count
#   H  lock: a verb while another holds the lock answers `busy`
#   I  conf truth-set parity: au_conf_enabled agrees with
#      sa02m_audio.config.load().enabled over line and whole-file samples
#   P  root safety: a module planted on PYTHONPATH is never imported
#   L  a conf read with no 0/1 answer is `skipped` and logged in exactly one
#      line naming its cause (package absent); a clean answer logs nothing
#
# flock(1) missing (git-bash) ⇒ every case SKIP and exit 77: run.mjs reports
# SKIP, never PASS (quality-gate-rigor.md); Linux CI / WSL are the authority.
#
# Run: bash scripts/dev/test-audio-trigger.sh   (bash, python3, flock)
#      AUDIO_TRIGGER_SRC=<file> judges another copy of the helper (the RED run).
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${AUDIO_TRIGGER_SRC:-$HERE/usr/local/sbin/sa02m-audio-web-trigger.sh}"
PKG="$HERE/opt/sa02m-audio"

fails=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -r "$SRC" ] || { echo "audio-trigger: FAIL — cannot read $SRC"; exit 1; }
[ -f "$PKG/sa02m_audio/config.py" ] || { echo "audio-trigger: FAIL — $PKG/sa02m_audio/config.py absent"; exit 1; }
if ! command -v flock >/dev/null 2>&1; then
    for c in A B C D E F H I P L; do echo "  SKIP  $c flock(1) not found (util-linux) — not run (a skip is not a pass)"; done
    echo "audio-trigger: SKIPPED, NOT PASSED (no flock)"
    exit 77
fi
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "audio-trigger: FAIL — no python3"; exit 1; }

SB="$(mktemp -d "${TMPDIR:-/tmp}/audio-trigger.XXXXXX")" || { echo "audio-trigger: FAIL — mktemp"; exit 1; }
trap 'rm -rf -- "$SB"' EXIT
SHIM="$SB/shim"
mkdir -p "$SHIM"
CALLS="$SB/calls.log"

echo "A. extraction"
FN="$SB/functions.sh"
sed -n '/^# >>> FUNCTIONS/,/^# <<< FUNCTIONS/p' "$SRC" > "$FN"
n_fn_lines=$(grep -c . "$FN")
if [ "$n_fn_lines" -lt 30 ]; then
    bad "A1 the FUNCTIONS block extracted to $n_fn_lines line(s) — markers moved or the helper changed shape"
    echo "audio-trigger: $fails FAILURE(S)"; exit 1
fi
for f in au_log au_conf_enabled au_enable au_disable au_restart au_main; do
    grep -q "^$f() {" "$FN" || bad "A2 function $f missing from the extracted block"
done
if ! bash -n "$FN" 2>"$SB/syntax.err"; then
    bad "A3 the extracted block does not parse: $(head -c 200 "$SB/syntax.err")"
fi
[ "$fails" -eq 0 ] && ok "A1-A3 FUNCTIONS block extracted ($n_fn_lines lines, 6 functions) and parses"

# ── shims ───────────────────────────────────────────────────────────────────
cat > "$SHIM/systemctl" <<'EOF'
#!/usr/bin/env bash
b=UNBOUNDED; [ "${AU_VIA_TIMEOUT:-}" = 1 ] && b=BOUNDED
printf '%s %s\n' "$*" "$b" >> "$AU_CALLS"
exit 0
EOF
REAL_TIMEOUT=$(command -v timeout || true)
cat > "$SHIM/timeout" <<EOF
#!/usr/bin/env bash
if [ -n "$REAL_TIMEOUT" ]; then AU_VIA_TIMEOUT=1 exec "$REAL_TIMEOUT" "\$@"; fi
shift
AU_VIA_TIMEOUT=1 exec "\$@"
EOF
printf '#!/usr/bin/env bash\nprintf "logger %%s\\n" "$*" >> "$AU_LOGS"\nexit 0\n' > "$SHIM/logger"
chmod +x "$SHIM"/*
export PATH="$SHIM:$PATH" AU_CALLS="$CALLS" AU_LOGS="$SB/logger.log"

reset_tree() {  # $1 = enabled value
    rm -rf -- "$SB/root" "$CALLS" "$SB/logger.log"
    mkdir -p "$SB/root/etc/systemd/system" "$SB/root/etc/sa02m-audio" "$SB/root/var/lib/sa02m-audio/rec"
    : > "$SB/root/etc/systemd/system/sa02m-audio.service"
    : > "$CALLS"
    printf '[recorder]\nenabled = %s\nstorage = emmc\n' "${1:-false}" > "$SB/root/etc/sa02m-audio/sa02m-audio.conf"
}

run_trigger() {
    (
        set -euo pipefail
        UNIT=sa02m-audio.service
        UNIT_FILE=$SB/root/etc/systemd/system/sa02m-audio.service
        CONF=$SB/root/etc/sa02m-audio/sa02m-audio.conf
        LOCK_FILE=$SB/lockfile
        LOCK_WAIT_S=${AU_LOCK_WAIT_S:-5}
        PKG_DIR=${AU_TEST_PKG_DIR:-$PKG}
        # shellcheck source=/dev/null
        . "$FN"
        au_main "$@"
    ) > "$SB/out" 2>"$SB/err"
    RC=$?
}
: > "$SB/lockfile"

calls_of()  { grep -c "^$1 " "$CALLS" 2>/dev/null; }
n_calls()   { grep -c . "$CALLS" 2>/dev/null; }
unbounded() { grep -c ' UNBOUNDED$' "$CALLS" 2>/dev/null; }
out_has()   { grep -qF -- "$1" "$SB/out"; }
total_sysctl=0

echo "B. argv"
for argv in "bogus" "enable extra" "" "unlink" "record" "ENABLE" "enable;reboot" "restart --now"; do
    reset_tree false
    # shellcheck disable=SC2086
    run_trigger $argv
    if [ "$RC" -ne 0 ] && [ "$(n_calls)" -eq 0 ]; then
        ok "B argv '$argv' refused (rc=$RC, $(tr -d '\n' < "$SB/out")), no systemctl"
    else
        bad "B argv '$argv' was accepted (rc=$RC) or reached systemctl ($(n_calls) call(s))"
    fi
done

echo "C. not installed"
reset_tree true
rm -f "$SB/root/etc/systemd/system/sa02m-audio.service"
run_trigger enable
if [ "$RC" -ne 0 ] && out_has '"not_installed"' && [ "$(n_calls)" -eq 0 ]; then
    ok "C no unit file ⇒ not_installed, no systemctl"
else
    bad "C no unit file: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi

echo "D. disable"
reset_tree true
clip="$SB/root/var/lib/sa02m-audio/rec/20261015T120000Z-abcdef.wav"
printf 'RIFF' > "$clip"
run_trigger disable
total_sysctl=$((total_sysctl + $(n_calls)))
if [ "$RC" -eq 0 ] && [ "$(calls_of stop)" -eq 1 ] && [ "$(calls_of disable)" -eq 1 ] && [ "$(unbounded)" -eq 0 ] \
   && out_has '"action":"disable"'; then
    ok "D1 disable: stop + disable, all $(n_calls) systemctl call(s) timeout-bounded"
else
    bad "D1 disable: rc=$RC stop=$(calls_of stop) disable=$(calls_of disable) unbounded=$(unbounded)"
fi
[ "$(cat "$clip" 2>/dev/null)" = RIFF ] && ok "D2 a recording in the store survives disable (F10)" \
    || bad "D2 disable touched the recordings"

echo "E. enable"
reset_tree true
run_trigger enable
total_sysctl=$((total_sysctl + $(n_calls)))
if [ "$RC" -eq 0 ] && [ "$(calls_of unmask)" -eq 1 ] && [ "$(calls_of enable)" -eq 1 ] \
   && [ "$(calls_of restart)" -eq 1 ] && [ "$(unbounded)" -eq 0 ] && out_has '"action":"enable"'; then
    ok "E enable: unmask + enable + restart, all bounded"
else
    bad "E enable: rc=$RC unmask=$(calls_of unmask) enable=$(calls_of enable) restart=$(calls_of restart) unbounded=$(unbounded)"
fi

echo "F. restart"
reset_tree true
run_trigger restart
total_sysctl=$((total_sysctl + $(n_calls)))
if [ "$RC" -eq 0 ] && [ "$(calls_of restart)" -eq 1 ] && [ "$(unbounded)" -eq 0 ] && out_has '"applied":"restart"'; then
    ok "F1 conf enabled ⇒ bounded restart"
else
    bad "F1 conf enabled: rc=$RC restart=$(calls_of restart) out=$(cat "$SB/out")"
fi
reset_tree false
run_trigger restart
if [ "$RC" -eq 0 ] && [ "$(n_calls)" -eq 0 ] && out_has '"applied":"skipped"'; then
    ok "F2 conf disabled ⇒ skipped, no systemctl"
else
    bad "F2 conf disabled: rc=$RC calls=$(n_calls) out=$(cat "$SB/out")"
fi
reset_tree false
au_c="$SB/root/etc/sa02m-audio/sa02m-audio.conf"
{ printf '[other]\nenabled = true\n\n'; cat "$au_c"; } > "$au_c.new" && mv "$au_c.new" "$au_c"
run_trigger restart
if [ "$RC" -eq 0 ] && [ "$(n_calls)" -eq 0 ] && out_has '"applied":"skipped"'; then
    ok "F3 an \`enabled = true\` outside [recorder] does not count (the daemon reads [recorder] only)"
else
    bad "F3 section-blind enabled read: rc=$RC calls=$(n_calls) out=$(cat "$SB/out")"
fi

echo "H. lock"
reset_tree true
exec 8<"$SB/lockfile"
flock 8
AU_LOCK_WAIT_S=0 run_trigger enable
flock -u 8
exec 8<&-
if [ "$RC" -ne 0 ] && out_has '"busy"' && [ "$(n_calls)" -eq 0 ]; then
    ok "H a held lock ⇒ busy, no systemctl"
else
    bad "H held lock: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi

echo "I. conf truth-set parity with sa02m_audio.config"
i_n=0; i_bad=0
while IFS= read -r sample; do
    [ -n "$sample" ] || continue
    conf="$SB/parity.conf"
    printf '%b' "$sample" > "$conf"
    py=$(PYTHONPATH="$PKG" "$PY" -c 'import sys; from sa02m_audio import config; print("1" if config.load(sys.argv[1]).enabled else "0")' "$conf" 2>/dev/null)
    sh=$( ( CONF=$conf; PKG_DIR=$PKG; . "$FN"; au_conf_enabled && echo 1 || echo 0 ) 2>/dev/null )
    i_n=$((i_n + 1))
    if [ -z "$py" ] || [ "$py" != "$sh" ]; then
        bad "I sample '$sample': daemon says '${py:-<error>}', helper says '$sh'"
        i_bad=$((i_bad + 1))
    fi
done <<'SAMPLES'
[recorder]\nenabled = true\n
[recorder]\nenabled = false\n
[recorder]\nenabled=TRUE\n
[recorder]\nenabled = yes\n
[recorder]\nenabled = on\n
[recorder]\nenabled = 1\n
[recorder]\nenabled = maybe\n
[recorder]\n# enabled = true\n
[recorder]\nenabled =\n
[recorder]\r\nenabled = true\r\n
[recorder]\nenabled = true\nenabled = false\n
[recorder]\nenabled = true\n[recorder]\nstorage = usb\n
[recorder]\nenabled = true\ngarbage line\n
[DEFAULT]\nenabled = true\n[recorder]\nstorage = usb\n
enabled = true\n[recorder]\nenabled = true\n
\xef\xbb\xbf[recorder]\nenabled = true\n
[recorder]\nenabled = true\ndevice = \xff\n
[recorder]\nenabled = 100%\n
SAMPLES
[ "$i_n" -ge 18 ] || bad "I only $i_n parity samples ran (non-vacuity floor 18)"
[ "$i_bad" -eq 0 ] && [ "$i_n" -ge 18 ] && ok "I helper and daemon agree on all $i_n conf samples"

echo "P. root safety of the conf read"
reset_tree true
mkdir -p "$SB/poison"
rm -f "$SB/poisoned"
printf 'open("%s/poisoned", "w").write("imported\\n")\nraise ImportError("poisoned")\n' "$SB" > "$SB/poison/configparser.py"
export PYTHONPATH="$SB/poison"
run_trigger restart
unset PYTHONPATH
if [ ! -e "$SB/poisoned" ] && [ "$RC" -eq 0 ] && [ "$(calls_of restart)" -eq 1 ] && out_has '"applied":"restart"'; then
    ok "P a module planted on PYTHONPATH is never imported by the root conf read"
else
    bad "P poisoned PYTHONPATH: imported=$([ -e "$SB/poisoned" ] && echo yes || echo no) rc=$RC restart=$(calls_of restart) out=$(cat "$SB/out")"
fi

echo "L. a conf read that gives no 0/1 answer is logged, exactly once"
log_lines() { [ -f "$SB/logger.log" ] || { echo 0; return; }; grep -c 'conf read' "$SB/logger.log" || true; }
reset_tree true
mkdir -p "$SB/nopkg"
AU_TEST_PKG_DIR="$SB/nopkg" run_trigger restart
line=$(grep -F 'conf read' "$SB/logger.log" 2>/dev/null || true)
if [ "$RC" -eq 0 ] && [ "$(n_calls)" -eq 0 ] && out_has '"applied":"skipped"' \
   && [ "$(log_lines)" -eq 1 ] && [[ $line == *ModuleNotFoundError* ]]; then
    ok "L1 package absent ⇒ skipped, exactly one log line naming ModuleNotFoundError"
else
    bad "L1 package absent: rc=$RC calls=$(n_calls) lines=$(log_lines) out=$(cat "$SB/out")"
fi
reset_tree false
run_trigger restart
if [ "$RC" -eq 0 ] && out_has '"applied":"skipped"' && [ "$(log_lines)" -eq 0 ]; then
    ok "L2 a clean \"not enabled\" answer logs no conf-read line"
else
    bad "L2 noise on a clean answer: rc=$RC lines=$(log_lines)"
fi

echo "Z. non-vacuity"
if [ "$total_sysctl" -ge 4 ]; then
    ok "Z $total_sysctl systemctl calls observed across the verb cases (floor 4)"
else
    bad "Z only $total_sysctl systemctl calls observed — the shims saw nothing (vacuous run)"
fi

if [ "$fails" -eq 0 ]; then
    echo "audio-trigger: all checks passed"
    exit 0
fi
echo "audio-trigger: $fails FAILURE(S)"
exit 1
