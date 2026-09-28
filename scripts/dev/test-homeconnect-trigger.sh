#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-homeconnect-trigger.sh — behavioural harness for the Home Connect
# card's privileged helper usr/local/sbin/sa02m-homeconnect-web-trigger.sh
# (quality row `homeconnect-trigger`; contract docs/contracts/home-connect.md
# §10; root via etc/sudoers.d/sa02m-homeconnect).
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
#   B  argv: unknown verb, extra argument, empty argv, the HomeKit verb, the
#      action spelling — non-zero, no systemctl
#   C  not installed (no unit file) — non-zero, no systemctl
#   D  disable: stop + disable, all bounded; a pending link.json removed; a
#      link.json SYMLINK removed without its target being touched; no status
#      file written (the dispatch answers `disabled` from the conf)
#   E  enable: unmask + enable + restart, all bounded
#   F  restart: conf enabled ⇒ restart; conf disabled ⇒ `skipped`, no restart;
#      an `enabled = true` OUTSIDE [account] does not count
#   G  unlink: removes EXACTLY tokens.json + .hc-*.tmp (+ link.json);
#      budget.json, appliances.json, the conf and a `.hc-*.tmp` DIRECTORY
#      stay; a tokens.json SYMLINK is removed, its target untouched; starts
#      again only when enabled; refuses a symlinked or missing state dir;
#      refuses to delete while the unit still reports active OR deactivating
#      after the stop (is-active exits non-zero for both)
#   H  lock: a verb while another holds the lock answers `busy`
#   I  conf truth set parity with the daemon: hc_conf_enabled agrees with
#      sa02m_homeconnect.config.load().enabled over a sample matrix
#
# Comment-mutation cases registered with the row (measured RED here):
#   commenting out `rm -f -- "$VAR_DIR/tokens.json" 2>/dev/null || true` → G1 RED;
#   commenting out `rm -f -- "$LINK_FILE" 2>/dev/null || true` (both verbs) →
#   D2 + G3 RED.
#
# Run: bash scripts/dev/test-homeconnect-trigger.sh   (bash, python3, flock)
#      HC_TRIGGER_SRC=<file> judges another copy of the helper (the RED run).
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="${HC_TRIGGER_SRC:-$HERE/usr/local/sbin/sa02m-homeconnect-web-trigger.sh}"
PKG="$HERE/opt/sa02m-homeconnect"

fails=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -r "$SRC" ] || { echo "homeconnect-trigger: FAIL — cannot read $SRC"; exit 1; }
command -v flock >/dev/null 2>&1 || { echo "homeconnect-trigger: FAIL — flock not found (util-linux)"; exit 1; }

SB="$(mktemp -d "${TMPDIR:-/tmp}/hc-trigger.XXXXXX")" || { echo "homeconnect-trigger: FAIL — mktemp"; exit 1; }
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
    echo "homeconnect-trigger: $fails FAILURE(S)"; exit 1
fi
for f in hc_conf_enabled hc_unit_stopped hc_enable hc_disable hc_restart hc_remove_tokens hc_unlink hc_main; do
    grep -q "^$f() {" "$FN" || bad "A2 function $f missing from the extracted block"
done
if ! bash -n "$FN" 2>"$SB/syntax.err"; then
    bad "A3 the extracted block does not parse: $(head -c 200 "$SB/syntax.err")"
fi
[ "$fails" -eq 0 ] && ok "A1-A3 FUNCTIONS block extracted ($n_fn_lines lines, 8 functions) and parses"

