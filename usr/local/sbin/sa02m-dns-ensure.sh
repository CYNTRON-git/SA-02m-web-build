#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-dns-ensure.sh — regenerate the resolver REGARDLESS of whether ifup
# completed. Two rungs, idempotent, always exit 0.
#
# Why this exists: /etc/network/if-up.d/000resolvconf is the ONLY thing on this
# board that ever updates resolvconf. An `ifup` that dies on the gateway step
# (late PHY carrier — see sa02m-wait-carrier.sh) aborts before the if-up.d
# stage, so the hook never runs and /etc/resolv.conf stays EMPTY while the
# address and the default route look healthy. The board pings and serves its
# panel; nothing fails until something resolves a name, and then the OTA web
# update dies with "Could not resolve host: github.com".
# /etc/resolvconf/resolv.conf.d/base already carries working fallback servers —
# they were simply never applied, because only that hook would have applied them.
# Contract: docs/contracts/boot-network-dns.md
#
#   rung 1  file the interface record from interfaces.d `dns-nameservers`,
#           under the SAME record name 000resolvconf uses, and only when that
#           record is ABSENT — so a real ifup always wins, and ifdown's own
#           /etc/network/if-down.d/resolvconf removes it normally (no leak).
#   rung 2  if /etc/resolv.conf still carries no nameserver at all, run
#           `resolvconf -u` so resolv.conf.d/base materialises.
#
# Callers: sa02m-dns-ensure.service (the deterministic boot pass, After=
# networking.service), fix-eth.sh and sa02m-eth-coldboot.sh (the recovery
# ladder, for "the cable arrived later"). The ladder alone is not enough — it
# has early-return and skip branches — hence the unit.
#
# NEVER removes a working resolver: rung 1 only ADDS when the record is absent,
# rung 2 only runs when there is no nameserver at all. Nothing here touches the
# link, the address or the route, so it cannot knock a live board off the LAN.
#
# Logs ONLY when it actually writes. That is deliberate: a board logging this
# every boot is a visible signal that ifup is still failing. Do not make it
# quiet, and do not make it unconditional.
#
# Usage: sa02m-dns-ensure.sh [<iface>|--all]   (no argument == --all)
# ═══════════════════════════════════════════════════════════════════════════
set -u

IFACE_CONF_DIR=/etc/network/interfaces.d
RESOLVCONF_RUN_DIR=/run/resolvconf/interface
RESOLV_CONF=/etc/resolv.conf

# Bounds on what may reach `resolvconf` from an operator-influenced file.
MAX_LINE_LEN=1024
MAX_NAMESERVERS=8
MAX_SEARCH=6

IPV4_RE='^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$'
IPV6_RE='^(([0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|([0-9A-Fa-f]{1,4}:){1,7}:|([0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|([0-9A-Fa-f]{1,4}:){1,5}(:[0-9A-Fa-f]{1,4}){1,2}|([0-9A-Fa-f]{1,4}:){1,4}(:[0-9A-Fa-f]{1,4}){1,3}|([0-9A-Fa-f]{1,4}:){1,3}(:[0-9A-Fa-f]{1,4}){1,4}|([0-9A-Fa-f]{1,4}:){1,2}(:[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(:[0-9A-Fa-f]{1,4}){1,6}|:((:[0-9A-Fa-f]{1,4}){1,7}|:))$'
SEARCH_RE='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'

log() { logger -t sa02m-dns-ensure -- "$*" 2>/dev/null || true; }

valid_iface() {
    case "$1" in
        ''|.|..|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    [ "${#1}" -le 15 ] || return 1
    return 0
}

# interfaces.d is root-owned but operator-influenced: apply.cgi validates what
# the panel writes, KLogic and manual edits do not. Its values reach
# `resolvconf -a` as DATA, so every token is validated at this boundary and the
# rest dropped — a control character or an oversized line never gets through,
# and no raw line ever becomes a shell word.
valid_ns() {
    printf '%s' "$1" | grep -Eq "$IPV4_RE" && return 0
    printf '%s' "$1" | grep -Eq "$IPV6_RE" && return 0
    return 1
}

valid_search() {
    [ "${#1}" -le 253 ] || return 1
    printf '%s' "$1" | grep -Eq "$SEARCH_RE"
}

