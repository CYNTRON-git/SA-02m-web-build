#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-iface-routes-ensure.sh — regression test for the static-route guarantee
# (usr/local/sbin/sa02m-routes-ensure.sh; contract
# docs/contracts/boot-network-routes.md).
#
# Why this exists: the defect it pins is SILENT. ifupdown applies a
# `post-up ip route add …` line only at the end of a successful ifup; an ifup
# that dies on the gateway step (late carrier) never runs it, and a link cycle
# in the recovery ladder flushes it while only the default route is put back.
# The board pings and serves its panel while every client behind the declared
# gateway loses it — bench 1.135, 2026-10-29: 192.168.10.0/24 via
# 192.168.1.100 gone after a reboot, MQTT clients in 192.168.10.x cut off for
# a day. No syntax or static gate can see that, so it is pinned functionally.
#
# Method, in the idiom of test-iface-dns-ensure.sh: copy the SHIPPED helper
# into a scratch tree and sed-retarget its two roots (interfaces.d and
# /run/sa02m-routes-ensure); `ip` is a STATEFUL PATH shim (per-iface address
# files, one routes table, `route add` appends or answers EEXIST, argv recorded
# one `|`-separated line per call so token boundaries are assertable) and
# `logger` a recording shim. Host `ip` is hidden through
# scripts/dev/lib_path_mask.sh for the «ip absent» case. No root, no device,
# no systemd, nothing touches the real system; there is deliberately NO env
# seam in the shipped helper — the retarget is the test's job.
#
# Layers: every case under the shebang shell; R1, R7 and R16 again under a
# strict POSIX shell (dash / busybox sh / a `sh` without BASH_VERSION) where
# one exists — the board runs dash; R1, R6 and R7 again with `awk` → mawk
# where mawk exists — the board's awk, whose regex dialect differs from gawk
# (CI Ubuntu has it). A missing layer prints `skip`, never `ok`. Then the
# coldboot ifup_in_progress gate, extracted and run against a scripted
# systemctl shim, and static floors (comment-stripped through
# .ai-dev/quality/checks/lib_check.sh): the four ladder call sites, the -x
# guards, the coldboot gate, the installer line, and no live eval / sh -c /
# bash -c in the helper.
#
# Non-vacuous: a failed or over-wide retarget, a missing helper, a fixture
# line that stopped being present, an `ip` argv the shim does not model, or a
# shim never invoked FAILS rather than passing on zero matches.
#
# Drive-to-failure recipe (how this harness was proven to be a real ratchet):
#   cp usr/local/sbin/sa02m-routes-ensure.sh /tmp/broken.sh
#   # neuter one guard, e.g. delete the first `show exact` early return in
#   # add_route (R2: a present route is re-added), the awk `default` exclusion
#   # (R4: the installer's own default line is logged as rejected), the
#   # `dev` must-equal check (R7: `dev eth1` in eth0.conf is applied), or the
#   # `.rejected` marker compare (R7b: the reject line repeats every pass),
#   # the r/ro/rou/rout object prefixes (R6, R7: `ip r add` silently lost),
#   # the unknown-option scan (R7: `ip -n blue route add` silently lost), a
#   # `wrapped(...)` call (R7: `sleep 5 && ip route add …` silently lost), or
#   # the `-z "$_sum"` guard (R7c: a failing cksum swallows the reject line)
#   ROUTES_ENSURE_SRC=/tmp/broken.sh bash scripts/dev/test-iface-routes-ensure.sh
# COLDBOOT_SRC does the same for the coldboot gate (e.g. make its `case`
# never match: the gate case FAILS). The overrides never edit the real tree.
#
# Off-board only: the sandbox replaces ip(8) by a shim on PATH; on a board a
# mis-set PATH would reach the real routing table. Do not run it on a board.
#
# Run: bash scripts/dev/test-iface-routes-ensure.sh   (bash + coreutils + awk)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${ROUTES_ENSURE_SRC:-usr/local/sbin/sa02m-routes-ensure.sh}
CONTRACT=docs/contracts/boot-network-routes.md

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$SRC" "$CONTRACT"; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done
# shellcheck source=scripts/dev/lib_path_mask.sh
. scripts/dev/lib_path_mask.sh || { echo "FAIL  scripts/dev/lib_path_mask.sh not loadable"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

CD="$T/ifd"                       # /etc/network/interfaces.d
RUNDIR="$T/rundir"                # parent of STATE_DIR (R16 turns it into a file)
STATE="$RUNDIR/sa02m-routes-ensure"
NET="$T/net"                      # the shim's kernel: addr.<if>, routes
BIN="$T/bin"
OUT="$T/out"                      # stdout+stderr of the last run
CALLS="$T/calls.log"              # reset per case
CALLS_ALL="$T/calls-all.log"      # never reset — the harness non-vacuity witness
UNMODELLED="$T/unmodelled.log"    # argv shapes the ip shim does not model
LOGGED="$T/logger.log"
mkdir -p "$CD" "$RUNDIR" "$NET" "$BIN"
: > "$CALLS_ALL"; : > "$UNMODELLED"

# ── retarget the SHIPPED helper into the sandbox ───────────────────────────
H="$T/sa02m-routes-ensure.sh"
sed -e "s#^IFACE_CONF_DIR=/etc/network/interfaces\.d\$#IFACE_CONF_DIR=$CD#" \
    -e "s#^STATE_DIR=/run/sa02m-routes-ensure\$#STATE_DIR=$STATE#" \
    "$SRC" > "$H"
chmod +x "$H"
for pat in "IFACE_CONF_DIR=$CD" "STATE_DIR=$STATE"; do
    grep -Fqx "$pat" "$H" || { echo "FAIL  retarget missed in routes-ensure: $pat"; exit 1; }
done
grep -Eq '^[A-Z_]+=/(etc|run)/' "$H" && { echo "FAIL  routes-ensure still holds a real /etc or /run root"; exit 1; }

# ── PATH shims ─────────────────────────────────────────────────────────────
# The shims read their state locations from the environment, so the same shim
# serves the bash, the POSIX-shell and the mawk runs.
export RT_NET="$NET" RT_CALLS="$CALLS" RT_CALLS_ALL="$CALLS_ALL" \
       RT_UNMODELLED="$UNMODELLED" RT_LOGGED="$LOGGED"
cat > "$BIN/ip" <<'SHIM'
#!/bin/bash
{ printf 'ip'; for _a in "$@"; do printf '|%s' "$_a"; done; printf '\n'; } \
  | tee -a "$RT_CALLS_ALL" >> "$RT_CALLS"
has() { local f _r; while read -r f _r; do [ "$f" = "$1" ] && return 0; done < "$RT_NET/routes"; return 1; }
if [ "$#" = 6 ] && [ "$1 $2 $3 $4 $5" = "-4 -o addr show dev" ]; then
    if [ -n "${ADDR_ALL:-}" ] || [ -s "$RT_NET/addr.$6" ]; then
        printf '2: %s    inet 192.168.1.135/24 brd 192.168.1.255 scope global %s\n' "$6" "$6"
    fi
    exit 0
fi
if [ "$#" = 5 ] && [ "$1 $2 $3 $4" = "-4 route show exact" ]; then
    while read -r f r; do [ "$f" = "$5" ] && printf '%s %s\n' "$f" "$r"; done < "$RT_NET/routes"
    exit 0
fi
if [ "$#" -ge 6 ] && [ "$1 $2 $3" = "-4 route add" ]; then
    d=$4; shift 4
    if [ -n "${ROUTE_ADD_FAIL:-}" ]; then
        printf 'Error: Nexthop has\001 invalid gateway.\nsecond line of noise\n' >&2; exit 2
    fi
    if [ -n "${ROUTE_RACE:-}" ] && ! has "$d"; then
        printf '%s via 192.168.1.254 dev eth0 proto static\n' "$d" >> "$RT_NET/routes"
    fi
    if has "$d"; then echo 'RTNETLINK answers: File exists' >&2; exit 2; fi
    printf '%s %s\n' "$d" "$*" >> "$RT_NET/routes"
    exit 0
fi
printf '%s\n' "$*" >> "$RT_UNMODELLED"
exit 1
SHIM
cat > "$BIN/logger" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$RT_LOGGED"
exit 0
SHIM
chmod +x "$BIN/ip" "$BIN/logger"
PATH="$BIN:$PATH"; export PATH

RUN_SH=""          # "" = the helper's own shebang; else a POSIX shell to run it under
LAYER=""           # label suffix for the re-run layers
reset() {
    rm -rf "$CD" "$NET" "$RUNDIR"; mkdir -p "$CD" "$NET" "$RUNDIR"
    : > "$NET/routes"; : > "$CALLS"; : > "$LOGGED"
    unset ROUTE_ADD_FAIL ROUTE_RACE ADDR_ALL
}
addr()  { printf 'up\n' > "$NET/addr.$1"; }
# shellcheck disable=SC2086  # RUN_SH is a command prefix: empty, `dash` or `busybox sh`
run()   { $RUN_SH "$H" "$@" > "$OUT" 2>&1; rc=$?; }
# `grep -c` prints 0 AND exits 1 on no match; `|| true` keeps that single 0.
adds()      { grep -c '^ip|-4|route|add|' "$CALLS" 2>/dev/null || true; }
rcalls()    { grep -c '^ip|-4|route|' "$CALLS" 2>/dev/null || true; }
ipcalls()   { grep -c '^ip' "$CALLS" 2>/dev/null || true; }
argv_n()    { grep -cxF "$1" "$CALLS" 2>/dev/null || true; }
nlogs()     { grep -c '' "$LOGGED" 2>/dev/null || true; }
has_route() { grep -q "^$1 " "$NET/routes"; }
stanza() {   # $1 iface — the incident conf's head, without route lines
    printf 'auto %s\niface %s inet static\n    address 192.168.1.135\n' "$1" "$1"
    printf '    netmask 255.255.255.0\n    gateway 192.168.1.1\n'
    printf '    dns-nameservers 77.88.8.8 77.88.8.1\n'
}
conf() {     # $1 iface, then route lines (written with a 4-space indent)
    local i=$1; shift
    { stanza "$i"; for l in "$@"; do printf '    %s\n' "$l"; done; } > "$CD/$i.conf"
    for l in "$@"; do
        grep -qF -- "$l" "$CD/$i.conf" || bad "fixture: line missing from $i.conf: $l"
    done
}
INCIDENT='post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 || true'
R1_ARGV='ip|-4|route|add|192.168.10.0/24|via|192.168.1.100|dev|eth0'

# ═══ cases (functions, so the dialect and awk layers re-run the same code) ═══

case_r1() {
    reset; conf eth0 "$INCIDENT"; addr eth0
    run eth0
    if [ "$rc" = 0 ] && [ ! -s "$OUT" ] && [ "$(argv_n "$R1_ARGV")" = 1 ] && [ "$(adds)" = 1 ] \
       && [ "$(nlogs)" = 1 ] && grep -q '192.168.10.0/24 via 192.168.1.100 dev eth0 added' "$LOGGED" \
       && has_route 192.168.10.0/24; then
        ok "R1$LAYER: the 1.135 incident — a declared route missing from the table is added once, separate argv tokens, one journal line"
    else
        bad "R1$LAYER: incident shape rc=$rc adds=$(adds) logs=$(nlogs) out='$(cat "$OUT")' calls='$(tr '\n' ';' < "$CALLS")'"
    fi
}

case_r6() {
    reset
    conf eth0 \
        'up /sbin/ip -4 route replace 10.0.0.0/8 via 192.168.1.100' \
        'post-up ip route add 172.16.0.0/12 via 192.168.1.100 metric 50 onlink' \
        'post-up ip route add 10.20.0.0/16 via 192.168.1.100 dev $IFACE' \
        'post-up ip route add 10.21.0.0/16 dev ${IFACE} via 192.168.1.100' \
        'post-up ip route add 10.30.0.0/16 via 192.168.1.100 2>/dev/null || /bin/true' \
        'post-up ip route add 10.40.0.0/16 via 192.168.1.100 dev eth0 # plant LAN' \
        'post-up ip r add 10.60.0.0/16 via 192.168.1.100' \
        'post-up ip ro add 10.61.0.0/16 via 192.168.1.100 dev eth0' \
        'up /sbin/ip -f inet rout replace 10.62.0.0/16 via 192.168.1.100'
    printf '\tpost-up ip route add 10.50.0.0/16 via 192.168.1.100\n' >> "$CD/eth0.conf"
    addr eth0
    run eth0
    local miss=""
    for e in 'ip|-4|route|add|10.0.0.0/8|via|192.168.1.100' \
             'ip|-4|route|add|172.16.0.0/12|via|192.168.1.100|metric|50|onlink' \
             'ip|-4|route|add|10.20.0.0/16|via|192.168.1.100|dev|eth0' \
             'ip|-4|route|add|10.21.0.0/16|via|192.168.1.100|dev|eth0' \
             'ip|-4|route|add|10.30.0.0/16|via|192.168.1.100' \
             'ip|-4|route|add|10.40.0.0/16|via|192.168.1.100|dev|eth0' \
             'ip|-4|route|add|10.50.0.0/16|via|192.168.1.100' \
             'ip|-4|route|add|10.60.0.0/16|via|192.168.1.100' \
             'ip|-4|route|add|10.61.0.0/16|via|192.168.1.100|dev|eth0' \
             'ip|-4|route|add|10.62.0.0/16|via|192.168.1.100'; do
        [ "$(argv_n "$e")" = 1 ] || miss="$miss [$e]"
    done
    if [ "$rc" = 0 ] && [ -z "$miss" ] && [ "$(adds)" = 10 ] && [ "$(nlogs)" = 10 ] \
       && ! grep -q 'not understood' "$LOGGED"; then
        ok "R6$LAYER: accepted variants — /sbin/ip -4 replace (no dev), metric+onlink, dev \$IFACE/\${IFACE}, a 2>/dev/null || /bin/true tail, a trailing comment, TAB indent, the iproute2 abbreviations ip r / ip ro / rout and -f inet"
    else
        bad "R6$LAYER: accepted variants rc=$rc adds=$(adds) missing:$miss log='$(tr '\n' ';' < "$LOGGED")'"
    fi

    reset
    { printf 'auto eth0\r\niface eth0 inet static\r\n    address 192.168.1.135\r\n'
      printf '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 || true\r\n'
    } > "$CD/eth0.conf"
    addr eth0
    run eth0
    if [ "$rc" = 0 ] && [ "$(argv_n "$R1_ARGV")" = 1 ] && [ "$(adds)" = 1 ] && ! grep -q $'\r' "$CALLS"; then
        ok "R6$LAYER: a CRLF-delivered conf parses, no CR reaches ip(8) (the recurring CRLF class)"
    else
        bad "R6$LAYER: CRLF conf rc=$rc adds=$(adds) calls='$(tr '\n\r' ';^' < "$CALLS")'"
    fi
}

R7_LINES=(
    'post-up ip route add 10.1.0.0/16 via 192.168.1.100 table 100'
    'post-up ip route add 10.2.0.0/16 via 192.168.1.100 src 1.2.3.4'
    'post-up ip route add 10.3.0.0/16 via 192.168.1.100 dev eth1'
    'post-up ip route add 10.4.0.0/16 dev eth0'
    'post-up ip route add 10.5.0.0/16 via 192.168.1.300'
    'post-up ip route add 192.168.010.0/24 via 192.168.1.100'
    'post-up ip route add 10.6.0.0/33 via 192.168.1.100'
    'post-up ip route add 10.7.0.0/16 via 192.168.1.100 ; reboot'
    'post-up ip route add 10.8.0.0/16 via 192.168.1.100 $(reboot)'
    'post-up ip route add 10.9.0.0/16 via 192.168.1.100 && true'
    'post-up ip route add 10.10.0.0/16 via 192.168.1.100 `reboot`'
    'post-up ip route add 10.11.0.0/16 via 192.168.1.100 *'
    'post-up ip -6 route add 2001:db8::/32 via fe80::1'
    'post-up route add -net 10.12.0.0 netmask 255.255.0.0 gw 192.168.1.100'
    'post-up ip route append 10.13.0.0/16 via 192.168.1.100'
    "$(printf 'post-up ip route add 10.14.0.0/16 via 192.168.1.100\001')"
    'post-up ip route add 10.15.0.0/16 via 192.168.1.100 dev eth0 dev eth0'
    'post-up ip -n blue route add 10.16.0.0/16 via 192.168.1.100'
    'post-up ip -f inet6 r add 2001:db8::/32 via fe80::1'
    'post-up ip r a 10.17.0.0/16 via 192.168.1.100'
    # Route commands wrapped in another command: never applied, but logged.
    'post-up sleep 5 && ip route add 10.18.0.0/16 via 192.168.1.100'
    'post-up /bin/sh -c "ip route add 10.19.0.0/16 via 192.168.1.100"'
    "post-up sh -c 'ip r add 10.23.0.0/16 via 192.168.1.100'"
    'post-up ip link set eth0 up && ip r add 10.20.0.0/16 via 192.168.1.100'
    'post-up ip route del 10.22.0.0/16 2>/dev/null; ip route add 10.22.0.0/16 via 192.168.1.100'
    'post-up sleep 1; /sbin/route add -net 10.21.0.0 netmask 255.255.0.0 gw 192.168.1.100'
)
case_r7() {
    reset; conf eth0 "${R7_LINES[@]}"; addr eth0
    # The `*` fixture: run from a directory where the glob WOULD match a
    # grammar keyword, so a `*` that reached a shell word could turn into one.
    mkdir -p "$T/globcwd"; : > "$T/globcwd/onlink"
    local first; first=$(grep -n 'table 100' "$CD/eth0.conf" | cut -d: -f1)
    # shellcheck disable=SC2086  # RUN_SH is a command prefix, as in run()
    ( cd "$T/globcwd" && $RUN_SH "$H" eth0 > "$OUT" 2>&1 ); rc=$?
    local log; log=$(cat "$LOGGED")
    if [ "$rc" = 0 ] && [ "$(adds)" = 0 ] && [ "$(ipcalls)" = 0 ] && [ "$(nlogs)" = 1 ] \
       && [ "${log#*"${#R7_LINES[@]} route line(s) in eth0.conf not understood"}" != "$log" ] \
       && [ "${log#*"line(s) $first,"}" != "$log" ] \
       && ! grep -Eq 'reboot|table|src|blue|sleep|netmask|\*|\$\(|`|&&' "$LOGGED" \
       && ! grep -q '[[:cntrl:]]' "$LOGGED"; then
        ok "R7$LAYER: ${#R7_LINES[@]} unsupported/hostile lines — nothing applied, ONE journal line with line numbers only (no raw token, no control char)"
    else
        bad "R7$LAYER: rejected set rc=$rc adds=$(adds) ipcalls=$(ipcalls) logs=$(nlogs) log='$log'"
    fi
}

case_r16() {
    reset; conf eth0 "$INCIDENT" 'post-up ip route add 10.1.0.0/16 via 192.168.1.100 table 100'; addr eth0
    rm -rf "$RUNDIR"; : > "$RUNDIR"     # STATE_DIR's parent is a regular file
    export ROUTE_ADD_FAIL=1; run eth0; rc1=$rc; o1=$(cat "$OUT"); unset ROUTE_ADD_FAIL
    run eth0; rc2=$rc; o2=$(cat "$OUT")
    local rej fail add
    rej=$(grep -c 'not understood' "$LOGGED" || true)
    fail=$(grep -c 'could not be added' "$LOGGED" || true)
    add=$(grep -c ' added (ifup' "$LOGGED" || true)
    if [ "$rc1" = 0 ] && [ "$rc2" = 0 ] && [ -z "$o1$o2" ] && [ "$rej" = 2 ] && [ "$fail" = 1 ] \
       && [ "$add" = 1 ] && has_route 192.168.10.0/24; then
        ok "R16$LAYER: unwritable STATE_DIR — markers fail silently, rc=0, the run completes (route added), rejects repeat (toward visibility)"
    else
        bad "R16$LAYER: unwritable STATE_DIR rc=$rc1/$rc2 rej=$rej fail=$fail add=$add out='$o1$o2'"
    fi
}

# ═══ R1-R16 under the shebang shell ════════════════════════════════════════

case_r1

reset; conf eth0 "$INCIDENT"; addr eth0
printf '192.168.10.0/24 via 192.168.1.254 dev eth0 proto static\n' > "$NET/routes"
run eth0
if [ "$rc" = 0 ] && [ "$(adds)" = 0 ] && [ "$(nlogs)" = 0 ] \
   && [ "$(cat "$NET/routes")" = "192.168.10.0/24 via 192.168.1.254 dev eth0 proto static" ]; then
    ok "R2: a route to the prefix via a DIFFERENT gateway is left alone (fill the gap, never fight)"
else
    bad "R2: present route touched: adds=$(adds) logs=$(nlogs) routes='$(tr '\n' ';' < "$NET/routes")'"
fi

reset; conf eth0 "$INCIDENT"; addr eth0
run eth0; run eth0; run eth0
if [ "$rc" = 0 ] && [ "$(adds)" = 1 ] && [ "$(nlogs)" = 1 ]; then
    ok "R3: idempotent under the 30 s ladder — three runs from absent = one add, one journal line"
else
    bad "R3: idempotency adds=$(adds) logs=$(nlogs)"
fi

reset
conf eth0 'post-up ip route add 0.0.0.0/0 via 192.168.1.1 || true' \
          'post-up ip route replace 10.0.0.0/0 via 192.168.1.1' \
          'up ip route add default via 192.168.1.1 dev eth0'
printf 'auto eth1\niface eth1 inet dhcp\n    post-up ip route replace default via 192.168.1.1 dev eth1 metric 100 || true\n' > "$CD/eth1.conf"
addr eth0; addr eth1
run eth0; run eth1
if [ "$rc" = 0 ] && [ "$(rcalls)" = 0 ] && [ "$(nlogs)" = 0 ]; then
    ok "R4: default routes (default, 0.0.0.0/0, any /0, the installer's eth1 line) are silent — owned elsewhere"
else
    bad "R4: a default route line was acted on or logged: rcalls=$(rcalls) logs=$(nlogs) log='$(cat "$LOGGED")'"
fi

reset
conf eth0 'post-up /home/klogic/adjust-eth0 || /bin/true' 'up ip addr add 10.9.9.9/24 dev eth0' \
          'post-up ip link set eth0 mtu 1400' 'post-up ip -s link show' 'up ip -o addr show' \
          'post-up /bin/sh -c "echo up"'
addr eth0
run eth0
if [ "$rc" = 0 ] && [ "$(ipcalls)" = 0 ] && [ "$(nlogs)" = 0 ]; then
    ok "R5: non-route hooks (KLogic adjust, ip addr, ip link, option-prefixed ip -s link / ip -o addr, a wrapped sh -c with no route in it) cost nothing — zero ip(8) calls, zero logs"
else
    bad "R5: a non-route line reached ip(8) or the journal: ipcalls=$(ipcalls) logs=$(nlogs)"
fi

case_r6
case_r7

# R7b — log once per conf content; an edit re-arms it.
run eth0; n1=$(nlogs)
printf '#\n' >> "$CD/eth0.conf"
run eth0; n2=$(nlogs)
if [ "$n1" = 1 ] && [ "$n2" = 2 ] && [ "$(adds)" = 0 ]; then
    ok "R7b: the reject line is logged once per conf content; an edit to the conf logs it again"
else
    bad "R7b: log-once broken: after rerun=$n1 (want 1), after edit=$n2 (want 2)"
fi

# R7c — a failing cksum yields an empty checksum, which equals the empty read
# of an absent marker: the reject line must still be logged (every pass,
# toward visibility), never swallowed. A failing shim stands in for cksum.
BINCK="$T/bin-badcksum"; mkdir -p "$BINCK"
printf '#!/bin/bash\nexit 1\n' > "$BINCK/cksum"; chmod +x "$BINCK/cksum"
reset; conf eth0 'post-up ip route add 10.1.0.0/16 via 192.168.1.100 table 100'; addr eth0
if [ "$(PATH="$BINCK:$PATH" cksum < "$CD/eth0.conf" 2>/dev/null)" != "" ]; then
    bad "harness: the failing cksum shim still prints a checksum — R7c cannot test anything"
else
    PATH="$BINCK:$PATH" "$H" eth0 > "$OUT" 2>&1; rc1=$?
    PATH="$BINCK:$PATH" "$H" eth0 >> "$OUT" 2>&1; rc2=$?
    if [ "$rc1" = 0 ] && [ "$rc2" = 0 ] && [ "$(grep -c 'not understood' "$LOGGED" || true)" = 2 ]; then
        ok "R7c: cksum failing — the reject line is still logged on every pass, never silenced"
    else
        bad "R7c: empty-checksum path rc=$rc1/$rc2 log='$(tr '\n' ';' < "$LOGGED")'"
    fi
fi

reset; conf eth0 "$INCIDENT"
run eth0
if [ "$rc" = 0 ] && [ "$(ipcalls)" = 1 ] && [ "$(argv_n 'ip|-4|-o|addr|show|dev|eth0')" = 1 ] \
   && [ "$(rcalls)" = 0 ] && [ "$(nlogs)" = 0 ]; then
    ok "R8: no IPv4 address yet — only the address probe, no route call, silent (the next pass retries)"
else
    bad "R8: no-address path ipcalls=$(ipcalls) rcalls=$(rcalls) logs=$(nlogs)"
fi

reset; conf eth0 "$INCIDENT"; addr eth0
export ROUTE_ADD_FAIL=1
run eth0; l1=$(nlogs); run eth0; l2=$(nlogs)
unset ROUTE_ADD_FAIL
r9_log=$(head -n 1 "$LOGGED")
mark="$STATE/eth0.fail.192-168-10-0_24"
[ -e "$mark" ] && m1=y || m1=n
run eth0; l3=$(nlogs)
[ -e "$mark" ] && m2=y || m2=n
: > "$NET/routes"; export ROUTE_ADD_FAIL=1; run eth0; l4=$(nlogs); unset ROUTE_ADD_FAIL
if [ "$l1" = 1 ] && [ "$l2" = 1 ] && [ "$l3" = 2 ] && [ "$l4" = 3 ] && [ "$m1" = y ] && [ "$m2" = n ] \
   && [ "${r9_log%could not be added: Error: Nexthop has invalid gateway.}" != "$r9_log" ] \
   && ! grep -q 'second line' "$LOGGED" && ! grep -q '[[:cntrl:]]' "$LOGGED" \
   && sed -n 2p "$LOGGED" | grep -q "added"; then
    ok "R9: a failed add logs once (first error line, control chars stripped); success clears the marker; a later failure logs again"
else
    bad "R9: fail path logs=$l1/$l2/$l3/$l4 marker=$m1/$m2 first='$r9_log'"
fi

reset; conf eth0 "$INCIDENT"; addr eth0
export ROUTE_RACE=1; run eth0; unset ROUTE_RACE
if [ "$rc" = 0 ] && [ "$(nlogs)" = 0 ] && [ "$(adds)" = 1 ] && has_route 192.168.10.0/24 \
   && [ ! -e "$STATE/eth0.fail.192-168-10-0_24" ]; then
    ok "R10: lost the check→add race (EEXIST) — re-check sees the route, no failure log, no marker"
else
    bad "R10: race path logs=$(nlogs) adds=$(adds) log='$(cat "$LOGGED")'"
fi

reset
{ stanza eth0
  printf '    post-up ip route add 10.49.0.0/16 via 192.168.1.100\n'
  printf 'iface eth0:1 inet static\n    address 10.0.0.1\n'
  printf '    post-up ip route add 10.50.0.0/16 via 192.168.1.100\n'
  printf 'auto eth1\n    post-up ip route add 10.51.0.0/16 via 192.168.1.100\n'
  printf 'iface eth0 inet6 static\n    post-up ip route add 10.52.0.0/16 via 192.168.1.100\n'
} > "$CD/eth0.conf"
addr eth0
run eth0
if [ "$rc" = 0 ] && [ "$(adds)" = 1 ] && [ "$(argv_n 'ip|-4|route|add|10.49.0.0/16|via|192.168.1.100')" = 1 ] \
   && [ "$(nlogs)" = 1 ]; then
    ok "R11: stanza scoping — only the 'iface eth0 inet' stanza counts (eth0:1, after 'auto eth1', inet6 ignored)"
else
    bad "R11: stanza scoping adds=$(adds) calls='$(tr '\n' ';' < "$CALLS")'"
fi

reset
long="post-up ip route add 10.60.0.0/16 via 192.168.1.100 # $(printf 'a%.0s' $(seq 1 1100))"
conf eth0 "$long"; addr eth0
run eth0
if [ "$rc" = 0 ] && [ "$(adds)" = 0 ] && grep -q '1 route line(s)' "$LOGGED"; then
    ok "R12: a candidate line over MAX_LINE_LEN is rejected whole"
else
    bad "R12: oversized line adds=$(adds) log='$(cut -c1-200 "$LOGGED")'"
fi
reset
r12=(); for n in $(seq 1 20); do r12+=("post-up ip route add 10.$n.0.0/16 via 192.168.1.100"); done
conf eth0 "${r12[@]}"; addr eth0
run eth0
if [ "$rc" = 0 ] && [ "$(adds)" = 16 ] && [ "$(argv_n 'ip|-4|route|add|10.16.0.0/16|via|192.168.1.100')" = 1 ] \
   && [ "$(argv_n 'ip|-4|route|add|10.17.0.0/16|via|192.168.1.100')" = 0 ] \
   && grep -q '4 route line(s) in eth0.conf not understood' "$LOGGED"; then
    ok "R12: MAX_ROUTES — 20 valid lines = exactly 16 adds, the other 4 reported"
else
    bad "R12: route bound adds=$(adds) log='$(cat "$LOGGED")'"
fi

reset
export ADDR_ALL=1
printf 'iface ../x inet static\n    post-up ip route add 10.70.0.0/16 via 192.168.1.100\n' > "$T/x.conf"
printf 'iface lo inet loopback\n    post-up ip route add 10.71.0.0/16 via 192.168.1.100\n' > "$CD/lo.conf"
printf 'iface abcdefghijklmnop inet static\n    post-up ip route add 10.72.0.0/16 via 192.168.1.100\n' > "$CD/abcdefghijklmnop.conf"
printf 'iface eth0;x inet static\n    post-up ip route add 10.73.0.0/16 via 192.168.1.100\n' > "$CD/eth0;x.conf" 2>/dev/null
r13=""
for a in '../x' 'eth0;x' 'abcdefghijklmnop' 'lo' '-x'; do
    : > "$CALLS"; run "$a"
    { [ "$rc" = 0 ] && [ "$(ipcalls)" = 0 ]; } || r13="$r13 [$a rc=$rc calls=$(ipcalls)]"
done
unset ADDR_ALL
if [ -z "$r13" ] && [ "$(nlogs)" = 0 ]; then
    ok "R13: ../x, eth0;x, a 16-char name, lo and -x are refused before any path or ip(8) call"
else
    bad "R13: argument hygiene:$r13 logs=$(nlogs)"
fi
reset
conf eth0 "$INCIDENT"
printf 'auto eth1\niface eth1 inet static\n    address 10.0.0.2\n    post-up ip route add 10.80.0.0/16 via 10.0.0.1 dev eth1\n' > "$CD/eth1.conf"
addr eth0; addr eth1
run
if [ "$rc" = 0 ] && [ "$(adds)" = 2 ] && [ "$(argv_n "$R1_ARGV")" = 1 ] \
   && [ "$(argv_n 'ip|-4|route|add|10.80.0.0/16|via|10.0.0.1|dev|eth1')" = 1 ]; then
    ok "R13: no argument = --all — every *.conf in interfaces.d is processed (two confs, two adds)"
else
    bad "R13: --all adds=$(adds) calls='$(tr '\n' ';' < "$CALLS")'"
fi

# R14 — ip absent. The logger shim stays live; a host ip in /usr/bin or /bin is
# hidden, so «not installed» is this harness's call on every host.
BIN2="$T/bin-noip"; mkdir -p "$BIN2"
command cp "$BIN/logger" "$BIN2/"
NOIP_PATH="$BIN2:$(PATH=/usr/bin:/bin path_without "$T/hostmask" ip)"
reset; conf eth0 "$INCIDENT"; addr eth0
if PATH="$NOIP_PATH" command -v ip >/dev/null 2>&1; then
    bad "harness: ip is reachable in the 'absent' PATH — R14 cannot test anything"
else
    out=$(PATH="$NOIP_PATH" "$H" eth0 2>&1); rc=$?
    if [ "$rc" = 0 ] && [ -z "$out" ] && [ "$(nlogs)" = 0 ]; then
        ok "R14: ip(8) absent = clean silent no-op, exit 0"
    else
        bad "R14: ip-absent path rc=$rc out='$out' log='$(cat "$LOGGED")'"
    fi
fi

reset
export ADDR_ALL=1; run eth0; unset ADDR_ALL
if [ "$rc" = 0 ] && [ "$(ipcalls)" = 0 ] && [ "$(nlogs)" = 0 ]; then
    ok "R15: no conf for the interface = no ip(8) call at all"
else
    bad "R15: absent conf ipcalls=$(ipcalls) logs=$(nlogs)"
fi

case_r16

# ═══ dialect layer — the board's /bin/sh is dash ═══════════════════════════
# `sh` is NOT assumed to be POSIX: on some dev hosts /usr/bin/sh IS bash.
POSIX_SH=""
for cand in dash busybox_sh sh; do
    case "$cand" in
        busybox_sh) command -v busybox >/dev/null 2>&1 && POSIX_SH="busybox sh" ;;
        *) command -v "$cand" >/dev/null 2>&1 && POSIX_SH="$cand" ;;
    esac
    [ -n "$POSIX_SH" ] || continue
    if [ -z "$($POSIX_SH -c 'echo ${BASH_VERSION:-}' 2>/dev/null)" ]; then break; fi
    POSIX_SH=""
done
if [ -z "$POSIX_SH" ]; then
    printf 'skip  dialect: no strict POSIX shell here (sh is bash) — CI/Linux runs R1, R7, R16 under dash\n'
else
    RUN_SH=$POSIX_SH; LAYER=" [$POSIX_SH]"
    case_r1; case_r7; case_r16
    RUN_SH=""; LAYER=""
fi

# ═══ awk layer — the board's awk is mawk ═══════════════════════════════════
if command -v mawk >/dev/null 2>&1; then
    AWKBIN="$T/bin-mawk"; mkdir -p "$AWKBIN"
    printf '#!/bin/sh\nexec %s "$@"\n' "$(command -v mawk)" > "$AWKBIN/awk"
    chmod +x "$AWKBIN/awk"
    if [ "$(PATH="$AWKBIN:$PATH" awk -W version 2>&1 | head -n 1 | grep -c -i mawk)" = 1 ]; then
        OLDPATH=$PATH; PATH="$AWKBIN:$PATH"; LAYER=" [mawk]"
        case_r1; case_r6; case_r7
        PATH=$OLDPATH; LAYER=""
    else
        bad "awk-dialect: the awk → mawk shim does not answer as mawk — the layer would be vacuous"
    fi
else
    printf 'skip  awk-dialect: no mawk here (CI Ubuntu has it)\n'
fi

# ═══ coldboot: no route add while ifupdown is still bringing the iface up ═══
# The SHIPPED ifup_in_progress is extracted and run (under the POSIX shell when
# there is one) against a scripted systemctl shim: an add racing a mid-run
# ifup@<if> would make that ifup fail on EEXIST at its own post-up line.
COLD=${COLDBOOT_SRC:-usr/local/sbin/sa02m-eth-coldboot.sh}
BINSC="$T/bin-sc"; mkdir -p "$BINSC"
cat > "$BINSC/systemctl" <<'SHIM'
#!/bin/bash
# systemctl show -p ActiveState --value <unit>: answer from $SC_<unit-ish>
case "${5:-}" in
    networking.service) printf '%s\n' "${SC_NET:-active}" ;;
    ifup@*.service)     printf '%s\n' "${SC_IFUP:-inactive}" ;;