# ── shims ───────────────────────────────────────────────────────────────────
# systemctl: records "<verb> <unit> BOUNDED|UNBOUNDED"; `is-active` answers
# like the real one — prints the state, exit 0 only for `active`: the state
# comes from $SB/active (present ⇒ its content, default `active`), else
# `inactive`. A `stop` also records whether tokens.json still existed (the
# delete must come AFTER the stop).
cat > "$SHIM/systemctl" <<'EOF'
#!/usr/bin/env bash
b=UNBOUNDED; [ "${HC_VIA_TIMEOUT:-}" = 1 ] && b=BOUNDED
printf '%s %s\n' "$*" "$b" >> "$HC_CALLS"
case "$1" in
    stop)
        [ -e "$HC_SB/root/var/lib/sa02m-homeconnect/tokens.json" ] && echo "stop-with-tokens" >> "$HC_CALLS.order"
        exit 0 ;;
    is-active)
        st=inactive
        if [ -e "$HC_SB/active" ]; then
            st=$(cat "$HC_SB/active"); [ -n "$st" ] || st=active
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
HC_VIA_TIMEOUT=1 exec "$@"
EOF
printf '#!/usr/bin/env bash\nprintf "logger %%s\\n" "$*" >> "$HC_LOGS"\nexit 0\n' > "$SHIM/logger"
chmod +x "$SHIM"/*
export PATH="$SHIM:$PATH" HC_CALLS="$CALLS" HC_SB="$SB" HC_LOGS="$SB/logger.log"

# Fresh sandbox tree + constants for one case.
reset_tree() {
    rm -rf -- "$SB/root" "$SB/active" "$CALLS" "$CALLS.order" "$SB/logger.log"
    mkdir -p "$SB/root/etc/systemd/system" "$SB/root/etc/sa02m-homeconnect" \
             "$SB/root/var/lib/sa02m-homeconnect" "$SB/root/run/sa02m-homeconnect"
    : > "$SB/root/etc/systemd/system/sa02m-homeconnect.service"
    : > "$CALLS"
    printf '[account]\nenabled = %s\nclient_id = ABCDEFGH12345678\nvendor_client_id = \nhost = api\nlink_requested_at = 0\n\n[control]\nmode = off\n' \
        "${1:-false}" > "$SB/root/etc/sa02m-homeconnect/sa02m-homeconnect.conf"
}

# Run the extracted hc_main in a subshell with sandboxed constants.
# stdout → $SB/out, rc → $RC.
run_trigger() {
    (
        set -euo pipefail
        UNIT=sa02m-homeconnect.service
        UNIT_FILE=$SB/root/etc/systemd/system/sa02m-homeconnect.service
        CONF=$SB/root/etc/sa02m-homeconnect/sa02m-homeconnect.conf
        VAR_DIR=$SB/root/var/lib/sa02m-homeconnect
        RUN_DIR=$SB/root/run/sa02m-homeconnect
        LINK_FILE=$RUN_DIR/link.json
        LOCK_FILE=$SB/lockfile
        LOCK_WAIT_S=${HC_LOCK_WAIT_S:-5}
        # shellcheck source=/dev/null
        . "$FN"
        hc_main "$@"
    ) > "$SB/out" 2>"$SB/err"
    RC=$?
}
: > "$SB/lockfile"

calls_of()  { grep -c "^$1 " "$CALLS" 2>/dev/null; }
n_calls()   { grep -c . "$CALLS" 2>/dev/null; }
unbounded() { grep -c ' UNBOUNDED$' "$CALLS" 2>/dev/null; }
out_has()   { grep -qF -- "$1" "$SB/out"; }
total_sysctl=0
v="$SB/root/var/lib/sa02m-homeconnect"
r="$SB/root/run/sa02m-homeconnect"

echo "B. argv"
for argv in "bogus" "enable extra" "" "reset-pairing" "set_client_id" "UNLINK" "unlink;reboot"; do
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
rm -f "$SB/root/etc/systemd/system/sa02m-homeconnect.service"
run_trigger enable
if [ "$RC" -ne 0 ] && out_has '"not_installed"' && [ "$(n_calls)" -eq 0 ]; then
    ok "C no unit file ⇒ not_installed, no systemctl"
else
    bad "C no unit file: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi

echo "D. disable"
reset_tree false
printf '{"user_code":"ABCD-1234"}\n' > "$r/link.json"
run_trigger disable
total_sysctl=$((total_sysctl + $(n_calls)))
if [ "$RC" -eq 0 ] && [ "$(calls_of stop)" -eq 1 ] && [ "$(calls_of disable)" -eq 1 ] && [ "$(unbounded)" -eq 0 ] \
   && out_has '"action":"disable"'; then
    ok "D1 disable: stop + disable, all $(n_calls) systemctl call(s) timeout-bounded"
else
    bad "D1 disable: rc=$RC stop=$(calls_of stop) disable=$(calls_of disable) unbounded=$(unbounded)"
fi
[ ! -e "$r/link.json" ] && ok "D2 a pending link.json removed (a killed daemon cannot withdraw its sign-in code)" \
    || bad "D2 link.json survived disable — a stale sign-in code stays readable"
[ ! -e "$r/status.json" ] && ok "D3 no status file written by root (the dispatch answers \`disabled\` from the conf)" \
    || bad "D3 disable wrote $(ls "$r") into the daemon-owned run dir"
reset_tree false
printf 'precious\n' > "$SB/victim"
ln -s "$SB/victim" "$r/link.json"
run_trigger disable
if [ "$RC" -eq 0 ] && [ "$(cat "$SB/victim")" = precious ] && [ ! -e "$r/link.json" ] && [ ! -L "$r/link.json" ]; then
    ok "D4 a link.json symlink planted by the daemon user is removed, its target untouched"
else
    bad "D4 planted link.json symlink: rc=$RC victim=$(head -c 40 "$SB/victim") link-left=$([ -L "$r/link.json" ] && echo yes || echo no)"
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
hc_c="$SB/root/etc/sa02m-homeconnect/sa02m-homeconnect.conf"
{ printf '[other]\nenabled = true\n\n'; cat "$hc_c"; } > "$hc_c.new" && mv "$hc_c.new" "$hc_c"
run_trigger restart
if [ "$RC" -eq 0 ] && [ "$(n_calls)" -eq 0 ] && out_has '"applied":"skipped"'; then
    ok "F3 an \`enabled = true\` outside [account] does not count (the daemon reads [account] only)"
else
    bad "F3 section-blind enabled read: rc=$RC calls=$(n_calls) out=$(cat "$SB/out")"
fi

echo "G. unlink"
seed_store() {
    printf '{"access_token":"SECRET","refresh_token":"SECRET"}\n' > "$v/tokens.json"
    printf '{"day":"2026-09-28","used":17}\n' > "$v/budget.json"
    printf '[{"ha_id":"X","device_id":"hc-x"}]\n' > "$v/appliances.json"
    printf 'torn\n' > "$v/.hc-abc123.tmp"
    printf 'torn\n' > "$v/.hc-zz.tmp"
    mkdir -p "$v/.hc-dir.tmp"
    printf '{"user_code":"ABCD-1234"}\n' > "$r/link.json"
}
reset_tree true
seed_store
run_trigger unlink
total_sysctl=$((total_sysctl + $(n_calls)))
if [ "$RC" -eq 0 ] && [ ! -e "$v/tokens.json" ] && [ ! -e "$v/.hc-abc123.tmp" ] && [ ! -e "$v/.hc-zz.tmp" ]; then
    ok "G1 tokens.json and both .hc-*.tmp sidecars removed"
else
    bad "G1 unlink left the sign-in: rc=$RC out=$(cat "$SB/out") ls=$(ls -A "$v" | tr '\n' ' ')"
fi
if [ -f "$v/budget.json" ] && grep -q '"used":17' "$v/budget.json" && [ -f "$v/appliances.json" ] \
   && [ -d "$v/.hc-dir.tmp" ] && [ -f "$SB/root/etc/sa02m-homeconnect/sa02m-homeconnect.conf" ]; then
    ok "G2 budget.json (BSH counts per client+user), appliances.json, the conf and a .hc-*.tmp DIRECTORY untouched"
else
    bad "G2 unlink removed more than the sign-in: $(ls -A "$v" | tr '\n' ' ')"
fi
if [ "$(calls_of stop)" -eq 1 ] && [ "$(calls_of start)" -eq 1 ] && [ "$(unbounded)" -eq 0 ] \
   && out_has '"restarted":true' && [ ! -e "$r/link.json" ]; then
    ok "G3 enabled ⇒ stop, delete, start again (bounded); stale link.json removed"
else
    bad "G3 enabled: stop=$(calls_of stop) start=$(calls_of start) unbounded=$(unbounded) out=$(cat "$SB/out")"
fi
order=$(grep -nE '^(stop|start) ' "$CALLS" | cut -d: -f1 | tr '\n' ' ')
if grep -qx 'stop-with-tokens' "$CALLS.order" 2>/dev/null; then
    case "$order" in "1 "*) ok "G4 the stop is the first systemctl call and ran while tokens.json still existed" ;;
                     *) bad "G4 stop is not first: $(cat "$CALLS")" ;; esac
else
    bad "G4 the tokens were deleted BEFORE the stop — a refresh in flight writes them back"
fi

reset_tree false
seed_store
run_trigger unlink
if [ "$RC" -eq 0 ] && [ ! -e "$v/tokens.json" ] && [ "$(calls_of start)" -eq 0 ] && out_has '"restarted":false'; then
    ok "G5 disabled ⇒ sign-in removed, the unit is NOT started"
else
    bad "G5 disabled: rc=$RC start=$(calls_of start) out=$(cat "$SB/out")"
fi

reset_tree true
seed_store
touch "$SB/active"
run_trigger unlink
if [ "$RC" -ne 0 ] && out_has '"still_running"' && [ -f "$v/tokens.json" ]; then
    ok "G6 unit still active after the stop ⇒ still_running, nothing deleted"
else
    bad "G6 deleted under a live daemon or wrong answer: rc=$RC out=$(cat "$SB/out") tokens=$([ -f "$v/tokens.json" ] && echo kept || echo GONE)"
fi

reset_tree true
seed_store
printf 'deactivating\n' > "$SB/active"
run_trigger unlink
if [ "$RC" -ne 0 ] && out_has '"still_running"' && [ -f "$v/tokens.json" ]; then
    ok "G6b unit still deactivating after the stop ⇒ still_running, nothing deleted"
else
    bad "G6b deleted under a daemon still shutting down: rc=$RC out=$(cat "$SB/out") tokens=$([ -f "$v/tokens.json" ] && echo kept || echo GONE)"
fi

reset_tree true
seed_store
printf 'precious\n' > "$SB/victim"
rm -f "$v/tokens.json"
ln -s "$SB/victim" "$v/tokens.json"
run_trigger unlink
if [ "$RC" -eq 0 ] && [ "$(cat "$SB/victim")" = precious ] && [ ! -L "$v/tokens.json" ] && [ ! -e "$v/tokens.json" ]; then
    ok "G7 a tokens.json symlink is removed (the link, not its target)"
else
    bad "G7 tokens.json symlink: rc=$RC victim=$(head -c 40 "$SB/victim") link-left=$([ -L "$v/tokens.json" ] && echo yes || echo no)"
fi

reset_tree true
mkdir -p "$SB/elsewhere"
printf 'SECRET\n' > "$SB/elsewhere/tokens.json"
rm -rf -- "$v"
ln -s "$SB/elsewhere" "$v"
run_trigger unlink
if [ "$RC" -ne 0 ] && out_has '"state_dir_invalid"' && [ -f "$SB/elsewhere/tokens.json" ] && [ "$(n_calls)" -eq 0 ]; then
    ok "G8 symlinked state dir refused before any systemctl; its target untouched"
else
    bad "G8 symlinked state dir: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi
rm -f -- "$v"
run_trigger unlink
if [ "$RC" -ne 0 ] && out_has '"state_dir_invalid"'; then
    ok "G9 missing state dir refused"
else
    bad "G9 missing state dir: rc=$RC out=$(cat "$SB/out")"
fi

echo "H. lock"
reset_tree true
exec 8<"$SB/lockfile"
flock 8
HC_LOCK_WAIT_S=0 run_trigger enable
flock -u 8
exec 8<&-
if [ "$RC" -ne 0 ] && out_has '"busy"' && [ "$(n_calls)" -eq 0 ]; then
    ok "H a held lock ⇒ busy, no systemctl"
else
    bad "H held lock: rc=$RC out=$(cat "$SB/out") calls=$(n_calls)"
fi

echo "I. conf truth-set parity with sa02m_homeconnect.config"
if [ -f "$PKG/sa02m_homeconnect/config.py" ]; then
    i_n=0; i_bad=0
    while IFS= read -r sample; do
        [ -n "$sample" ] || continue
        conf="$SB/parity.conf"
        printf '[account]\n%b\nclient_id = ABCDEFGH12345678\nhost = api\n\n[control]\nmode = off\n' "$sample" > "$conf"
        py=$(PYTHONPATH="$PKG" python3 -c 'import sys; from sa02m_homeconnect import config; print("1" if config.load(sys.argv[1]).enabled else "0")' "$conf" 2>/dev/null)
        sh=$( ( CONF=$conf; . "$FN"; hc_conf_enabled && echo 1 || echo 0 ) 2>/dev/null )
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
Enabled : On
enabled = true # a comment
enabled = maybe
# enabled = true
enabled =
SAMPLES
    [ "$i_n" -ge 13 ] || bad "I only $i_n parity samples ran (non-vacuity floor 13)"
    [ "$i_bad" -eq 0 ] && [ "$i_n" -ge 13 ] && ok "I helper and daemon agree on all $i_n conf samples"
else
    bad "I $PKG/sa02m_homeconnect/config.py absent — the parity half cannot run"
fi

echo "Z. non-vacuity"
if [ "$total_sysctl" -ge 4 ]; then
    ok "Z $total_sysctl systemctl calls observed across the verb cases (floor 4)"
else
    bad "Z only $total_sysctl systemctl calls observed — the shims saw nothing (vacuous run)"
fi

if [ "$fails" -eq 0 ]; then
    echo "homeconnect-trigger: all checks passed"
    exit 0
fi
echo "homeconnect-trigger: $fails FAILURE(S)"
exit 1