# First value of $2 in conf $1; empty when absent or over-long. `tr -d '\r'`
# because a CRLF-delivered conf would otherwise turn every token into an
# invalid one and silently produce no record (the recurring CRLF class,
# BUGLOG 2026-08-14).
conf_value() {
    _v=$(sed -n "s/^[[:space:]]*$2[[:space:]]\{1,\}\(.*\)\$/\1/p" "$1" 2>/dev/null \
         | head -n 1 | tr -d '\r')
    [ "${#_v}" -le "$MAX_LINE_LEN" ] || return 0
    printf '%s' "$_v"
}

# Same field order 000resolvconf writes, so a later real ifup overwrites this
# record cleanly instead of producing a different-looking one. Reads the three
# lists ensure_iface has just built.
emit_record() {
    [ -n "$search_out" ] && printf 'search %s\n' "$search_out"
    [ -n "$domain_out" ] && printf 'domain %s\n' "$domain_out"
    for _ns in $ns_out; do printf 'nameserver %s\n' "$_ns"; done
    return 0
}

ensure_iface() {
    iface=$1
    valid_iface "$iface" || return 0
    [ "$iface" = "lo" ] && return 0

    # Cheap test FIRST. net-watchdog drives fix-eth.sh every 30 s, which drives
    # this — the steady-state path must cost one stat and nothing else.
    [ -e "$RESOLVCONF_RUN_DIR/${iface}.inet" ] && return 0

    # `_conf`, not `conf`: the --all loop below iterates a variable of that
    # name and sh has no function-local scope.
    _conf="$IFACE_CONF_DIR/${iface}.conf"
    [ -f "$_conf" ] || return 0

    ns_raw=$(conf_value "$_conf" "dns-nameservers")
    # No nameservers declared is NORMAL, not an error: a dhcp stanza (2-eth
    # eth1) has none, and dhclient owns its own resolvconf record.
    [ -n "$ns_raw" ] || return 0

    ns_out=""
    ns_count=0
    for tok in $ns_raw; do
        [ "$ns_count" -lt "$MAX_NAMESERVERS" ] || break
        valid_ns "$tok" || continue
        ns_out="${ns_out}${ns_out:+ }${tok}"
        ns_count=$((ns_count + 1))
    done
    # Parse failure writes NOTHING — never a partial or guessed record.
    [ -n "$ns_out" ] || return 0

    search_out=""
    search_count=0
    for tok in $(conf_value "$_conf" "dns-search"); do
        [ "$search_count" -lt "$MAX_SEARCH" ] || break
        valid_search "$tok" || continue
        search_out="${search_out}${search_out:+ }${tok}"
        search_count=$((search_count + 1))
    done

    domain_out=""
    for tok in $(conf_value "$_conf" "dns-domain"); do
        valid_search "$tok" || continue
        domain_out=$tok
        break
    done

    if emit_record | resolvconf -a "${iface}.inet" >/dev/null 2>&1; then
        log "$iface: resolvconf record filed (ifup never reached its if-up.d hooks): $ns_out"
    else
        log "$iface: resolvconf -a failed, resolver left as it was"
    fi
    return 0
}

# resolvconf absent (an older or stripped board) — clean no-op, today's
# behaviour, no log.
command -v resolvconf >/dev/null 2>&1 || exit 0

case "${1:---all}" in
    --all)
        for conf in "$IFACE_CONF_DIR"/*.conf; do
            [ -f "$conf" ] || continue
            ensure_iface "$(basename "$conf" .conf)"
        done
        ;;
    -*)
        exit 0
        ;;
    *)
        ensure_iface "$1"
        ;;
esac

# ── rung 2 — the fallback sweep ────────────────────────────────────────────
# resolv.conf.d/base is present and correct on every board the installer has
# touched; nothing but the if-up.d hook ever applied it. This is the rung that
# would have prevented the outage on its own.
if ! grep -Eq '^[[:space:]]*nameserver[[:space:]]+[^[:space:]]' "$RESOLV_CONF" 2>/dev/null; then
    if resolvconf -u >/dev/null 2>&1; then
        log "resolv.conf carried no nameserver — regenerated from resolvconf (resolv.conf.d/base)"
    else
        log "resolv.conf carries no nameserver and 'resolvconf -u' failed"
    fi
fi

exit 0
