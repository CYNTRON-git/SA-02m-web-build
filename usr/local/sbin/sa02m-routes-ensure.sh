#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-routes-ensure.sh — put back the static IPv4 routes an interface conf
# declares on `post-up`/`up` lines when the kernel table no longer has them.
# Idempotent, always exit 0.
#
# Why this exists: ifupdown applies such a route ONLY by running the conf line
# at the end of a successful `ifup`. Two things skip that on this board:
#   - an `ifup` that dies on the gateway step (late PHY carrier) aborts before
#     its post-up stage — the chain is homed in docs/contracts/boot-network-dns.md
#     §2; the line is simply never run;
#   - a link cycle (fix-eth.sh, sa02m-eth-coldboot.sh) flushes every route on
#     the device, and only the default route is restored afterwards.
# Either way the board pings and serves its panel while every client behind
# the declared gateway loses it (bench 1.135, 2026-10-29).
# Contract: docs/contracts/boot-network-routes.md
#
# NEVER executes a conf line (one awk parses it; only anchored-regex-validated
# tokens leave the parser, as separate argv to ip(8)), NEVER touches a default
# route (fix-eth.sh / inet-failover.sh / the dhclient hooks own those), NEVER
# modifies or deletes an existing route — a route to the same prefix with ANY
# gateway is left alone — and never fails its caller.
#
# Logs ONLY on an add, a rejected line (once per boot per conf content) or a
# failed add (once per boot per iface+destination). A board logging an add
# every boot is a visible signal that ifup is still failing — keep it that way.
#
# Callers: fix-eth.sh and sa02m-eth-coldboot.sh (the recovery ladder, both
# branches each). No unit of its own: coldboot already gives the boot pass.
#
# Usage: sa02m-routes-ensure.sh [<iface>|--all]   (no argument == --all)
# ═══════════════════════════════════════════════════════════════════════════
set -u

IFACE_CONF_DIR=/etc/network/interfaces.d
STATE_DIR=/run/sa02m-routes-ensure

# Bounds on what may reach ip(8) from an operator-influenced file.
MAX_LINE_LEN=1024
MAX_ROUTES=16

log() { logger -t sa02m-routes-ensure -- "$*" 2>/dev/null || true; }

valid_iface() {
    case "$1" in
        ''|.|..|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    [ "${#1}" -le 15 ] || return 1
    return 0
}

