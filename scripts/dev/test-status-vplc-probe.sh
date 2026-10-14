#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-status-vplc-probe.sh — status.cgi's probe of the vPLC page server
# (`svc_vplc_ui` in the `services` part, docs/contracts/vplc-page-seam.md §4)
# answers active|inactive and starts NO external process.
#
# Why this exists: the probe runs on EVERY board once per services build
# (≤45 s), and most boards carry no vPLC at all. It used to spawn
# `timeout systemctl show … | head | tr` (four processes plus a D-Bus round
# trip) to learn a fact that the filesystem already holds: the unit's
# RuntimeDirectory is removed when it stops (no RuntimeDirectoryPreserve —
# seam condition §3), so "unit file installed AND socket present" is the
# whole truth, readable with three `[ -f ]` and one `[ -S ]`.
#
# Method: fast_service_state, systemd_unit_file_installed and
# status_timeout_run are extracted from the SHIPPED status.cgi by function
# anchor; the three systemd unit directories and the socket path are
# retargeted into a sandbox; the probe then runs with PATH = a shim directory
# ONLY. Every external command the probe may reach is a counting wrapper
# (systemctl and timeout are fakes; head, tr, cat, … forward to the real
# binary), and anything else fails as "command not found" — so the call count
# is the number of processes the probe starts, not a guess.
#
# Cases:
#   1  no unit file, no socket        -> inactive, 0 external commands
#   2  no unit file, socket present   -> inactive, 0 external commands  (AF_UNIX)
#   3  unit file (/lib) + socket      -> active,   0 external commands  (AF_UNIX)
#   4  unit file (/etc), no socket    -> inactive, 0 external commands
#   5  every output is one of active|inactive (the producer enum the
#      consumer, applyVplcNavLink, is tested against in js-unit-vplc-nav)
#   6  the call site `SVC_VPLC_UI=$(fast_service_state vplc-plant-ui)` and the
#      JSON line `"svc_vplc_ui": "${SVC_VPLC_UI:-unknown}"` are live
#      (comment-stripped via lib_check.sh)
# Cases 2 and 3 need a real AF_UNIX socket file, created by python3: on a host
# whose python has no socket.AF_UNIX (Windows CPython) they print SKIP and the
# summary says so — never a pass. WSL / Linux CI run them.
#
# RED, measured on the 1.0.7.1 probe (`status_timeout_run systemctl show -p
# ActiveState …`): cases 1-4 FAIL on the call count (4 external commands each:
# timeout, systemctl, head, tr) while every state was right — the defect was
# the cost, not the answer. Non-vacuous: a failed or over-wide extraction, a
# missing vplc branch, or an un-retargeted path FAILS.
#
# Run: bash scripts/dev/test-status-vplc-probe.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
# shellcheck source=../../.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh

