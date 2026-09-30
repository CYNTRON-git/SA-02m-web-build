#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-homekit-trigger.sh — behavioural harness for the HomeKit card's
# privileged helper usr/local/sbin/sa02m-homekit-web-trigger.sh (planned
# quality row `homekit-trigger`; contract docs/contracts/homekit-bridge.md).
#
# Method: the block between the helper's `>>> FUNCTIONS` / `<<< FUNCTIONS`
# markers is extracted from the SHIPPED file and run in a sandbox. Every path
# constant is re-pointed into a scratch dir; `systemctl`, `timeout` and
# `logger` are PATH shims — nothing here talks to systemd, needs root or
# touches a real /run, /var/lib or /etc. The `timeout` shim marks its child,
# so a systemctl reached WITHOUT a timeout is recorded as UNBOUNDED (the
# web-code-rigor "timeouts everywhere" floor measured, not grepped).
#
# Cases:
#   A  extraction + non-vacuity (a failed extraction, a missing function or a
#      run with < 4 systemctl calls FAILS)
#   B  argv: unknown verb, extra argument, empty argv — non-zero, no systemctl
#   C  not installed (no unit file) — non-zero, no systemctl
#   D  disable: stop + disable, all bounded; writes `disabled` when the status
#      is absent/older; NEVER clobbers a fresher file; replaces a planted
#      status.json symlink and leaves its target untouched; removes setup.json;
#      D8 a temp NAME swapped for a symlink after its creation (the daemon
#      winning the race, modelled by a losing `mktemp` shim) redirects nothing
#      — root never re-opens a name in the daemon's dir (RED on the pre-fix
#      mktemp + `printf > "$tmp"` + `chmod "$tmp"`: victim overwritten, 0644)
#      D9 a fallback that fails (a directory at status.json → EISDIR) logs
#      the exception text via logger, stdout stays the JSON answer, no temp
#      left (RED on the pre-fix `2>/dev/null`: the cause was dropped)
#   E  enable: unmask + enable + restart, all bounded
#   F  restart: conf enabled ⇒ restart; conf disabled ⇒ `skipped`, no restart;
#      an `enabled = true` OUTSIDE [bridge] does not count; `ENABLED = true`
#      does (configparser lower-cases keys)
#   G  reset-pairing: removes EXACTLY state.json + .hk-*.tmp; aids.json,
#      identity.json, the conf and a `.hk-*.tmp` DIRECTORY stay; starts again
#      only when enabled; refuses a symlinked or missing state dir; refuses to
#      delete while the unit still reports active OR deactivating after the
#      stop (is-active exits non-zero for both `deactivating` and `inactive`)
#   H  lock: a verb while another holds the lock answers `busy`
#   I  conf truth set parity with the daemon: hk_conf_enabled agrees with
#      sa02m_homekit.config.load().enabled over a sample matrix (key case and
#      a key in another section included)
#   I2 whole-file parity: CRLF, duplicate key (any case), duplicate section,
#      a parse error in [bridge] or elsewhere, a continuation line, [DEFAULT]
#      inheritance, `[bridge] ; c`, a key before any section, a UTF-8 BOM,
#      non-UTF-8 bytes — helper vs config.load() directly
#   P  root safety: a module planted on PYTHONPATH is never imported by the
#      helper's conf read (env -i + python3 -I)
#
# Comment-mutation cases to register with the row (measured RED here):
#   commenting out `hk_write_disabled_status "$since"` → D2 RED;
#   commenting out `rm -f -- "$VAR_DIR/state.json" 2>/dev/null || true` → G1 RED.
#
# Run: bash scripts/dev/test-homekit-trigger.sh      (bash, python3, flock)
#      HK_TRIGGER_SRC=<file> judges another copy of the helper (the RED run).
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${HK_TRIGGER_SRC:-$HERE/usr/local/sbin/sa02m-homekit-web-trigger.sh}"
PKG="$HERE/opt/sa02m-homekit"

fails=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -r "$SRC" ] || { echo "homekit-trigger: FAIL — cannot read $SRC"; exit 1; }
command -v flock >/dev/null 2>&1 || { echo "homekit-trigger: FAIL — flock not found (util-linux)"; exit 1; }