esac
exit 0
SHIM
chmod +x "$BINSC/systemctl"
sed -n '/^ifup_in_progress() {/,/^}$/p' "$COLD" > "$T/ifup_in_progress.sh"
if ! grep -q '^ifup_in_progress() {' "$T/ifup_in_progress.sh" || ! grep -q 'systemctl show' "$T/ifup_in_progress.sh"; then
    bad "coldboot: ifup_in_progress could not be extracted from $COLD"
else
    gate() {   # $1 SC_NET, $2 SC_IFUP -> prints the function's status
        SC_NET=$1 SC_IFUP=$2 PATH="$BINSC:$PATH" ${POSIX_SH:-sh} -c \
            '. "$1"; ifup_in_progress eth0; echo $?' sh "$T/ifup_in_progress.sh" 2>&1
    }
    g1=$(gate active activating); g2=$(gate activating inactive)
    g3=$(gate active active);     g4=$(gate inactive inactive); g5=$(gate active deactivating)
    if [ "$g1" = 0 ] && [ "$g2" = 0 ] && [ "$g3" = 1 ] && [ "$g4" = 1 ] && [ "$g5" = 0 ]; then
        ok "coldboot: the route belt waits while networking / ifup@<if> is (de)activating, runs once both have settled"
    else
        bad "coldboot: ifup_in_progress answered ifup-activating=$g1 net-activating=$g2 settled=$g3 down=$g4 deactivating=$g5 (want 0 0 1 1 0)"
    fi