SRC=${STATUS_CGI_SRC:-www/network_config/cgi-bin/status.cgi}
[ -f "$SRC" ] || { echo "FAIL  status.cgi not found: $SRC"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN" "$T/lib" "$T/usrlib" "$T/etc"
SOCKDIR="$T/run/vplc-plant-ui"; SOCK="$SOCKDIR/ui.sock"
CALLS="$T/calls"

fails=0; skips=0; passed=0
ok()   { printf 'ok    %s\n' "$1"; passed=$((passed + 1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; skips=$((skips + 1)); }

# ── extract the probe from the shipped CGI ─────────────────────────────────
EX="$T/probe-fns.sh"
: > "$EX"
extract_fn() {  # $1 = function name
    awk -v fn="$1" '
        $0 ~ "^" fn "\\(\\) \\{" { inb = 1 }
        inb { print }
        inb && /^\}/ { inb = 0; found = 1 }
        END { exit (found ? 0 : 3) }
    ' "$SRC" >> "$EX" || { echo "FAIL  could not extract ${1}() from $SRC (did the file shape change?)"; exit 1; }
    printf '\n' >> "$EX"
}
for fn in status_timeout_run systemd_unit_file_installed fast_service_state; do
    extract_fn "$fn"
done
EXT=$(cat "$EX")
text_has "$EXT" 'vplc-plant-ui|vplc-plant-ui.service)' \
    || { echo "FAIL  fast_service_state has no vplc-plant-ui branch — nothing to test"; exit 1; }
text_matches "$EXT" '^(build_services_json|gather_|load_serial_map_conf)' \
    && { echo "FAIL  the extraction is over-wide — it swallowed code past the probe functions"; exit 1; }

# ── retarget the unit directories and the socket into the sandbox ──────────
sed -i \
    -e "s|for d in /lib/systemd/system /usr/lib/systemd/system /etc/systemd/system;|for d in $T/lib $T/usrlib $T/etc;|" \
    -e "s|/run/vplc-plant-ui/ui.sock|$SOCK|g" \
    "$EX"
EXT=$(cat "$EX")
# Quoted/anchored forms: the sandbox path ends in the real one, so a bare
# substring test would cry wolf on a correct retarget.
for abs in 'for d in /lib/systemd/system' ' /run/vplc-plant-ui/ui.sock'; do
    text_has "$EXT" "$abs" && { echo "FAIL  '$abs' was not retargeted into the sandbox"; exit 1; }
done

# ── shims: the ONLY directory on PATH while the probe runs ──────────────────
cat > "$BIN/systemctl" <<EOF
#!/bin/bash
printf 'systemctl %s\n' "\$*" >> "$CALLS"
s=inactive; [ -r "$T/activestate" ] && IFS= read -r s < "$T/activestate"
printf '%s\n' "\$s"
EOF
cat > "$BIN/timeout" <<EOF
#!/bin/bash
printf 'timeout %s\n' "\$*" >> "$CALLS"
shift
exec "\$@"
EOF
for tool in head tr cat stat ls grep awk sed pgrep pidof ps ss readlink; do
    real=$(command -v "$tool" 2>/dev/null) || continue
    case "$real" in /*) ;; *) continue ;; esac
    cat > "$BIN/$tool" <<EOF
#!/bin/bash
printf '$tool %s\n' "\$*" >> "$CALLS"
exec "$real" "\$@"
EOF
done
chmod +x "$BIN"/*

# shellcheck disable=SC1090
. "$EX"

PROBE_OUT=""; PROBE_ERR=""; PROBE_CALLS=0
run_probe() {  # $1 = ActiveState the fake systemctl reports
    printf '%s\n' "$1" > "$T/activestate"
    : > "$CALLS"
    PROBE_OUT=$(PATH="$BIN" fast_service_state vplc-plant-ui 2>"$T/err")
    PROBE_ERR=$(<"$T/err")
    PROBE_CALLS=0
    local line
    while IFS= read -r line; do [ -n "$line" ] && PROBE_CALLS=$((PROBE_CALLS + 1)); done < "$CALLS"
}
reset_tree() {
    rm -f "$T/lib/vplc-plant-ui.service" "$T/usrlib/vplc-plant-ui.service" "$T/etc/vplc-plant-ui.service"
    rm -rf "$T/run"
}
make_socket() {  # rc 0 = a real AF_UNIX socket exists at $SOCK
    mkdir -p "$SOCKDIR"
    local p
    for p in python3 python py; do
        command -v "$p" >/dev/null 2>&1 || continue
        "$p" -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$SOCK" >/dev/null 2>&1 && break
    done
    [ -S "$SOCK" ]
}
check() {  # $1 = case label  $2 = expected state
    local seen="state=$PROBE_OUT, external commands=$PROBE_CALLS"
    if [ "$PROBE_OUT" = "$2" ] && [ "$PROBE_CALLS" -eq 0 ] && [ -z "$PROBE_ERR" ]; then
        ok "$1 -> $2, 0 external commands"
    else
        bad "$1: want $2 with 0 external commands, got $seen${PROBE_ERR:+, stderr: $PROBE_ERR}"
        [ -s "$CALLS" ] && sed 's/^/        call: /' "$CALLS"
    fi
}
OUTPUTS=""

# 1 — the common board: no vPLC at all
reset_tree
run_probe inactive
check "1 no unit file, no socket" inactive
OUTPUTS="$OUTPUTS $PROBE_OUT"

# 2 — a socket without an installed unit is not a page server
reset_tree
if make_socket; then
    run_probe inactive
    check "2 no unit file, socket present" inactive
    OUTPUTS="$OUTPUTS $PROBE_OUT"
else
    skip "2 no unit file, socket present — no AF_UNIX socket can be made on this host (python without socket.AF_UNIX); WSL / Linux CI run it"
fi

# 3 — the running page server (unit shipped by the deb under /lib)
reset_tree
: > "$T/lib/vplc-plant-ui.service"
if make_socket; then
    run_probe active
    check "3 unit file (/lib) + socket" active
    OUTPUTS="$OUTPUTS $PROBE_OUT"
else
    skip "3 unit file + socket — no AF_UNIX socket can be made on this host (python without socket.AF_UNIX); WSL / Linux CI run it"
fi

# 4 — installed but stopped (or not yet bound): no socket
reset_tree
: > "$T/etc/vplc-plant-ui.service"
run_probe active
check "4 unit file (/etc), no socket" inactive
OUTPUTS="$OUTPUTS $PROBE_OUT"

# 5 — the producer enum
enum_ok=1
for o in $OUTPUTS; do
    case "$o" in active|inactive) ;; *) enum_ok=0 ;; esac
done
if [ -n "${OUTPUTS// /}" ] && [ "$enum_ok" -eq 1 ]; then
    ok "5 every probe output is active|inactive (${OUTPUTS# })"
else
    bad "5 probe outputs outside active|inactive: '${OUTPUTS# }'"
fi

# 6 — the probe is wired into the services part
if stripped_has "$SRC" 'SVC_VPLC_UI=$(fast_service_state vplc-plant-ui)'; then
    ok "6a the services build calls the probe"
else
    bad "6a the call site 'SVC_VPLC_UI=\$(fast_service_state vplc-plant-ui)' is gone from $SRC"
fi
if stripped_has "$SRC" '"svc_vplc_ui": "${SVC_VPLC_UI:-unknown}"'; then
    ok "6b the services JSON emits svc_vplc_ui"
else
    bad "6b the JSON line '\"svc_vplc_ui\": \"\${SVC_VPLC_UI:-unknown}\"' is gone from $SRC"
fi

echo
if [ "$fails" -eq 0 ]; then
    if [ "$skips" -gt 0 ]; then
        echo "status-vplc-probe: ALL OK — $passed case(s) green, $skips SKIPPED (no AF_UNIX on this host; not a pass for those cases)"
    else
        echo "status-vplc-probe: ALL OK — $passed case(s) green"
    fi
    exit 0
fi
echo "status-vplc-probe: $fails FAIL(s)"
exit 1