SB="$(mktemp -d "${TMPDIR:-/tmp}/hk-trigger.XXXXXX")" || { echo "homekit-trigger: FAIL — mktemp"; exit 1; }
trap 'rm -rf -- "$SB"' EXIT
SHIM="$SB/shim"
mkdir -p "$SHIM"
CALLS="$SB/calls.log"

echo "A. extraction"
FN="$SB/functions.sh"
sed -n '/^# >>> FUNCTIONS/,/^# <<< FUNCTIONS/p' "$SRC" > "$FN"
n_fn_lines=$(grep -c . "$FN")
if [ "$n_fn_lines" -lt 40 ]; then
    bad "A1 the FUNCTIONS block extracted to $n_fn_lines line(s) — markers moved or the helper changed shape"
    echo "homekit-trigger: $fails FAILURE(S)"; exit 1
fi
for f in hk_conf_enabled hk_write_disabled_status hk_enable hk_disable hk_restart hk_remove_pairing_store hk_reset_pairing hk_main; do
    grep -q "^$f() {" "$FN" || bad "A2 function $f missing from the extracted block"
done
[ "$fails" -eq 0 ] && ok "A1-A2 FUNCTIONS block extracted ($n_fn_lines lines, 8 functions)"

# ── shims ───────────────────────────────────────────────────────────────────
# systemctl: records "<verb> <unit> BOUNDED|UNBOUNDED"; `is-active` answers
# like the real one — prints the state (unless --quiet), exit 0 only for
# `active`: the state comes from $SB/active (present ⇒ its content, default
# `active`), else `inactive`. Never touches anything else.
cat > "$SHIM/systemctl" <<'EOF'
#!/usr/bin/env bash
b=UNBOUNDED; [ "${HK_VIA_TIMEOUT:-}" = 1 ] && b=BOUNDED
printf '%s %s\n' "$*" "$b" >> "$HK_CALLS"
case "$1" in
    is-active)
        st=inactive
        if [ -e "$HK_SB/active" ]; then
            st=$(cat "$HK_SB/active"); [ -n "$st" ] || st=active
        fi
        case " $* " in *" --quiet "*|*" -q "*) ;; *) printf '%s\n' "$st" ;; esac
        [ "$st" = active ] && exit 0
        exit 3 ;;
