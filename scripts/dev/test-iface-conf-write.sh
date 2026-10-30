#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-iface-conf-write.sh — functional regression for the CONTENT guard of the
# root write helper etc/sa02m-iface-conf-write.sh (contract
# docs/contracts/ethernet-iface-naming.md §5.4), specifically its third exec
# form: a hand-added static-route line is carried through a panel save ONLY when
# it is in the strict route grammar AND already present byte-identically in the
# current live destination file — and for apply.cgi's answer when that helper
# refuses the canonical conf: the WHOLE save is refused (Operator, 2026-10-30).
#
# Why this exists: bench 1.135 carries `post-up ip route add 192.168.10.0/24 via
# 192.168.1.100 dev eth0` in eth0.conf. apply.cgi merges that foreign line
# verbatim and hands the conf to this helper, whose content guard admitted only
# two enumerated hooks — so EVERY panel network save on such a board was
# refused and the old conf stayed. The fix must not become a new capability: a
# www-data session can call the helper directly, so a route line it INVENTS
# must still be refused. Both halves are pinned here by running the helper.
#
# Method: copy the SHIPPED helper into a scratch tree and sed-retarget its two
# roots (/etc/network/interfaces.d/ in the literal `case` allow-list, and the
# install log); the copy is then proven to differ from the original ONLY in
# those paths (the reverse substitution reproduces it byte-for-byte) and to hold
# no real root. No root, no device, no sudo; nothing touches the real system.
# test-iface-conf-merge.sh stubs this helper and so cannot reach its guard;
# sudoers-pin-contract drives the two original hook forms but not this one.
#
# Cases:
#   W1  route line present in the current dst -> written, dst byte-identical to
#       stdin; W1b every accepted grammar variant carried together.
#   W2  same line NOT in the current dst (new)       -> exit 2, dst unchanged.
#   W3  dst absent (first write) + route line        -> exit 2, nothing written.
#   W4  present line, stdin differs by one token / whitespace -> exit 2.
#   W5  present but NOT strict grammar (wrapped, default, /0, net-tools, -6,
#       del, `;`, `$(`, backtick, quotes, table, redirection, another port's
#       dev, …) -> exit 2 even though present.
#   W6  klogic hook + installer default-route hook still admitted with no
#       current file; `pre-up /bin/sh …` still refused even when present.
#   W7  `.sa02m-bak` target unaffected (no content guard).
#   W8  CRLF current file: the present line still matches.
#   W9  current file over the 200-line / 32 KiB bounds preserves nothing, but a
#       route-free write is not refused for that alone.
#   W10 presence is read from $dst only, never from a sibling conf.
#   W11 `dev` is the destination's own name: `dev end0` carried in end0.conf,
#       refused in eth0.conf. W11b a legacy end0.conf also takes its canonical
#       `dev eth0` (the one widening over §3), never `dev eth1` / `dev end1`.
#       W11c the same for end1.conf: `dev eth1` carried, `dev eth0` / `dev end0`
#       refused (the second port of an SA-02m-2).
#   W12 every C0 control byte but TAB (NUL included) and DEL refused by the
#       control-char guard; the helper source carries no producer piped into an
#       early-exit grep/head (pipefail shape (f) — a guard that fails open).
#   W13–W17 apply.cgi end to end (method at that block): a refused canonical
#       write answers {"ok":false,"error":"conf_write_refused"}, writes no
#       mirror, applies nothing (W13 dual-conf = review F1, W14 single name,
#       W15 eth1 DHCP); the 1.135 save (W16) and a clean dual-conf save (W17)
#       still apply.
#   W18a review r2 R2-1: a `dev eth0` route in BOTH confs of the dual-conf
#       window — both written, end0 bounced (no split save).
#   W18b a mirror refusal W18a's widening does not cover (end0.conf over the
#       32 KiB snapshot bound): eth0.conf written, NO bounce, the answer
#       {"ok":true,"applied":false,"reason":"reboot_required","status":"applied"}.
#
# Non-vacuous: a missing helper/apply.cgi/lib, a failed or over-wide retarget,
# a helper that never logs, or an unloadable lib_check.sh FAILS rather than
# passing on zero matches.
#
# RED proof (IFACE_CONF_WRITE_SRC / APPLY_CGI_SRC never edit the real tree):
#   git show origin/main:etc/sa02m-iface-conf-write.sh > /tmp/old.sh
#   IFACE_CONF_WRITE_SRC=/tmp/old.sh bash scripts/dev/test-iface-conf-write.sh
#   -> W1, W1b, W8, W11, W11b, W11c, W12, W16, W18a, W18b FAIL (the two-form
#   guard refuses every route line, and its `tr | grep -q` control guard misses
#   NUL and CR on git-bash). Each mutation of the CURRENT helper, measured
#   2026-10-30 (lists as printed, nothing inferred):
#     `line_in_current` dropped               -> W2 W3 W4 W9 W10 W18b
#     CR strip as a literal `$'\r'` in the
#       array append (bash 5.2 leaves it)     -> W8
#     `_rdev` widened to all four LAN names   -> W5 W11 W11b W11c
#     no end0->eth0 / end1->eth1 widening     -> W11b W11c W18a
#     only the `end1)` widening line commented-> W11c
#     control guard as `tr -d '\t' < … |
#       grep -q '[[:cntrl:]]'`                -> W12 (the shape pin; on git-bash
#                                                also NUL and CR)
#   APPLY_CGI_SRC=<git show origin/main:www/network_config/cgi-bin/apply.cgi>
#   -> W13, W14, W15, W18b FAIL (302 «applied»; W13 rewrites and bounces
#   end0.conf). The mirror status unchecked (round-2 apply.cgi) -> W18b only.
#
# Run: bash scripts/dev/test-iface-conf-write.sh   (bash + coreutils + sed + awk)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${IFACE_CONF_WRITE_SRC:-etc/sa02m-iface-conf-write.sh}
APPLY_SRC=${APPLY_CGI_SRC:-www/network_config/cgi-bin/apply.cgi}
CGI_LIBDIR=www/network_config/cgi-bin

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