# One awk pass over the conf. Prints `R <dst> <via> <dev|-> <metric|-> <0|1>`
# per supported route line of THIS interface's `iface <if> inet` stanza and
# `X <lineno>` per route line it will not apply; nothing for any other line.
# The grammar is the contract's (§3). Board awk is mawk: no `{n,m}` interval
# braces, no gawk extensions. Every emitted token matched an anchored regex —
# no raw conf text ever leaves this function.
parse_conf() {
    awk -v ifc="$1" -v maxlen="$MAX_LINE_LEN" -v maxr="$MAX_ROUTES" '
    # A token with leading quote/paren/$ and trailing quote/paren/; stripped,
    # then any path prefix: `"ip` and `$(/sbin/ip` both give `ip`.
    function base(t) {
        sub(LEAD, "", t); sub(TRAIL, "", t); sub(/.*\//, "", t)
        return t
    }
    # A route command wrapped in something else (`sleep 5 && ip route add`,
    # `/bin/sh -c "ip route add …"`): an `ip` word later followed by a route
    # object, or `route add`. Detection only — such a line is LOGGED as not
    # understood and never applied; no token of it reaches ip(8) or the log.
    function wrapped(from,    k, b, sip) {
        sip = 0
        for (k = from; k <= NF; k++) {
            b = base($k)
            if (sip && b ~ RO) return 1
            if (b == "ip") sip = 1
            if (b == "route" && base($(k + 1)) == "add") return 1
        }
        return 0
    }
    BEGIN {
        Q = sprintf("%c", 39)
        LEAD = "^[\"" Q "`($]+"
        TRAIL = "[\"" Q "`);]+$"
        O = "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])"
        IP4 = "^" O "\\." O "\\." O "\\." O "$"
        DST = "^" O "\\." O "\\." O "\\." O "(/(3[0-2]|[12][0-9]|[1-9]))?$"
        # ip(8) prefix-matches the object: `ip r add` IS `ip route add`.
        RO = "^(r|ro|rou|rout|route)$"
        in_st = 0; nr = 0
    }
    { sub(/\r$/, "") }
    $1 == "iface" || $1 == "mapping" || $1 == "auto" || $1 ~ /^allow-/ ||
    $1 == "source" || $1 == "source-directory" || $1 == "rename" ||
    $1 == "no-auto-down" || $1 == "no-scripts" {
        in_st = ($1 == "iface" && $2 == ifc && $3 == "inet")
        next
    }
    !in_st { next }
    $1 != "post-up" && $1 != "up" { next }
    {
        c = $2
        if (c == "route" || c == "/sbin/route" || c == "/usr/sbin/route" || c == "/bin/route") {
            print "X " NR; next
        }
        if (c != "ip" && c != "/sbin/ip" && c != "/usr/sbin/ip" && c != "/bin/ip" && c != "/usr/bin/ip") {
            if (wrapped(2)) print "X " NR
            next
        }
        # Options before the object. -4/-6 and `-f|-family inet|inet6` are
        # understood; any other option makes the line unparseable, so it is
        # logged if a route object follows anywhere on the line. What this
        # parser cannot see at all: a route added inside a script or binary the
        # line calls — only the conf text is read.
        i = 3; fam = ""; unk = 0
        while (substr($i, 1, 1) == "-") {
            o = $i
            if (o == "-4" || o == "-6") fam = o
            else if ((o == "-f" || o == "-family") && ($(i + 1) == "inet" || $(i + 1) == "inet6")) {
                fam = ($(i + 1) == "inet") ? "-4" : "-6"; i++
            } else unk = 1
            i++
        }
        if (unk) {
            for (k = i; k <= NF; k++) if ($k ~ RO) { print "X " NR; next }
            next
        }
        if ($i !~ RO) { if (wrapped(i + 1)) print "X " NR; next }
        if (fam == "-6") { print "X " NR; next }
        i++; a = $i
        # A bare `ip route` lists, like show/list.
        if (a == "" || a == "del" || a == "delete" || a == "flush" || a == "show" || a == "list" || a == "get") {
            if (wrapped(i + 1)) print "X " NR
            next
        }
        if (a != "add" && a != "replace") { print "X " NR; next }
        i++; dst = $i
        if (dst == "default" || dst == "0.0.0.0/0" || dst == "0/0" || dst ~ /\/0$/) {
            if (wrapped(i + 1)) print "X " NR
            next
        }
        if (length($0) > maxlen || dst !~ DST) { print "X " NR; next }

        via = ""; dev = "-"; met = "-"; onl = 0
        sdev = 0; smet = 0
        st = "kv"; ok = 1
        for (j = i + 1; j <= NF && ok; j++) {
            t = $j
            if (substr(t, 1, 1) == "#") {
                # sh comment: only where the line is already complete.
                if (st == "or") ok = 0
                break
            }
            if (st == "end") { ok = 0; break }
            if (st == "or") {
                if (t == "true" || t == "/bin/true" || t == "/usr/bin/true" || t == ":") st = "end"
                else ok = 0
                continue
            }
            if (t == "||") { st = "or"; continue }
            if (t == ">/dev/null" || t == "1>/dev/null" || t == "2>/dev/null" || t == "2>&1") { st = "tail"; continue }
            if (st == "tail") { ok = 0; break }
            if (t == "via") {
                if (via != "" || j == NF || $(j + 1) !~ IP4) { ok = 0; break }
                j++; via = $j
            } else if (t == "dev") {
                if (sdev || j == NF) { ok = 0; break }
                j++; v = $j
                if (v != ifc && v != "$IFACE" && v != "${IFACE}") { ok = 0; break }
                dev = ifc; sdev = 1
            } else if (t == "metric") {
                if (smet || j == NF) { ok = 0; break }
                j++; v = $j
                if (v !~ /^(0|[1-9][0-9]*)$/ || length(v) > 9) { ok = 0; break }
                met = v; smet = 1
            } else if (t == "onlink") {
                if (onl) { ok = 0; break }
                onl = 1
            } else { ok = 0; break }
        }
        if (st == "or" || via == "") ok = 0
        if (!ok || nr >= maxr) { print "X " NR; next }
        nr++
        print "R " dst " " via " " dev " " met " " onl
    }' "$2" 2>/dev/null
}

# Positional parameters are function-scoped, so `set --` here builds the argv
# without touching the caller's. Called only when `show exact` was empty.
add_route() {
    _ar_if=$1 _ar_dst=$2 _ar_via=$3 _ar_dev=$4 _ar_met=$5 _ar_onl=$6
    [ -n "$(ip -4 route show exact "$_ar_dst" 2>/dev/null </dev/null)" ] && return 0
    set -- route add "$_ar_dst" via "$_ar_via"
    _ar_desc="$_ar_dst via $_ar_via"
    if [ "$_ar_dev" != "-" ]; then
        set -- "$@" dev "$_ar_dev"; _ar_desc="$_ar_desc dev $_ar_dev"
    fi
    if [ "$_ar_met" != "-" ]; then
        set -- "$@" metric "$_ar_met"; _ar_desc="$_ar_desc metric $_ar_met"
    fi
    if [ "$_ar_onl" = 1 ]; then
        set -- "$@" onlink; _ar_desc="$_ar_desc onlink"
    fi
    _ar_mark="$STATE_DIR/${_ar_if}.fail.$(printf '%s' "$_ar_dst" | tr './' '-_')"
    # `add`, never `replace`: if anything inserted this prefix since the check
    # above, the kernel answers EEXIST instead of us overwriting its gateway.
    if _ar_err=$(ip -4 "$@" 2>&1 </dev/null); then
        log "$_ar_if: route $_ar_desc added (ifup never applied it)"
        rm -f "$_ar_mark" 2>/dev/null || true
        return 0
    fi
    # Lost the race to ifup / the operator: present now, nothing to report.
    [ -n "$(ip -4 route show exact "$_ar_dst" 2>/dev/null </dev/null)" ] && return 0
    [ -e "$_ar_mark" ] && return 0
    _ar_err=$(printf '%s\n' "$_ar_err" | head -n 1 | tr -d '[:cntrl:]' | cut -c 1-200)
    log "$_ar_if: route $_ar_dst via $_ar_via could not be added: $_ar_err"
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    touch "$_ar_mark" 2>/dev/null || true
    return 0
}

ensure_iface() {
    _if=$1
    valid_iface "$_if" || return 0
    [ "$_if" = "lo" ] && return 0
    # `_conf`, not `conf`: the --all loop below iterates a variable of that
    # name and sh has no function-local scope.
    _conf="$IFACE_CONF_DIR/${_if}.conf"
    [ -f "$_conf" ] || return 0

    # Steady state of every factory board: no route lines, one awk, no ip(8).
    _parsed=$(parse_conf "$_if" "$_conf")
    [ -n "$_parsed" ] || return 0

    _rej=0; _lines=""; _nr=0
    while read -r _k _a _rest; do
        case "$_k" in
            X) _rej=$((_rej + 1))
               [ "$_rej" -le 8 ] && _lines="${_lines}${_lines:+,}${_a}" ;;
            R) _nr=$((_nr + 1)) ;;
        esac
    done <<EOF
