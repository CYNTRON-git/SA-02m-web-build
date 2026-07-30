#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-subnet-validate.sh — regression test for the gateway-in-subnet
# validators in cgi-bin/lib_web_validate.sh (ipv4_to_int, netmask_is_contiguous,
# same_ipv4_subnet).
#
# Why this exists: the subnet math is pure Bash inside a CGI lib, so no other
# runner in this repo reaches it. A future edit could silently re-open the
# footgun this closes (an out-of-subnet default gateway → an unroutable
# `default via <foreign-gw>` route that dead-routes the board) with every
# syntax/static gate still green. This test pins the logic.
#
# Method: the lib is standalone-sourceable — source it and call the helpers
# directly. Nothing touches the real system.
#
# Run: bash scripts/dev/test-subnet-validate.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# shellcheck source=www/network_config/cgi-bin/lib_web_validate.sh
. www/network_config/cgi-bin/lib_web_validate.sh

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# gw_in_subnet <ip> <mask> <gw> — mirrors apply.cgi's guarded order: a bad mask
# rejects, then subnet membership. Returns 0 iff the pair would be accepted.
gw_in_subnet() {
    netmask_is_contiguous "$2" || return 1
    same_ipv4_subnet "$1" "$3" "$2"
}

# expect_pass <label> <ip> <mask> <gw>
expect_pass() {
    if gw_in_subnet "$2" "$3" "$4"; then ok "$1"; else bad "$1 (expected pass, got reject)"; fi
}
# expect_reject <label> <ip> <mask> <gw>
expect_reject() {
    if gw_in_subnet "$2" "$3" "$4"; then bad "$1 (expected reject, got pass)"; else ok "$1"; fi
}

# ── ipv4_to_int base-10 forcing (the octal trap) ───────────────────────────
[ "$(ipv4_to_int 0.0.0.0)" = "0" ]           && ok "ipv4_to_int 0.0.0.0 = 0"          || bad "ipv4_to_int 0.0.0.0"
[ "$(ipv4_to_int 255.255.255.255)" = "4294967295" ] && ok "ipv4_to_int 255.255.255.255" || bad "ipv4_to_int 255.255.255.255"
[ "$(ipv4_to_int 192.168.1.1)" = "3232235777" ] && ok "ipv4_to_int 192.168.1.1"        || bad "ipv4_to_int 192.168.1.1"
# 192.168.010.5 with octal-style octet: 10#010 == decimal 10, NOT octal 8.
[ "$(ipv4_to_int 192.168.010.5)" = "$(ipv4_to_int 192.168.10.5)" ] \
    && ok "ipv4_to_int 010 octet forced base-10" || bad "ipv4_to_int 010 octet (octal leak)"

# ── netmask_is_contiguous ──────────────────────────────────────────────────
netmask_is_contiguous 255.255.255.0   && ok "contiguous /24"   || bad "contiguous /24"
netmask_is_contiguous 255.255.0.0     && ok "contiguous /16"   || bad "contiguous /16"
netmask_is_contiguous 255.255.255.128 && ok "contiguous /25"   || bad "contiguous /25"
netmask_is_contiguous 255.255.255.255 && ok "contiguous /32"   || bad "contiguous /32"
netmask_is_contiguous 255.255.0.255   && bad "non-contiguous mask accepted" || ok "reject 255.255.0.255"
netmask_is_contiguous 0.0.0.0         && bad "zero mask /0 accepted"        || ok "reject 0.0.0.0"

# ── same_ipv4_subnet / full guarded pipeline ───────────────────────────────
expect_pass   "in-subnet /24"                192.168.1.135 255.255.255.0   192.168.1.1
expect_reject "customer bug: gw /24 foreign" 192.168.1.135 255.255.255.0   192.168.10.1
expect_pass   "in-subnet /16"                10.0.5.7      255.255.0.0      10.0.99.1
expect_reject "out-of-subnet /16"            10.0.5.7      255.255.0.0      10.1.0.1
expect_pass   "in-subnet /25"                192.168.1.10  255.255.255.128  192.168.1.100
expect_reject "out /25 (other half)"         192.168.1.10  255.255.255.128  192.168.1.200
expect_reject "malformed mask"               192.168.1.10  255.255.0.255    192.168.1.1
expect_reject "zero mask /0"                 192.168.1.10  0.0.0.0          8.8.8.8
# leading-zero octet must be read decimal (10# in ipv4_to_int): 192.168.010.5
# is 192.168.10.5, in the same /24 as gateway 192.168.10.1.
expect_pass   "leading-zero octet (10#)"     192.168.010.5 255.255.255.0    192.168.10.1

# ── empty-gateway guard shape in apply.cgi (the caller, not the helper) ─────
# The helper never sees an empty gateway — apply.cgi wraps the check in
# `[ -n "$GATEWAY" ]`. Assert both interface guards exist so empty stays allowed.
APPLY=www/network_config/cgi-bin/apply.cgi
grep -q 'if \[ -n "\$GATEWAY" \]; then'      "$APPLY" && ok "apply.cgi eth0 empty-gateway guard" || bad "apply.cgi eth0 [ -n \"\$GATEWAY\" ] guard missing"
grep -q 'if \[ -n "\$GATEWAY_ETH1" \]; then' "$APPLY" && ok "apply.cgi eth1 empty-gateway guard" || bad "apply.cgi eth1 [ -n \"\$GATEWAY_ETH1\" ] guard missing"

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS  all subnet-validation cases green"
    exit 0
fi
echo "FAIL  $fails case(s) failed"
exit 1