esac
exit 0
EOF
# timeout: `timeout N cmd…` → run cmd with the BOUNDED mark.
cat > "$SHIM/timeout" <<'EOF'
#!/usr/bin/env bash
shift
HK_VIA_TIMEOUT=1 exec "$@"
EOF
printf '#!/usr/bin/env bash\nprintf "logger %%s\\n" "$*" >> "$HK_LOGS"\nexit 0\n' > "$SHIM/logger"
chmod +x "$SHIM"/*
export PATH="$SHIM:$PATH" HK_CALLS="$CALLS" HK_SB="$SB" HK_LOGS="$SB/logger.log"

# Fresh sandbox tree + constants for one case.
reset_tree() {
    rm -rf -- "$SB/root" "$SB/active" "$CALLS" "$SB/logger.log"
    mkdir -p "$SB/root/etc/systemd/system" "$SB/root/etc/sa02m-homekit" \
             "$SB/root/var/lib/sa02m-homekit" "$SB/root/run/sa02m-homekit"
    : > "$SB/root/etc/systemd/system/sa02m-homekit.service"
    : > "$CALLS"
    printf '[bridge]\nenabled = %s\ninterface = eth0\nport = 21064\n' "${1:-false}" \
        > "$SB/root/etc/sa02m-homekit/sa02m-homekit.conf"
}

# Run the extracted hk_main in a subshell with sandboxed constants.
# stdout → $SB/out, rc → $RC.
run_trigger() {
    (
        set -euo pipefail
        UNIT=sa02m-homekit.service
        UNIT_FILE=$SB/root/etc/systemd/system/sa02m-homekit.service
        CONF=$SB/root/etc/sa02m-homekit/sa02m-homekit.conf
        VAR_DIR=$SB/root/var/lib/sa02m-homekit
        RUN_DIR=$SB/root/run/sa02m-homekit
        STATUS_FILE=$RUN_DIR/status.json
        SETUP_FILE=$RUN_DIR/setup.json
        LOCK_FILE=$SB/lockfile
        LOCK_WAIT_S=${HK_LOCK_WAIT_S:-5}
        PKG_DIR=$PKG
        # shellcheck source=/dev/null
        . "$FN"
        hk_main "$@"
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
for argv in "bogus" "enable extra" "" "reset_pairing" "ENABLE"; do
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
rm -f "$SB/root/etc/systemd/system/sa02m-homekit.service"
run_trigger enable
if [ "$RC" -ne 0 ] && out_has '"not_installed"' && [ "$(n_calls)" -eq 0 ]; then
    ok "C no unit file ⇒ not_installed, no systemctl"
else
    bad "C no unit file: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi

echo "D. disable"
reset_tree false
printf '{"code":"111-22-333"}\n' > "$SB/root/run/sa02m-homekit/setup.json"
run_trigger disable
total_sysctl=$((total_sysctl + $(n_calls)))
if [ "$RC" -eq 0 ] && [ "$(calls_of stop)" -eq 1 ] && [ "$(calls_of disable)" -eq 1 ] && [ "$(unbounded)" -eq 0 ]; then
    ok "D1 disable: stop + disable, all $(n_calls) systemctl call(s) timeout-bounded"
else
    bad "D1 disable: rc=$RC stop=$(calls_of stop) disable=$(calls_of disable) unbounded=$(unbounded)"
fi
st="$SB/root/run/sa02m-homekit/status.json"
if [ -f "$st" ] && [ ! -L "$st" ] && grep -q '"state":"disabled"' "$st" \
   && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["state"]=="disabled" and d["enabled"] is False' "$st" 2>/dev/null; then
    ok "D2 absent status ⇒ a valid JSON \`disabled\` status written"
else
    bad "D2 no valid \`disabled\` status.json after disable (absent status)"
fi
[ ! -e "$SB/root/run/sa02m-homekit/setup.json" ] && ok "D3 setup.json removed (a stopped daemon cannot withdraw its code)" \
    || bad "D3 setup.json survived disable — a stale pairing code stays readable"
leftover=$(find "$SB/root/run/sa02m-homekit" -name '.status.*' | grep -c .)
[ "$leftover" -eq 0 ] && ok "D4 no temp file left behind" || bad "D4 $leftover temp file(s) left in the run dir"

# fresher file (the daemon's own) is never clobbered
reset_tree false
printf '{"state":"disabled","ts":1,"message":"daemon-own"}\n' > "$st"
touch -d '+1 hour' "$st"
run_trigger disable
grep -q 'daemon-own' "$st" && ok "D5 a fresher status.json is kept (not clobbered)" \
    || bad "D5 a status.json newer than the call was overwritten"

# older file is replaced
reset_tree false
printf '{"state":"running","ts":1,"message":"old"}\n' > "$st"
touch -d '-1 hour' "$st"
run_trigger disable
grep -q '"state":"disabled"' "$st" && ok "D6 an older status.json is replaced by \`disabled\`" \
    || bad "D6 an older status.json was not replaced"

# planted symlinks in the daemon-owned run dir never redirect a root write
reset_tree false
printf 'precious\n' > "$SB/target"
touch -d '-1 hour' "$SB/target"
ln -s "$SB/target" "$st"
ln -s "$SB/target" "$st.tmp"
run_trigger disable
if [ "$(cat "$SB/target")" = precious ] && [ -f "$st" ] && [ ! -L "$st" ]; then
    ok "D7 planted status.json / status.json.tmp symlinks: target untouched, status.json replaced by a regular file"
else
    bad "D7 a planted symlink redirected the root write (target now: $(head -c 60 "$SB/target"))"
fi

# the daemon user owns the run dir, so it can swap a temp NAME for a symlink
# between its creation and root's write; modelled by a `mktemp` that always
# loses that race. Root must never re-open a temp by name (the pre-fix
# `mktemp` + `printf > "$tmp"` + `chmod "$tmp"` wrote and chmod'ed the victim).
reset_tree false
printf 'precious\n' > "$SB/victim"
chmod 0600 "$SB/victim"
HK_REAL_MKTEMP=$(PATH=${PATH#"$SHIM:"} command -v mktemp)
export HK_REAL_MKTEMP
cat > "$SHIM/mktemp" <<'EOF'
#!/usr/bin/env bash
t=$("$HK_REAL_MKTEMP" "$@") || exit 1
rm -f -- "$t"
ln -s "$HK_SB/victim" "$t"
printf '%s\n' "$t"
EOF
chmod +x "$SHIM/mktemp"
run_trigger disable
rm -f -- "$SHIM/mktemp"
if [ "$(cat "$SB/victim")" = precious ] && [ "$(stat -c %a "$SB/victim")" = 600 ] \
   && [ -f "$st" ] && [ ! -L "$st" ] && grep -q '"state":"disabled"' "$st"; then
    ok "D8 a temp name swapped for a symlink never redirects the write: victim bytes/mode untouched, status.json a regular file"
else
    bad "D8 the fallback wrote through a swapped temp name (victim now: $(head -c 60 "$SB/victim"), mode $(stat -c %a "$SB/victim"); status.json regular=$([ -f "$st" ] && [ ! -L "$st" ] && echo yes || echo no))"
fi

# a failing fallback names its cause in the log, never on stdout (the CGI
# parses stdout as JSON): a DIRECTORY at status.json makes the rename fail
# with EISDIR, as root or not. Before the fix the python's stderr went to
# /dev/null and the log line named the dir but not why.
reset_tree false
mkdir "$st"
run_trigger disable
logged=$(grep -F 'cannot write the fallback status' "$SB/logger.log" 2>/dev/null)
out9=$(tr -d '\n' < "$SB/out")
leftover=$(find "$SB/root/run/sa02m-homekit" -name '.status.*' | grep -c .)
case "$logged" in
    *'Is a directory'*) cause9=yes ;;
    *) cause9=no ;;
esac
if [ "$RC" -eq 0 ] && [ "$out9" = '{"ok":true,"action":"disable"}' ] && [ "$cause9" = yes ] \
   && [ "$leftover" -eq 0 ] && [ -d "$st" ]; then
    ok "D9 a failed fallback logs its exception text (EISDIR), stdout stays the JSON answer, no temp left"
else
    bad "D9 failed fallback: rc=$RC out=$out9 cause-logged=$cause9 leftover=$leftover log=$(tr '\n' ' ' < "$SB/logger.log" 2>/dev/null | head -c 300)"
fi

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
hk_c="$SB/root/etc/sa02m-homekit/sa02m-homekit.conf"
{ printf '[other]\nenabled = true\n\n'; cat "$hk_c"; } > "$hk_c.new" && mv "$hk_c.new" "$hk_c"
run_trigger restart
if [ "$RC" -eq 0 ] && [ "$(n_calls)" -eq 0 ] && out_has '"applied":"skipped"'; then
    ok "F3 an \`enabled = true\` outside [bridge] does not count (the daemon reads [bridge] only)"
else
    bad "F3 section-blind enabled read: rc=$RC calls=$(n_calls) out=$(cat "$SB/out")"
fi
reset_tree false
printf '[bridge]\nENABLED = true\ninterface = eth0\nport = 21064\n' > "$hk_c"
run_trigger restart
if [ "$RC" -eq 0 ] && [ "$(calls_of restart)" -eq 1 ] && out_has '"applied":"restart"'; then
    ok "F4 \`ENABLED = true\` counts (configparser lower-cases keys) ⇒ restart"
else
    bad "F4 case-sensitive enabled read: rc=$RC restart=$(calls_of restart) out=$(cat "$SB/out")"
fi

echo "G. reset-pairing"
seed_store() {
    local v="$SB/root/var/lib/sa02m-homekit"
    printf 'keys\n' > "$v/state.json"
    printf '{}\n' > "$v/aids.json"
    printf '{}\n' > "$v/identity.json"
    printf 'torn\n' > "$v/.hk-abc123.tmp"
    printf 'torn\n' > "$v/.hk-zz.tmp"
    mkdir -p "$v/.hk-dir.tmp"
    printf 'code\n' > "$SB/root/run/sa02m-homekit/setup.json"
}
reset_tree true
seed_store
run_trigger reset-pairing
total_sysctl=$((total_sysctl + $(n_calls)))
v="$SB/root/var/lib/sa02m-homekit"
if [ "$RC" -eq 0 ] && [ ! -e "$v/state.json" ] && [ ! -e "$v/.hk-abc123.tmp" ] && [ ! -e "$v/.hk-zz.tmp" ]; then
    ok "G1 state.json and both .hk-*.tmp sidecars removed"
else
    bad "G1 reset-pairing left the pairing store: rc=$RC out=$(cat "$SB/out") ls=$(ls -A "$v" | tr '\n' ' ')"
fi
if [ -f "$v/aids.json" ] && [ -f "$v/identity.json" ] && [ -d "$v/.hk-dir.tmp" ] \
   && [ -f "$SB/root/etc/sa02m-homekit/sa02m-homekit.conf" ]; then
    ok "G2 aids.json, identity.json, the conf and a .hk-*.tmp DIRECTORY untouched (no recursive rm)"
else
    bad "G2 reset-pairing removed more than the pairing store: $(ls -A "$v" | tr '\n' ' ')"
fi
if [ "$(calls_of stop)" -eq 1 ] && [ "$(calls_of start)" -eq 1 ] && [ "$(unbounded)" -eq 0 ] \
   && out_has '"restarted":true' && [ ! -e "$SB/root/run/sa02m-homekit/setup.json" ]; then
    ok "G3 enabled ⇒ stop, delete, start again (bounded); stale setup.json removed"
else
    bad "G3 enabled: stop=$(calls_of stop) start=$(calls_of start) unbounded=$(unbounded) out=$(cat "$SB/out")"
fi
# the order: the delete happens between the stop and the start
order=$(grep -nE '^(stop|start) ' "$CALLS" | cut -d: -f1 | tr '\n' ' ')
case "$order" in "1 "*) ok "G4 stop is the first systemctl call" ;; *) bad "G4 stop is not first: $(cat "$CALLS")" ;; esac

reset_tree false
seed_store
run_trigger reset-pairing
if [ "$RC" -eq 0 ] && [ ! -e "$v/state.json" ] && [ "$(calls_of start)" -eq 0 ] && out_has '"restarted":false'; then
    ok "G5 disabled ⇒ store removed, the unit is NOT started"
else
    bad "G5 disabled: rc=$RC start=$(calls_of start) out=$(cat "$SB/out")"
fi

reset_tree true
seed_store
touch "$SB/active"
run_trigger reset-pairing
if [ "$RC" -ne 0 ] && out_has '"still_running"' && [ -f "$v/state.json" ]; then
    ok "G6 unit still active after the stop ⇒ still_running, nothing deleted"
else
    bad "G6 deleted under a live daemon or wrong answer: rc=$RC out=$(cat "$SB/out") state=$([ -f "$v/state.json" ] && echo kept || echo GONE)"
fi

# `deactivating` (the stop timed out client-side, the daemon still shutting
# down) exits non-zero from is-active exactly like `inactive` — only the
# printed state tells them apart.
reset_tree true
seed_store
printf 'deactivating\n' > "$SB/active"
run_trigger reset-pairing
if [ "$RC" -ne 0 ] && out_has '"still_running"' && [ -f "$v/state.json" ]; then
    ok "G6b unit still deactivating after the stop ⇒ still_running, nothing deleted"
else
    bad "G6b deleted under a daemon still shutting down: rc=$RC out=$(cat "$SB/out") state=$([ -f "$v/state.json" ] && echo kept || echo GONE)"
fi

reset_tree true
mkdir -p "$SB/elsewhere"
printf 'keys\n' > "$SB/elsewhere/state.json"
rm -rf -- "$v"
ln -s "$SB/elsewhere" "$v"
run_trigger reset-pairing
if [ "$RC" -ne 0 ] && out_has '"state_dir_invalid"' && [ -f "$SB/elsewhere/state.json" ] && [ "$(n_calls)" -eq 0 ]; then
    ok "G7 symlinked state dir refused before any systemctl; its target untouched"
else
    bad "G7 symlinked state dir: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi
rm -f -- "$v"
run_trigger reset-pairing
if [ "$RC" -ne 0 ] && out_has '"state_dir_invalid"'; then
    ok "G8 missing state dir refused"
else
    bad "G8 missing state dir: rc=$RC out=$(cat "$SB/out")"
fi

echo "H. lock"
reset_tree true
exec 8<"$SB/lockfile"
flock 8
HK_LOCK_WAIT_S=0 run_trigger enable
flock -u 8
exec 8<&-
if [ "$RC" -ne 0 ] && out_has '"busy"' && [ "$(n_calls)" -eq 0 ]; then
    ok "H a held lock ⇒ busy, no systemctl"
else
    bad "H held lock: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi

echo "I. conf truth-set parity with sa02m_homekit.config"
if [ -f "$PKG/sa02m_homekit/config.py" ]; then
    i_n=0; i_bad=0
    while IFS= read -r sample; do
        [ -n "$sample" ] || continue
        conf="$SB/parity.conf"
        printf '[bridge]\n%b\ninterface = eth0\nport = 21064\n' "$sample" > "$conf"
        py=$(PYTHONPATH="$PKG" python3 -c 'import sys; from sa02m_homekit import config; print("1" if config.load(sys.argv[1]).enabled else "0")' "$conf" 2>/dev/null)
        sh=$( ( CONF=$conf; PKG_DIR=$PKG; . "$FN"; hk_conf_enabled && echo 1 || echo 0 ) 2>/dev/null )
        i_n=$((i_n + 1))
        if [ -z "$py" ] || [ "$py" != "$sh" ]; then
            bad "I sample '$sample': daemon says '${py:-<error>}', helper says '$sh'"
            i_bad=$((i_bad + 1))
        fi
    done <<'SAMPLES'
enabled = true
enabled = false
enabled=TRUE
enabled = yes
enabled = on
enabled = 1
enabled = 0
enabled: true
ENABLED = true
Enabled = true
enabled = true # a comment
enabled = maybe
# enabled = true
enabled =
[other]\nenabled = true
SAMPLES
    [ "$i_n" -ge 15 ] || bad "I only $i_n parity samples ran (non-vacuity floor 15)"
    [ "$i_bad" -eq 0 ] && [ "$i_n" -ge 15 ] && ok "I helper and daemon agree on all $i_n conf samples"
else
    bad "I $PKG/sa02m_homekit/config.py absent — the parity half cannot run"
fi

echo "I2. whole-file parity with sa02m_homekit.config (shapes a line reader gets wrong)"
if [ -f "$PKG/sa02m_homekit/config.py" ]; then
    j_n=0; j_bad=0
    while IFS= read -r sample; do
        [ -n "$sample" ] || continue
        conf="$SB/parity2.conf"
        printf '%b' "$sample" > "$conf"
        py=$(PYTHONPATH="$PKG" python3 -c 'import sys; from sa02m_homekit import config; print("1" if config.load(sys.argv[1]).enabled else "0")' "$conf" 2>/dev/null)
        sh=$( ( CONF=$conf; PKG_DIR=$PKG; . "$FN"; hk_conf_enabled && echo 1 || echo 0 ) 2>/dev/null )
        j_n=$((j_n + 1))
        if [ -z "$py" ] || [ "$py" != "$sh" ]; then
            bad "I2 file '$sample': daemon says '${py:-<error>}', helper says '$sh'"
            j_bad=$((j_bad + 1))
        fi
    done <<'SAMPLES'
[bridge]\r\nenabled = true\r\ninterface = eth0\r\n
[bridge]\nenabled = true\nenabled = false\n
[bridge]\nenabled = true\nENABLED = false\n
[bridge]\nenabled = true\n[bridge]\nport = 21064\n
[bridge]\nenabled = true\ngarbage line\n
[bridge]\nenabled = true\n[other]\ngarbage line\n
[bridge]\nenabled = true\n  continued\n
[DEFAULT]\nenabled = true\n[bridge]\ninterface = eth0\n
[bridge] ; c\nenabled = true\n
enabled = true\n[bridge]\nenabled = true\n
\xef\xbb\xbf[bridge]\nenabled = true\n
[bridge]\nenabled = true\nname = \xff\n
[bridge]\nenabled = true\n
SAMPLES
    [ "$j_n" -ge 13 ] || bad "I2 only $j_n whole-file samples ran (non-vacuity floor 13)"
    [ "$j_bad" -eq 0 ] && [ "$j_n" -ge 13 ] && ok "I2 helper and daemon agree on all $j_n whole-file samples"
else
    bad "I2 $PKG/sa02m_homekit/config.py absent — the parity half cannot run"
fi

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

echo "Z. non-vacuity"
if [ "$total_sysctl" -ge 4 ]; then
    ok "Z $total_sysctl systemctl calls observed across the verb cases (floor 4)"
else
    bad "Z only $total_sysctl systemctl calls observed — the shims saw nothing (vacuous run)"
fi

if [ "$fails" -eq 0 ]; then
    echo "homekit-trigger: all checks passed"
    exit 0
fi
echo "homekit-trigger: $fails FAILURE(S)"
exit 1