$_parsed
EOF
    [ "$_rej" -gt 8 ] && _lines="${_lines},..."

    # Once per boot per conf CONTENT: an edit changes the checksum and re-arms
    # the line. An unwritable STATE_DIR repeats it every pass — toward
    # visibility, never toward silence. Line numbers only, never conf text.
    if [ "$_rej" -gt 0 ]; then
        _sum=$(cksum < "$_conf" 2>/dev/null)
        _rmark="$STATE_DIR/${_if}.rejected"
        # An empty checksum (cksum missing or failing) logs every pass rather
        # than matching the empty read of an absent marker and never logging.
        if [ -z "$_sum" ] || [ "$(cat "$_rmark" 2>/dev/null)" != "$_sum" ]; then
            log "$_if: $_rej route line(s) in ${_if}.conf not understood and skipped (line(s) $_lines) — supported form: docs/contracts/boot-network-routes.md"
            mkdir -p "$STATE_DIR" 2>/dev/null || true
            # printf is a REGULAR built-in, so a failed open is an ordinary
            # non-zero status even under dash (a special built-in such as `:`
            # would abort the script — sa02m-eth-coldboot.sh, the `touch`
            # comment). The brace group only lets 2>/dev/null take effect
            # before the inner `>` fails, silencing the shell's diagnostic.
            { printf '%s\n' "$_sum" > "$_rmark"; } 2>/dev/null || true
        fi
    fi

    [ "$_nr" -gt 0 ] || return 0
    # No address yet: the ladder calls again on its next pass.
    [ -n "$(ip -4 -o addr show dev "$_if" 2>/dev/null </dev/null)" ] || return 0

    set -f
    while read -r _k _dst _via _dev _met _onl; do
        [ "$_k" = R ] || continue
        add_route "$_if" "$_dst" "$_via" "$_dev" "$_met" "$_onl"
    done <<EOF
$_parsed
EOF
    set +f
    return 0
}

# ip(8) absent (a stripped board) — clean no-op, no log.
command -v ip >/dev/null 2>&1 || exit 0

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

exit 0