[ -s "$SRC" ] || { echo "FAIL  missing or empty: $SRC"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
IFD="$T/ifd"
LOGF="$T/install.log"
mkdir -p "$IFD"

# ── retarget the SHIPPED helper into the sandbox ───────────────────────────
H="$T/helper.sh"
sed -e "s#/etc/network/interfaces\.d/#$IFD/#g" \
    -e "s#/var/log/sa02m_install\.log#$LOGF#g" \
    "$SRC" > "$H"
if grep -Eq '/etc/network/interfaces\.d/|/var/log/sa02m_install\.log' "$H"; then
    echo "FAIL  retarget left a real root in the helper copy"; exit 1
fi
n_ret=$(grep -cF "$IFD/" "$H")
[ "$n_ret" -ge 8 ] || { echo "FAIL  retarget hit $n_ret allow-list lines, expected >= 8"; exit 1; }
grep -qF "LOG=\"$LOGF\"" "$H" || { echo "FAIL  retarget missed the LOG path"; exit 1; }
if ! sed -e "s#$IFD/#/etc/network/interfaces.d/#g" -e "s#$LOGF#/var/log/sa02m_install.log#g" "$H" \
     | cmp -s - "$SRC"; then
    echo "FAIL  the helper copy differs from $SRC beyond the retargeted paths"; exit 1
fi

# ── fixtures ───────────────────────────────────────────────────────────────
STANZA=('auto eth0' 'iface eth0 inet static' '    address 192.168.1.135'
        '    netmask 255.255.255.0' '    gateway 192.168.1.1')
STANZA2=('auto eth0' 'iface eth0 inet static' '    address 192.168.1.136'
         '    netmask 255.255.255.0' '    gateway 192.168.1.1')
ROUTE='    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0'

cur()  { local f="$IFD/$1"; shift; printf '%s\n' "$@" > "$f"; }      # current live file
feed() {                                                              # stdin -> helper; sets rc
    local d="$1"; shift
    printf '%s\n' "$@" > "$T/in"
    bash "$H" "$IFD/$d" < "$T/in" > /dev/null 2>&1; rc=$?
}
snap()      { cp "$IFD/$1" "$T/before" 2>/dev/null || rm -f "$T/before"; }
unchanged() { cmp -s "$IFD/$1" "$T/before"; }
written()   { cmp -s "$IFD/$1" "$T/in"; }
reset()     { rm -rf "$IFD"; mkdir -p "$IFD"; : > "$LOGF"; }

# ── W1: the 1.135 shape — a present route line is carried, byte-exact ──────
reset; cur eth0.conf "${STANZA[@]}" "$ROUTE"
feed eth0.conf "${STANZA2[@]}" "$ROUTE"
if [ "$rc" = 0 ] && written eth0.conf && grep -q 'wrote: .*eth0.conf' "$LOGF"; then
    ok "W1: a route line already in eth0.conf is carried through a save (dst == stdin, logged 'wrote:')"
else
    bad "W1: present route line refused or mangled rc=$rc log='$(tr '\n' ';' < "$LOGF")'"
fi

# W1b: every accepted grammar variant, all present, carried together.
VARIANTS=(
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 || true'
    '    up /sbin/ip -4 route replace 10.0.0.0/8 via 192.168.1.100 metric 50 onlink || /bin/true'
    '    post-up ip r add 172.16.0.0/12 via 192.168.1.100'
    '    post-up /usr/sbin/ip -f inet rout add 10.20.0.0/16 via 192.168.1.100 dev $IFACE'
    '    post-up /bin/ip ro replace 10.21.0.0/16 via 192.168.1.100 dev ${IFACE} metric 0'
    '    post-up /usr/bin/ip rou add 10.23.0.0/16 via 192.168.1.100 dev eth0 onlink'
    $'\tpost-up ip route add 10.22.0.0/16 via 192.168.1.100'
    '    post-up ip route add 192.168.50.7 via 192.168.1.100   '
    '    post-up ip route add 0.0.0.0 via 192.168.1.100'
)
reset; cur eth0.conf "${STANZA[@]}" "${VARIANTS[@]}"
feed eth0.conf "${STANZA2[@]}" "${VARIANTS[@]}"
if [ "$rc" = 0 ] && written eth0.conf; then
    ok "W1b: ${#VARIANTS[@]} accepted variants (|| true, /sbin/ip -4 replace metric onlink, ip r, -f inet, \$IFACE, \${IFACE}, metric 0, TAB indent, host route) carried"
else
    bad "W1b: an accepted route variant was refused rc=$rc log='$(tr '\n' ';' < "$LOGF")'"
fi

# ── W2: a NEW route line (absent from the current dst) is refused ──────────
reset; cur eth0.conf "${STANZA[@]}"; snap eth0.conf
feed eth0.conf "${STANZA2[@]}" "$ROUTE"
if [ "$rc" = 2 ] && unchanged eth0.conf && grep -q 'REFUSED (line not allow-listed)' "$LOGF"; then
    ok "W2: a route line NOT in the current eth0.conf is refused (exit 2, dst unchanged, refusal logged)"
else
    bad "W2: a new route line was not refused rc=$rc"
fi

# ── W3: first write (no current dst) with a route line is refused ──────────
reset
feed eth0.conf "${STANZA[@]}" "$ROUTE"
if [ "$rc" = 2 ] && [ ! -e "$IFD/eth0.conf" ]; then
    ok "W3: dst absent + route line -> exit 2, nothing written"
else
    bad "W3: a route line was admitted with no current file rc=$rc"
fi

# ── W4: the present line differs from stdin by one token / whitespace ──────
W4_LINES=(
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.101 dev eth0'
    '    post-up ip route add 192.168.11.0/24 via 192.168.1.100 dev eth0'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 metric 5'
    'post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 '
)
w4_bad=0
for l in "${W4_LINES[@]}"; do
    reset; cur eth0.conf "${STANZA[@]}" "$ROUTE"; snap eth0.conf
    feed eth0.conf "${STANZA2[@]}" "$l"
    if [ "$rc" != 2 ] || ! unchanged eth0.conf; then
        bad "W4: variant of a present line admitted rc=$rc: [$l]"; w4_bad=1
    fi
done
[ "$w4_bad" = 0 ] && ok "W4: ${#W4_LINES[@]} one-token/whitespace variants of a present line refused (other via, other dst, extra metric, indent, trailing space)"

# ── W5: present but NOT strict grammar -> refused anyway ───────────────────
W5_LINES=(
    '    post-up sleep 5 && ip route add 192.168.10.0/24 via 192.168.1.100'
    '    post-up ip route add default via 192.168.1.100'
    '    post-up ip route replace default via 192.168.1.100 dev eth0'
    '    post-up ip route add 0.0.0.0/0 via 192.168.1.100'
    '    post-up ip route add 10.0.0.0/0 via 192.168.1.100'
    '    post-up /bin/sh -c "ip route add 192.168.10.0/24 via 192.168.1.100"'
    '    post-up route add -net 192.168.10.0/24 gw 192.168.1.100'
    '    post-up ip -6 route add 2001:db8::/32 via fe80::1'
    '    post-up ip -f inet6 route add 2001:db8::/32 via fe80::1'
    '    post-up ip route del 192.168.10.0/24 via 192.168.1.100'
    '    post-up ip route append 192.168.10.0/24 via 192.168.1.100'
    '    post-up ip r a 192.168.10.0/24 via 192.168.1.100'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100; reboot'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 && reboot'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 || reboot'
    '    post-up ip route add 192.168.10.0/24 via $(reboot)'
    '    post-up ip route add 192.168.10.0/24 via `reboot`'
    "    post-up ip route add '192.168.10.0/24' via 192.168.1.100"
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 table 100'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 src 192.168.1.135'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 >/dev/null 2>&1 || true'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 # comment'
    '    post-up ip route add 192.168.10.0/24 dev eth0'
    '    post-up ip route add 192.168.010.0/24 via 192.168.1.100'
    '    post-up ip route add 192.168.10.0/33 via 192.168.1.100'
    '    post-up ip -n blue route add 192.168.10.0/24 via 192.168.1.100'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev wlan0'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev end0'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth1 || true'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 dev eth0'
    '    pre-up ip route add 192.168.10.0/24 via 192.168.1.100'
    '    down ip route add 192.168.10.0/24 via 192.168.1.100'
    '    post-up ip route add 192.168.10.0/24 via 192.168.1.100 onlink dev eth0'
)
w5_bad=0
for l in "${W5_LINES[@]}"; do
    reset; cur eth0.conf "${STANZA[@]}" "$l"; snap eth0.conf
    grep -qxF -- "$l" "$IFD/eth0.conf" || { bad "W5 fixture: line not present in the current file: [$l]"; w5_bad=1; continue; }
    feed eth0.conf "${STANZA2[@]}" "$l"
    if [ "$rc" != 2 ] || ! unchanged eth0.conf; then
        bad "W5: a present but non-strict line was admitted rc=$rc: [$l]"; w5_bad=1
    fi
done
[ "$w5_bad" = 0 ] && ok "W5: ${#W5_LINES[@]} present-but-non-strict lines refused (wrapped, default, /0, sh -c, net-tools, -6, del/append, ip r a, ; && || \$( backtick quotes, table/src, redirection, comment, no via, leading zero, /33, -n netns, wlan0 / another LAN port's dev (end0, eth1 in eth0.conf) / repeated dev, pre-up/down, out-of-order)"

# ── W6: the two original hook forms unchanged; pre-up shell still refused ──
reset
feed eth0.conf 'allow-hotplug eth0' 'iface eth0 inet static' '    address 192.168.0.136' \
    '    netmask 255.255.255.0' '    post-up /home/klogic/adjust-eth0 || /bin/true'
rc_k=$rc
feed eth1.conf 'allow-hotplug eth1' 'iface eth1 inet dhcp' '    metric 100' \
    '    post-up ip route replace default via 192.168.1.1 dev eth1 metric 100 || true'
rc_d=$rc
PRE='    pre-up /bin/sh -c "cp /bin/bash /tmp/r; chmod 4755 /tmp/r"'
cur end0.conf "${STANZA[@]}" "$PRE"; snap end0.conf
feed end0.conf "${STANZA2[@]}" "$PRE"
rc_p=$rc
if [ "$rc_k" = 0 ] && [ "$rc_d" = 0 ] && [ "$rc_p" = 2 ] && unchanged end0.conf; then
    ok "W6: klogic hook and installer default-route hook still admitted (no current file); a present pre-up /bin/sh still refused"
else
    bad "W6: original forms changed — klogic rc=$rc_k default-route rc=$rc_d pre-up rc=$rc_p"
fi

# ── W7: the .sa02m-bak target has no content guard ─────────────────────────
reset
feed eth0.conf.sa02m-bak "${STANZA[@]}" "$ROUTE"
if [ "$rc" = 0 ] && written eth0.conf.sa02m-bak; then
    ok "W7: .sa02m-bak backup written verbatim with a route line (no content guard, unchanged)"
else
    bad "W7: the .sa02m-bak write changed behaviour rc=$rc"
fi

# ── W8: a CRLF current file still matches the present line ─────────────────
reset
printf '%s\r\n' "${STANZA[@]}" "$ROUTE" > "$IFD/eth0.conf"
# Count CRs with tr, not `grep -q $'\r$'`: MSYS grep answers that one "no match"
# on a CRLF file (measured), so the fixture check would lie on Windows.
n_cr=$(tr -cd '\r' < "$IFD/eth0.conf" | wc -c)
[ "$n_cr" -eq $(( ${#STANZA[@]} + 1 )) ] || bad "W8 fixture: the current file carries $n_cr CRs, expected one per line"
feed eth0.conf "${STANZA2[@]}" "$ROUTE"
if [ "$rc" = 0 ] && written eth0.conf; then
    ok "W8: CRLF current file — the present route line (trailing CR stripped) is carried; output is LF"
else
    bad "W8: a CRLF current file broke the presence match rc=$rc"
fi

# ── W9: an over-bounds current file preserves nothing; plain writes still pass
reset
{ printf '%s\n' "${STANZA[@]}" "$ROUTE"; for i in $(seq 1 200); do printf '# pad %s\n' "$i"; done; } > "$IFD/eth0.conf"
snap eth0.conf
feed eth0.conf "${STANZA2[@]}" "$ROUTE"; rc_l=$rc; unchanged eth0.conf; un_l=$?
feed eth0.conf "${STANZA2[@]}"; rc_lp=$rc
reset
{ printf '%s\n' "${STANZA[@]}" "$ROUTE"; printf '# %s\n' "$(head -c 33000 /dev/zero | tr '\0' x)"; } > "$IFD/eth0.conf"
snap eth0.conf
feed eth0.conf "${STANZA2[@]}" "$ROUTE"; rc_b=$rc; unchanged eth0.conf; un_b=$?
feed eth0.conf "${STANZA2[@]}"; rc_bp=$rc
if [ "$rc_l" = 2 ] && [ "$un_l" = 0 ] && [ "$rc_lp" = 0 ] \
   && [ "$rc_b" = 2 ] && [ "$un_b" = 0 ] && [ "$rc_bp" = 0 ] && written eth0.conf; then
    ok "W9: a current file over 200 lines / 32 KiB preserves no route line (exit 2), yet a route-free save still writes"
else
    bad "W9: bounds — lines: route rc=$rc_l plain rc=$rc_lp; bytes: route rc=$rc_b plain rc=$rc_bp"
fi

# ── W10: presence is per-destination, never a sibling conf ─────────────────
reset; cur eth1.conf "${STANZA[@]}" "$ROUTE"; cur eth0.conf "${STANZA[@]}"; snap eth0.conf
feed eth0.conf "${STANZA2[@]}" "$ROUTE"
if [ "$rc" = 2 ] && unchanged eth0.conf; then
    ok "W10: a route line present only in eth1.conf is NOT carried into eth0.conf"
else
    bad "W10: presence leaked across confs rc=$rc"
fi

# ── W11 (A2): `dev` is the destination's OWN name — end0 in end0.conf ──────
R_END0='    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev end0 || true'
STANZA_END0=('allow-hotplug end0' 'iface end0 inet static' '    address 192.168.1.135' '    netmask 255.255.255.0')
reset; cur end0.conf "${STANZA_END0[@]}" "$R_END0"
feed end0.conf "${STANZA_END0[@]}" "$R_END0"; rc_own=$rc
reset; cur eth0.conf "${STANZA[@]}" "$R_END0"; snap eth0.conf
feed eth0.conf "${STANZA2[@]}" "$R_END0"; rc_foreign=$rc
if [ "$rc_own" = 0 ] && [ "$rc_foreign" = 2 ] && unchanged eth0.conf; then
    ok "W11: a present 'dev end0' route is carried in end0.conf and refused in eth0.conf (dev = the destination's own iface)"
else
    bad "W11: dev narrowing — own-name rc=$rc_own (want 0), foreign-name rc=$rc_foreign (want 2)"
fi
# W11b (A4): a legacy conf also takes its CANONICAL sibling — end0.conf carries
# a present `dev eth0`, but still not `dev eth1` / `dev end1`.
R_DEV_ETH0='    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 || true'
reset; cur end0.conf "${STANZA_END0[@]}" "$R_DEV_ETH0"
feed end0.conf "${STANZA_END0[@]}" "$R_DEV_ETH0"; rc_sib=$rc
w11b_bad=0
for d in eth1 end1; do
    l="    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev $d || true"
    reset; cur end0.conf "${STANZA_END0[@]}" "$l"; snap end0.conf
    feed end0.conf "${STANZA_END0[@]}" "$l"
    { [ "$rc" = 2 ] && unchanged end0.conf; } || { bad "W11b: end0.conf admitted a present 'dev $d' (rc=$rc)"; w11b_bad=1; }
done
if [ "$rc_sib" = 0 ] && [ "$w11b_bad" = 0 ]; then
    ok "W11b: end0.conf carries a present 'dev eth0' (its canonical sibling) and still refuses 'dev eth1' / 'dev end1'"
elif [ "$rc_sib" != 0 ]; then
    bad "W11b: end0.conf refused a present 'dev eth0' (rc=$rc_sib) — the mirror would refuse what the canonical conf admits"
fi
# W11c (A4, the eth1 half): end1.conf — the second port of an SA-02m-2 in its
# own dual-conf window — carries a present `dev eth1`, never `dev eth0`/`dev end0`.
STANZA_END1=('allow-hotplug end1' 'iface end1 inet static' '    address 192.168.2.135' '    netmask 255.255.255.0')
R_DEV_ETH1='    post-up ip route add 10.10.0.0/16 via 192.168.2.1 dev eth1 || true'
reset; cur end1.conf "${STANZA_END1[@]}" "$R_DEV_ETH1"
feed end1.conf "${STANZA_END1[@]}" "$R_DEV_ETH1"; rc_sib1=$rc
w11c_sib=0; { [ "$rc_sib1" = 0 ] && written end1.conf; } || w11c_sib=1
[ "$w11c_sib" = 1 ] && bad "W11c: end1.conf refused a present 'dev eth1' (rc=$rc_sib1) — the eth1 mirror would refuse what eth1.conf admits"
w11c_bad=0
for d in eth0 end0; do
    l="    post-up ip route add 10.10.0.0/16 via 192.168.2.1 dev $d || true"
    reset; cur end1.conf "${STANZA_END1[@]}" "$l"; snap end1.conf
    feed end1.conf "${STANZA_END1[@]}" "$l"
    { [ "$rc" = 2 ] && unchanged end1.conf; } || { bad "W11c: end1.conf admitted a present 'dev $d' (rc=$rc)"; w11c_bad=1; }
done
[ "$w11c_sib" = 0 ] && [ "$w11c_bad" = 0 ] \
    && ok "W11c: end1.conf carries a present 'dev eth1' (its canonical sibling) and still refuses 'dev eth0' / 'dev end0'"

# ── W12 (A3): the control-char guard — every C0 control but TAB, and DEL ───
# Each byte goes into an otherwise valid conf, so only that guard can refuse it.
w12_bad=0; n_ctl=0
for oct in 000 001 010 013 014 015 033 037 177; do
    reset; n_ctl=$((n_ctl + 1))
    printf "auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask 255.255.255.0\n    gateway 192.168.1.1\\${oct}\n" > "$T/in"
    bash "$H" "$IFD/eth0.conf" < "$T/in" > /dev/null 2>&1; rc=$?
    if [ "$rc" != 2 ] || [ -e "$IFD/eth0.conf" ] || ! grep -q 'REFUSED (control char in content)' "$LOGF"; then
        bad "W12: control byte \\$oct not refused by the control-char guard (rc=$rc)"; w12_bad=1
    fi
done
reset
printf 'auto eth0\niface eth0 inet static\n\taddress 192.168.1.136\n\tnetmask 255.255.255.0\n' > "$T/in"
bash "$H" "$IFD/eth0.conf" < "$T/in" > /dev/null 2>&1; rc_tab=$?
[ "$rc_tab" = 0 ] && written eth0.conf || { bad "W12: a TAB-indented conf was refused (rc=$rc_tab)"; w12_bad=1; }
# Shape (f), pinned on the helper's own source (comment-stripped): no producer
# piped into an early-exit consumer. RED on the pre-A3 helper's
# `tr -d '\t' < … | grep -q '[[:cntrl:]]'`.
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
if . .ai-dev/quality/checks/lib_check.sh 2>/dev/null && command -v stripped_matches >/dev/null; then
    if stripped_matches "$SRC" '[|][[:space:]]*(LC_ALL=[^[:space:]]+[[:space:]]+)?(grep[[:space:]][^|]*-[A-Za-z]*[qlm]|head([[:space:]]|$))'; then
        bad "W12: the helper pipes a producer into an early-exit grep/head (pipefail shape (f) — a guard that can fail open)"; w12_bad=1
    fi
else
    bad "W12: .ai-dev/quality/checks/lib_check.sh not loadable — the shape-(f) pin cannot run"; w12_bad=1
fi
[ "$w12_bad" = 0 ] && ok "W12: $n_ctl control bytes (NUL, SOH, BS, VT, FF, CR, ESC, US, DEL) refused by the control-char guard; TAB admitted; no early-exit pipe in the helper"

# ═══ W13–W17 (A1): apply.cgi end to end — a refused canonical write stops the
# whole save («Отказ целиком», Operator 2026-10-30). The SHIPPED apply.cgi runs
# with the SHIPPED helper above (via a sudo shim), real lib_web_validate.sh, a
# retargeted lib_net_iface.sh, /sys/class/net as sandbox dirs; auth/CSRF and
# lib_rtc.sh are stubbed (their own rows gate them) and nohup only records the
# live-apply it would launch. ══════════════════════════════════════════════
E="$T/e2e"; ECGI="$E/cgi"; ESYS="$E/sys"; EBIN="$E/bin"
mkdir -p "$ECGI" "$ESYS" "$EBIN"
for f in "$APPLY_SRC" "$CGI_LIBDIR/lib_web_validate.sh" "$CGI_LIBDIR/lib_net_iface.sh"; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done
sed -e "s#/sys/class/net/#$ESYS/#g" \
    -e "s#/var/log/sa02m_install\.log#$LOGF#g" \
    -e "s#/usr/local/sbin/sa02m-iface-conf-write\.sh#$H#g" \
    "$APPLY_SRC" > "$ECGI/apply.cgi"
if ! sed -e "s#$H#/usr/local/sbin/sa02m-iface-conf-write.sh#g" -e "s#$ESYS/#/sys/class/net/#g" \
         -e "s#$LOGF#/var/log/sa02m_install.log#g" "$ECGI/apply.cgi" | cmp -s - "$APPLY_SRC"; then
    echo "FAIL  the apply.cgi copy differs from $APPLY_SRC beyond the retargeted paths"; exit 1
fi
for pat in "$H" "$ESYS/" "$LOGF"; do
    grep -qF "$pat" "$ECGI/apply.cgi" || { echo "FAIL  apply.cgi retarget missed: $pat"; exit 1; }
done
grep -Eq '/sys/class/net/|/var/log/sa02m_install\.log|/usr/local/sbin/sa02m-iface-conf-write\.sh' "$ECGI/apply.cgi" \
    && { echo "FAIL  apply.cgi copy still holds a real root"; exit 1; }
sed -e "s#/sys/class/net/#$ESYS/#g" -e "s#/etc/network/interfaces\.d/#$IFD/#g" \
    "$CGI_LIBDIR/lib_net_iface.sh" > "$ECGI/lib_net_iface.sh"
grep -Eq '/sys/class/net/|/etc/network/' "$ECGI/lib_net_iface.sh" \
    && { echo "FAIL  lib_net_iface.sh copy still holds a real root"; exit 1; }
cp "$CGI_LIBDIR/lib_web_validate.sh" "$ECGI/lib_web_validate.sh"
printf '%s\n' 'web_session_check_cookie() { return 0; }' 'web_csrf_require() { :; }' > "$ECGI/lib_web_auth.sh"
printf '%s\n' 'sync_rtc_from_system() { :; }' > "$ECGI/lib_rtc.sh"

export E2E_HELPER="$H" E2E_SUDO_LOG="$E/sudo.log" E2E_NOHUP_LOG="$E/nohup.log"
cat > "$EBIN/sudo" <<'SHIM'
#!/bin/bash
printf '%s\n' "$*" >> "$E2E_SUDO_LOG"
[ "${1:-}" = "-n" ] && shift
if [ "${1:-}" = "$E2E_HELPER" ]; then shift; exec bash "$E2E_HELPER" "$@"; fi
exit 1   # any other root helper: never run a host binary
SHIM
cat > "$EBIN/nohup" <<'SHIM'
#!/bin/bash
printf '%s %s\n' "${@: -2}" >> "$E2E_NOHUP_LOG"   # <iface> <mode> of the live-apply
exit 0
SHIM
chmod +x "$EBIN/sudo" "$EBIN/nohup"

E_CANON_ROUTE='    post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 || true'
e_reset() {   # $@ = live ifaces under /sys/class/net
    reset; rm -rf "$ESYS"; mkdir -p "$ESYS"; : > "$E2E_SUDO_LOG"; : > "$E2E_NOHUP_LOG"
    local i; for i in "$@"; do mkdir -p "$ESYS/$i"; done
}
e_post() {    # $1 = urlencoded form body; sets rc, leaves stdout in $E/out
    printf '%s' "$1" > "$E/body"
    REQUEST_METHOD=POST CONTENT_LENGTH=${#1} PATH="$EBIN:$PATH" \
        bash "$ECGI/apply.cgi" < "$E/body" > "$E/out" 2> "$E/err"; rc=$?
}
e_snap()   { local f; for f in "$@"; do cp "$IFD/$f" "$E/snap.$f"; done; }
e_same()   { local f; for f in "$@"; do cmp -s "$IFD/$f" "$E/snap.$f" || return 1; done; }
# The live-apply is launched in the background (`nohup … &`) and can land after
# the CGI exits: poll for its record ($1 = tenths of a second to wait).
e_wait_apply() { local i=0; while [ ! -s "$E2E_NOHUP_LOG" ] && [ "$i" -lt "$1" ]; do sleep 0.1; i=$((i + 1)); done; }
e_refused() {   # the JSON refusal, nothing applied, no 302
    e_wait_apply 10
    grep -q '"ok":false,"error":"conf_write_refused"' "$E/out" && ! grep -qi '^Location:' "$E/out" \
        && [ ! -s "$E2E_NOHUP_LOG" ] && grep -q 'network save NOT applied' "$LOGF"
}
e_applied() {   # $1 = iface the live-apply must target
    e_wait_apply 50
    grep -qx 'Location: /?status=applied' "$E/out" && grep -qx "$1 up" "$E2E_NOHUP_LOG"
}
POST_ETH0='net_iface=eth0&eth0_enable=1&ip=192.168.1.200&netmask=255.255.255.0&gateway=192.168.1.1'

# W13 — review F1: dual-conf window, a hand-added `dev end0` route. The canonical
# eth0.conf cannot carry it (not present there; dev is not eth0) — before A1 the
# legacy mirror end0.conf was STILL rewritten with the new address and bounced
# live, while eth0.conf (what survives the rename) kept the old one.
e_reset end0
cur eth0.conf "${STANZA[@]}" "$E_CANON_ROUTE"
cur end0.conf "${STANZA_END0[@]}" '    gateway 192.168.1.1' "$R_END0"
e_snap eth0.conf end0.conf
e_post "$POST_ETH0"
if [ "$rc" = 0 ] && e_refused && e_same eth0.conf end0.conf; then
    ok "W13: dual-conf, canonical write refused -> JSON conf_write_refused, eth0.conf AND end0.conf unchanged, no live apply"
else
    bad "W13: dual-conf split save — out='$(tr '\n' '|' < "$E/out")' apply='$(tr '\n' ';' < "$E2E_NOHUP_LOG")' end0.conf changed=$(e_same end0.conf && echo no || echo YES)"
fi

# W14 — single-name board, a foreign hook the helper never admits: the save
# used to answer «applied» and bounce eth0 on the OLD conf.
e_reset eth0
cur eth0.conf "${STANZA[@]}" '    post-up /usr/local/bin/my-routes.sh'
e_snap eth0.conf
e_post "$POST_ETH0"
if [ "$rc" = 0 ] && e_refused && e_same eth0.conf; then
    ok "W14: refused canonical write on a single-name board -> JSON refusal, eth0.conf unchanged, no live apply"
else
    bad "W14: refused write still answered/applied — out='$(tr '\n' '|' < "$E/out")' apply='$(tr '\n' ';' < "$E2E_NOHUP_LOG")'"
fi

# W15 — eth1 DHCP branch: the refusal also skips the dhclient-hook ensure.
e_reset eth1
cur eth1.conf 'allow-hotplug eth1' 'iface eth1 inet dhcp' '    metric 100' '    post-up /usr/local/bin/my-routes.sh'
e_snap eth1.conf
e_post 'net_iface=eth1&eth1_enable=0'
if [ "$rc" = 0 ] && e_refused && e_same eth1.conf && ! grep -q 'sa02m-ensure-eth1-dhcp-hook' "$E2E_SUDO_LOG"; then
    ok "W15: eth1 DHCP save with a refused write -> JSON refusal, eth1.conf unchanged, no dhcp-hook ensure, no live apply"
else
    bad "W15: eth1 refusal — out='$(tr '\n' '|' < "$E/out")' sudo='$(tr '\n' ';' < "$E2E_SUDO_LOG")'"
fi

# W16 — the 1.135 shape end to end (control): the route line is carried, the
# save answers «applied» and bounces eth0.
e_reset eth0
cur eth0.conf "${STANZA[@]}" "$E_CANON_ROUTE"
e_post "$POST_ETH0"
if [ "$rc" = 0 ] && e_applied eth0 && grep -qx '    address 192.168.1.200' "$IFD/eth0.conf" \
   && grep -qxF -- "$E_CANON_ROUTE" "$IFD/eth0.conf"; then
    ok "W16: 1.135 shape — save applied, eth0.conf has the new address AND the carried route line, eth0 bounced"
else
    bad "W16: the legitimate save failed — out='$(tr '\n' '|' < "$E/out")' apply='$(tr '\n' ';' < "$E2E_NOHUP_LOG")'"
fi

# W17 — dual-conf with an admissible content (control): both confs written,
# the live legacy iface bounced — A1 must not break the mirror itself.
e_reset end0
cur eth0.conf "${STANZA[@]}"
cur end0.conf "${STANZA_END0[@]}" '    gateway 192.168.1.1'
e_post "$POST_ETH0"
if [ "$rc" = 0 ] && e_applied end0 && grep -qx '    address 192.168.1.200' "$IFD/eth0.conf" \
   && grep -qx '    address 192.168.1.200' "$IFD/end0.conf" && grep -qx 'allow-hotplug end0' "$IFD/end0.conf"; then
    ok "W17: dual-conf save with admissible content — eth0.conf and the end0.conf mirror both written, end0 bounced"
else
    bad "W17: the dual-conf mirror broke — out='$(tr '\n' '|' < "$E/out")' apply='$(tr '\n' ';' < "$E2E_NOHUP_LOG")'"
fi

# W18a (A4) — review r2 R2-1, verbatim: in the dual-conf window the hand-added
# route reads `dev eth0` in BOTH confs (the installer copied end0.conf with
# s/\bend0\b/eth0/). The canonical eth0.conf admits it; the end0.conf mirror
# must too, or the save splits: eth0.conf written, the live end0 bounced on its
# OLD conf, the new address appearing only at the rename reboot.
e_reset end0
cur eth0.conf "${STANZA[@]}" "$E_CANON_ROUTE"
cur end0.conf "${STANZA_END0[@]}" '    gateway 192.168.1.1' "$E_CANON_ROUTE"
e_post "$POST_ETH0"
if [ "$rc" = 0 ] && e_applied end0 \
   && grep -qx '    address 192.168.1.200' "$IFD/eth0.conf" && grep -qxF -- "$E_CANON_ROUTE" "$IFD/eth0.conf" \
   && grep -qx '    address 192.168.1.200' "$IFD/end0.conf" && grep -qxF -- "$E_CANON_ROUTE" "$IFD/end0.conf"; then
    ok "W18a: dual-conf, a 'dev eth0' route in both confs — BOTH written with the new address and the route, end0 bounced (no split)"
else
    bad "W18a: dual-conf 'dev eth0' route split the save — out='$(tr '\n' '|' < "$E/out")' apply='$(tr '\n' ';' < "$E2E_NOHUP_LOG")' end0.conf=$(grep -c '192.168.1.200' "$IFD/end0.conf")"
fi

# W18b (A5) — a mirror refusal A4 does not cover: the live end0.conf is over the
# helper's 32 KiB snapshot bound (a hand-pasted 33 KB comment — dropped by the
# merge, which preserves only lines <= 512 bytes), so the mirror cannot prove
# the route line present and refuses it, while the small canonical eth0.conf
# admits it. Then: no bounce (it would re-apply the OLD end0.conf), and the
# answer is the honest «saved, applies after reboot» JSON, never the 302.
e_reset end0
cur eth0.conf "${STANZA[@]}" "$E_CANON_ROUTE"
{ printf '%s\n' "${STANZA_END0[@]}" '    gateway 192.168.1.1' "$E_CANON_ROUTE"
  printf '# %s\n' "$(head -c 33000 /dev/zero | tr '\0' x)"; } > "$IFD/end0.conf"
e_snap end0.conf
e_post "$POST_ETH0"
e_wait_apply 10
if [ "$rc" = 0 ] && grep -qx '{"ok":true,"applied":false,"reason":"reboot_required","status":"applied"}' "$E/out" \
   && ! grep -qi '^Location:' "$E/out" && [ ! -s "$E2E_NOHUP_LOG" ] && e_same end0.conf \
   && grep -qx '    address 192.168.1.200' "$IFD/eth0.conf" && grep -qxF -- "$E_CANON_ROUTE" "$IFD/eth0.conf" \
   && grep -q 'mirror .*end0.conf write refused' "$LOGF"; then
    ok "W18b: canonical written, mirror refused -> {ok:true,applied:false,reason:reboot_required}, no 302, no live apply, end0.conf unchanged, logged"
else
    bad "W18b: refused mirror after a written canonical — out='$(tr '\n' '|' < "$E/out")' apply='$(tr '\n' ';' < "$E2E_NOHUP_LOG")' end0.conf changed=$(e_same end0.conf && echo no || echo YES)"
fi

echo
if [ "$fails" = 0 ]; then
    echo "test-iface-conf-write: all cases passed"
    exit 0
fi
echo "test-iface-conf-write: $fails case(s) FAILED"
exit 1