fi

# ═══ static floors — the ladder wiring and the helper's no-exec rule ═══════
# COMMENT-STRIPPED through lib_check.sh: a `#` in front of a call site leaves
# its text in the file, and a raw grep would count it as live.
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && declare -F stripped_count >/dev/null; then
    if [ "$(stripped_count etc/fix-eth.sh 'routes_ensure "\$iface"')" -ge 2 ] \
       && [ "$(stripped_count usr/local/sbin/sa02m-eth-coldboot.sh 'routes_ensure "\$IFACE"')" -ge 2 ]; then
        ok "wiring: the ladder calls the helper on both fix-eth and both coldboot paths"
    else
        bad "wiring: a recovery-ladder call site is missing (or commented out)"
    fi
    if stripped_has etc/fix-eth.sh '[ -x "$ROUTES_ENSURE_BIN" ] || return 0' \
       && stripped_has usr/local/sbin/sa02m-eth-coldboot.sh '[ -x "$ROUTES_ENSURE_BIN" ] || return 0' \
       && stripped_has etc/fix-eth.sh 'timeout 10 "$ROUTES_ENSURE_BIN"' \
       && stripped_has usr/local/sbin/sa02m-eth-coldboot.sh 'timeout 10 "$ROUTES_ENSURE_BIN"' \
       && stripped_has usr/local/sbin/sa02m-eth-coldboot.sh 'ifup_in_progress "$1" && return 0'; then
        ok "wiring: both wrappers guard on -x (a partial deploy degrades to today) and bound the call with timeout 10; coldboot's waits out a running ifup"
    else
        bad "wiring: a wrapper lost its -x guard, its timeout, or (coldboot) its ifup_in_progress gate"
    fi
    if stripped_matches scripts/02-network.sh 'sa02m_atomic_install -m 755 "\$ETC_DIR/\.\./usr/local/sbin/sa02m-routes-ensure\.sh"'; then
        ok "wiring: the installer provisions the helper 0755"
    else
        bad "wiring: scripts/02-network.sh does not install sa02m-routes-ensure.sh with -m 755"
    fi
    n_exec=$(stripped_count "$SRC" '(^|[^A-Za-z0-9_])eval([[:space:]]|$)|(^|[^A-Za-z0-9_])(ba)?sh -c')
    if [ "$n_exec" = 0 ]; then
        ok "security: the helper has no live eval / sh -c / bash -c — a conf line is never executed"
    else
        bad "security: the helper carries $n_exec live eval / sh -c / bash -c line(s)"
    fi
else
    bad "wiring: .ai-dev/quality/checks/lib_check.sh could not be sourced — the ladder call sites were NOT verified (a skip is not a pass)"
fi

# ── harness non-vacuity ────────────────────────────────────────────────────
[ -s "$CALLS_ALL" ] || bad "harness: no shim was ever invoked — the cases are not exercising the helper"
if [ -s "$UNMODELLED" ]; then
    bad "harness: the helper called ip(8) with an argv the shim does not model: $(tr '\n' ';' < "$UNMODELLED")"
fi

printf '\n'
if [ "$fails" = 0 ]; then
    echo "PASS  test-iface-routes-ensure.sh"
    exit 0
fi
echo "FAIL  test-iface-routes-ensure.sh: $fails failing case(s)"
exit 1
