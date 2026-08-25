#!/bin/bash
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
web_session_check_cookie || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

# POST-only + CSRF BEFORE any mutation — network/time config is written to
# /etc/network as root. policy: docs/decisions/selective-csrf-policy.md.
# Headers not yet emitted here, so the gate prints its own and web_csrf_require
# emits its own on failure.
if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
    echo "Content-type: application/json"; echo ""
    echo '{"ok":false,"error":"method_not_allowed"}'
    exit 0
fi
web_csrf_require

# FCGI: читать ровно CONTENT_LENGTH байт (read -n останавливается на переводе строки)
POST_DATA=""
if [ -n "${CONTENT_LENGTH:-}" ]; then
    CL=$(printf '%s' "${CONTENT_LENGTH}" | tr -cd '0-9')
    if [ -n "$CL" ] && [ "$CL" -gt 0 ] 2>/dev/null; then
        POST_DATA=$(dd bs=1 count="$CL" 2>/dev/null) || POST_DATA=""
    fi
fi

decode() { printf '%b' "$(printf '%s' "$1" | sed 's/+/ /g; s/%/\\x/g')"; }
get_f() {
    local line val
    line=$(printf '%s' "$POST_DATA" | tr '&' '\n' | grep "^${1}=" | head -1)
    val="${line#*=}"
    decode "$val"
}

# shellcheck source=lib_web_validate.sh
. "$(dirname "$0")/lib_web_validate.sh"
# shellcheck source=lib_net_iface.sh
. "$(dirname "$0")/lib_net_iface.sh"

# Reject the whole request with a JSON error (network fields are attacker-
# controlled and are written into /etc/network/interfaces.d as root — an
# unvalidated newline injects an ifupdown pre-up hook, RCE). Never proceed to
# the sudo tee below on a bad value.
reject_bad_input() {
    echo "Content-type: application/json"; echo ""
    printf '{"error":"invalid input: %s"}\n' "$1"
    exit 0
}

# ── Foreign-stanza preservation ────────────────────────────────────────────
# The panel regenerates an interfaces.d conf from the form on every save, and
# until now that DELETED every line it did not itself write — including the
# `post-up /home/klogic/adjust-eth0` hook that is a KLogic board's ONLY path for
# applying its IP. One save silently disabled KLogic's IP control.
#
# Rule: we own an allow-list of stanza headers and option keys; every other line
# is foreign and is copied through VERBATIM in its original order. Security
# boundary (unchanged, not widened): preserved content comes ONLY from the
# existing root-owned file — no request value can create, extend or influence a
# preserved line, and form values still reach only the managed keys, which still
# pass the allow-list validation below before any write.
PRESERVE_MAX_LINES=64
PRESERVE_MAX_BYTES=8192
PRESERVE_MAX_LINE=512
PRESERVE_PRE=()
PRESERVE_IN=()
PRESERVE_DROPPED=0

# The only option keys we write, and therefore the only ones we replace.
conf_key_is_managed() {
    case "$1" in
        address|netmask|gateway|dns-nameservers|metric) return 0 ;;
    esac
    return 1
}

# First token at column 0 that opens a new stanza (ifupdown grammar).
conf_token_starts_stanza() {
    case "$1" in
        auto|allow-*|iface|source|source-directory|mapping) return 0 ;;
    esac
    return 1
}

# Single-generation backup before any destructive write. The pinned helper
# sa02m-iface-conf-write.sh is the only root file-write primitive the panel
# holds (audit B1 — www-data no longer has raw `tee`); it allow-lists the four
# LAN conf names and their .sa02m-bak targets, refuses symlinks, and
# content-validates the live .conf (the backup targets are inert, never sourced,
# so they skip the content check). One home, no accumulating history.
conf_backup() {
    [ -f "$1" ] && [ -r "$1" ] || return 0
    sudo -n /usr/local/sbin/sa02m-iface-conf-write.sh "${1}.sa02m-bak" < "$1" >/dev/null 2>&1 || true
}

# conf_scan_foreign <conf> <iface>
# Fills PRESERVE_PRE / PRESERVE_IN. Returns 1 when the file is unusable for a
# merge — unreadable, no `iface <iface> inet …` line, or more than one stanza
# for the same interface. A wrong merge into a network config is worse than a
# clean regeneration, so the caller then backs the file up and regenerates.
conf_scan_foreign() {
    local conf="$1" iface="$2"
    local line first second third stanzas=0 count=0 bytes=0 seen_iface=0 in_our_stanza=0

    PRESERVE_PRE=(); PRESERVE_IN=(); PRESERVE_DROPPED=0
    [ -f "$conf" ] && [ -r "$conf" ] || return 1

    stanzas=$(grep -cE "^[[:space:]]*iface[[:space:]]+${iface}[[:space:]]+inet6?([[:space:]]|\$)" "$conf" 2>/dev/null)
    [ "$stanzas" = "1" ] || return 1

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        # Drop any line carrying a control character other than tab.
        case "$line" in
            *[$'\001'-$'\010'$'\013'-$'\037'$'\177']*)
                PRESERVE_DROPPED=$((PRESERVE_DROPPED + 1)); continue ;;
        esac
        if [ "${#line}" -gt "$PRESERVE_MAX_LINE" ]; then
            PRESERVE_DROPPED=$((PRESERVE_DROPPED + 1)); continue
        fi

        # Token-split rather than match the whole line: a hand-edited
        # `auto  eth0` (double space) or an indented header must still be
        # recognised as OURS and regenerated, never preserved as foreign — else
        # the file would end up with two `auto` lines for one interface.
        read -r first second third _ <<< "$line"

        # Our own stanza headers are regenerated, never preserved.
        case "$first" in
            auto|allow-*) [ "$second" = "$iface" ] && continue ;;
        esac
        if [ "$seen_iface" = "0" ] && [ "$first" = "iface" ] \
           && [ "$second" = "$iface" ] && [ "$third" = "inet" ]; then
            seen_iface=1; in_our_stanza=1; continue
        fi
        # A new stanza (first token at column 0) closes ours: from there on
        # `address`/`netmask`/… belong to somebody else and must be preserved,
        # not swallowed as managed.
        if [ "$in_our_stanza" = "1" ] && [ "${line:0:1}" != " " ] && [ "${line:0:1}" != "	" ] \
           && conf_token_starts_stanza "$first"; then
            in_our_stanza=0
        fi
        if [ "$in_our_stanza" = "1" ] && conf_key_is_managed "$first"; then
            continue
        fi

        if [ "$count" -ge "$PRESERVE_MAX_LINES" ] \
           || [ $(( bytes + ${#line} + 1 )) -gt "$PRESERVE_MAX_BYTES" ]; then
            PRESERVE_DROPPED=$((PRESERVE_DROPPED + 1)); continue
        fi
        count=$((count + 1)); bytes=$(( bytes + ${#line} + 1 ))

        if [ "$seen_iface" = "0" ]; then
            PRESERVE_PRE+=("$line")
        else
            PRESERVE_IN+=("$line")
        fi
    done < "$conf"

    return 0
}

# conf_scan_source <conf> <iface> — scan ONCE per request; the buckets are then
# reused for every file this request writes, so the canonical conf and its
# legacy mirror are guaranteed to carry identical foreign content.
conf_scan_source() {
    if conf_scan_foreign "$1" "$2"; then
        if [ "$(( ${#PRESERVE_PRE[@]} + ${#PRESERVE_IN[@]} + PRESERVE_DROPPED ))" -gt 0 ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: $1 preserved $(( ${#PRESERVE_PRE[@]} + ${#PRESERVE_IN[@]} )) foreign line(s), dropped $PRESERVE_DROPPED" \
                >> /var/log/sa02m_install.log 2>&1
        fi
        return 0
    fi
    # Not mergeable. An absent file is the normal first-write case and needs no
    # backup; a present-but-malformed one is backed up before the clean
    # regeneration — a wrong merge into a network config is worse than a
    # recoverable regeneration (fail-safe toward "the interface still comes up").
    if [ -f "$1" ]; then
        conf_backup "$1"
        echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: $1 not mergeable (missing/duplicate iface $2 stanza) — backed up to ${1}.sa02m-bak and regenerated" \
            >> /var/log/sa02m_install.log 2>&1
    fi
    PRESERVE_PRE=(); PRESERVE_IN=()
    return 0
}

# conf_assemble <header line> <iface line> <managed option line…>
# pre-stanza foreign → managed block → in-stanza foreign, into CONF_LINES.
conf_assemble() {
    CONF_LINES=()
    [ "${#PRESERVE_PRE[@]}" -gt 0 ] && CONF_LINES+=("${PRESERVE_PRE[@]}")
    CONF_LINES+=("$@")
    [ "${#PRESERVE_IN[@]}" -gt 0 ] && CONF_LINES+=("${PRESERVE_IN[@]}")
    return 0
}

# Remove a legacy sibling conf whose device no longer exists. Primary path:
# the pinned sa02m-conf-rm.sh helper (the only rm capability sudoers grants
# www-data — allow-listed to the four LAN conf names, backs up first).
# Fallback where the helper/sudoers line is absent (pre-1.0.5.54 deploys):
# back up and neutralise to comments — stanza-free, ignored by `ifup -a`;
# the root-run installer deletes it on its next pass. Leaving an `auto end0`
# stanza for a gone device would fail networking.service at the next boot.
lan_conf_retire() {
    local conf="$1" canon="$2"
    [ -f "$conf" ] || return 0
    grep -qE '^[[:space:]]*(auto|allow-[a-z]+|iface|mapping)[[:space:]]' "$conf" 2>/dev/null || return 0
    # Primary path: the pinned rm helper (allow-listed, backs up itself) —
    # present on boards installed/updated since 1.0.5.54. Falls back to
    # retire-to-comments where the helper (or its sudoers line) is absent.
    if sudo -n /usr/local/sbin/sa02m-conf-rm.sh "$conf" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: $conf deleted via sa02m-conf-rm (superseded by ${canon})" \
            >> /var/log/sa02m_install.log 2>&1
        return 0
    fi
    conf_backup "$conf"
    write_iface_conf "$conf" \
        "# Retired by the SA-02m web panel: this interface is now ${canon}." \
        "# Previous content: ${conf}.sa02m-bak" \
        "# See docs/contracts/ethernet-iface-naming.md"
    echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: $conf retired (device absent, superseded by ${canon})" \
        >> /var/log/sa02m_install.log 2>&1
}

# write_lan_pair <canon> <legacy> <src-iface> <src-conf> <header-kw> <inet-type> <option line…>
#
# SUPERSEDES the former "write $CONF0 then rm the sibling" rule, which was
# actively wrong during the migration window: on a migrated-but-not-yet-rebooted
# board resolve_lan_iface prefers the LIVE name, so the save wrote end0.conf and
# DELETED eth0.conf — undoing the migration, and at the next boot the renamed
# eth0 would find no conf and come up with no address.
#
# The rule instead: always write the canonical conf; mirror it under the legacy
# name as `allow-hotplug` only while the legacy device is still live; bounce
# whichever interface is actually up.
write_lan_pair() {
    local canon="$1" legacy="$2" src_if="$3" src_conf="$4" hdr="$5" inet="$6"
    shift 6
    local canon_conf legacy_conf

    canon_conf=$(lan_iface_conf "$canon")
    legacy_conf=$(lan_iface_conf "$legacy")

    conf_scan_source "$src_conf" "$src_if"

    # Canonical conf — always, whatever the board is called today. This is what
    # survives the boot-time rename.
    conf_backup "$canon_conf"
    conf_assemble "$hdr $canon" "iface $canon inet $inet" "$@"
    write_iface_conf "$canon_conf" "${CONF_LINES[@]}"

    if [ -d "/sys/class/net/$legacy" ]; then
        # DUAL-CONF window: the rename happens at the next boot, so the legacy
        # device is what is actually carrying traffic right now. Mirror the same
        # content under `allow-hotplug` (NOT `auto`) so that once the rename
        # succeeds `ifup -a` ignores this stanza and networking.service stays
        # clean, and bounce the interface that is really up.
        conf_backup "$legacy_conf"
        conf_assemble "allow-hotplug $legacy" "iface $legacy inet $inet" "$@"
        write_iface_conf "$legacy_conf" "${CONF_LINES[@]}"
        schedule_iface_apply "$legacy"
    else
        lan_conf_retire "$legacy_conf" "$canon"
        schedule_iface_apply "$canon"
    fi
}

# Write an interfaces.d conf from a list of complete lines.
# The former `echo -e "$CFG" | sudo tee` interpreted backslash escapes in the
# assembled string. Harmless while every value was a validated IPv4, fatal once
# arbitrary preserved lines pass through it — a `\t` or `\n` inside a foreign
# post-up would be rewritten. printf '%s\n' interprets nothing, and the output
# is byte-identical to the old form for the managed lines.
# The write goes through the pinned sa02m-iface-conf-write.sh (audit B1 — no raw
# `tee`); it allow-lists the destination to the four LAN conf names AND
# content-validates each line (rejecting any non-enumerated pre-up/post-up hook),
# so a direct helper call cannot plant a root-exec hook `ifup` would run.
write_iface_conf() {
    local conf="$1"; shift
    printf '%s\n' "$@" | sudo -n /usr/local/sbin/sa02m-iface-conf-write.sh "$conf" >/dev/null
}

timeout_run() {
    local sec=${1:-5}
    shift || true
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
}

NET_IFACE=$(get_f "net_iface")
IP=$(get_f "ip"); NETMASK=$(get_f "netmask"); GATEWAY=$(get_f "gateway"); DNS=$(get_f "dns")
IP_ETH1=$(get_f "ip_eth1"); NETMASK_ETH1=$(get_f "netmask_eth1")
GATEWAY_ETH1=$(get_f "gateway_eth1"); DNS_ETH1=$(get_f "dns_eth1")
ETH0_ENABLE=$(get_f "eth0_enable"); ETH1_ENABLE=$(get_f "eth1_enable"); SKIP_NETWORK=$(get_f "skip_network")
TIMEZONE=$(get_f "timezone")
DATETIME=$(get_f "datetime")
DATETIME=$(printf '%s' "${DATETIME:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

REDIRECT="applied"
timezone_failed=0
time_ok=0
# Interfaces to bounce after HTTP response (space-separated).
# Suffix ":down" = ifdown only (conf removed). Bare name = ifdown+ifup.
# Background via nohup — bare (...)& dies with fcgiwrap when CGI exits.
APPLY_IFACES=""

schedule_iface_apply() {
    local iface="$1" mode="${2:-up}"
    [ -n "$iface" ] || return 0
    case " $APPLY_IFACES " in
        *" $iface "*|*" $iface:down "*) ;;
        *)
            if [ "$mode" = "down" ]; then
                APPLY_IFACES="${APPLY_IFACES:+$APPLY_IFACES }$iface:down"
            else
                APPLY_IFACES="${APPLY_IFACES:+$APPLY_IFACES }$iface"
            fi
            ;;
    esac
}

# Apply interfaces.d conf live. Use /sbin/ifdown|/sbin/ifup only (sudoers).
apply_ifaces_live() {
    local spec iface mode
    [ -n "$APPLY_IFACES" ] || return 0
    for spec in $APPLY_IFACES; do
        iface="${spec%:down}"
        mode="up"
        case "$spec" in
            *:down) mode="down" ;;
        esac
        # shellcheck disable=SC2016
        nohup sh -c '
iface="$1"
mode="$2"
log=/var/log/sa02m_install.log
run_to() { t="$1"; shift; if command -v timeout >/dev/null 2>&1; then timeout "$t" "$@"; else "$@"; fi; }
sleep 1
echo "$(date "+%Y-%m-%d %H:%M:%S") apply.cgi: live-apply $iface mode=$mode" >> "$log" 2>&1
run_to 20 sudo -n /sbin/ifdown "$iface" >> "$log" 2>&1 || true
if [ "$mode" = "down" ]; then
    echo "$(date "+%Y-%m-%d %H:%M:%S") apply.cgi: $iface ifdown done (conf removed)" >> "$log" 2>&1
    exit 0
fi
if run_to 25 sudo -n /sbin/ifup "$iface" >> "$log" 2>&1; then
    echo "$(date "+%Y-%m-%d %H:%M:%S") apply.cgi: $iface ifup OK" >> "$log" 2>&1
else
    echo "$(date "+%Y-%m-%d %H:%M:%S") apply.cgi: $iface ifup failed — restart networking" >> "$log" 2>&1
    run_to 40 sudo -n /usr/bin/systemctl restart networking.service >> "$log" 2>&1 || true
fi
[ -x /usr/local/bin/sa02m-eth0-led.sh ] && /usr/local/bin/sa02m-eth0-led.sh >> "$log" 2>&1 || true
' _ "$iface" "$mode" >>/var/log/sa02m_install.log 2>&1 &
    done
}

# ── Validate all network fields BEFORE any config write (allow-list) ────────
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth0" ] && [ "$ETH0_ENABLE" = "1" ]; then
    valid_ipv4 "$IP"       || reject_bad_input "ip"
    valid_ipv4 "$NETMASK"  || reject_bad_input "netmask"
    [ -z "$GATEWAY" ] || valid_ipv4 "$GATEWAY" || reject_bad_input "gateway"
    [ -z "$DNS" ]     || valid_ipv4_list "$DNS" || reject_bad_input "dns"
    # A default gateway outside the address subnet writes an unroutable
    # `default via <foreign-gw>` route that dead-routes the board. Reject it.
    if [ -n "$GATEWAY" ]; then
        netmask_is_contiguous "$NETMASK"             || reject_bad_input "netmask"
        same_ipv4_subnet "$IP" "$GATEWAY" "$NETMASK" || reject_bad_input "gateway"
    fi
fi
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth1" ] && [ "$ETH1_ENABLE" = "1" ]; then
    valid_ipv4 "$IP_ETH1"       || reject_bad_input "ip_eth1"
    valid_ipv4 "$NETMASK_ETH1"  || reject_bad_input "netmask_eth1"
    [ -z "$GATEWAY_ETH1" ] || valid_ipv4 "$GATEWAY_ETH1" || reject_bad_input "gateway_eth1"
    [ -z "$DNS_ETH1" ]     || valid_ipv4_list "$DNS_ETH1" || reject_bad_input "dns_eth1"
    if [ -n "$GATEWAY_ETH1" ]; then
        netmask_is_contiguous "$NETMASK_ETH1"                          || reject_bad_input "netmask_eth1"
        same_ipv4_subnet "$IP_ETH1" "$GATEWAY_ETH1" "$NETMASK_ETH1"    || reject_bad_input "gateway_eth1"
    fi
fi

# ── Ethernet № 1 (form net_iface=eth0; on-disk name may be eth0 or end0) ───
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth0" ]; then
    IF0=$(resolve_lan_iface eth0 end0)
    CONF0=$(lan_iface_conf "$IF0")
    if [ "$ETH0_ENABLE" = "1" ] && [ -n "$IP" ] && [ -n "$NETMASK" ]; then
        MANAGED0=("    address $IP" "    netmask $NETMASK")
        [ -n "$GATEWAY" ] && MANAGED0+=("    gateway $GATEWAY")
        [ -n "$DNS" ]     && MANAGED0+=("    dns-nameservers $DNS")
        write_lan_pair eth0 end0 "$IF0" "$CONF0" auto static "${MANAGED0[@]}"
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth0.conf updated IP=$IP (merged from ${CONF0})" >> /var/log/sa02m_install.log 2>&1
    else
        write_lan_pair eth0 end0 "$IF0" "$CONF0" auto dhcp
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth0.conf set to dhcp (merged from ${CONF0})" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── Ethernet № 2 (form net_iface=eth1; on-disk name may be eth1 or end1) ───
# Same semantics as eth0: enable+IP = static; toggle OFF = DHCP (NOT retire).
# Factory default on 2-eth boards is already dhcp; retiring the conf was the
# panel trap that left eth1 with no address and no way back to DHCP.
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth1" ]; then
    IF1=$(resolve_lan_iface eth1 end1)
    CONF1=$(lan_iface_conf "$IF1")
    if [ "$ETH1_ENABLE" = "1" ] && [ -n "$IP_ETH1" ] && [ -n "$NETMASK_ETH1" ]; then
        MANAGED1=("    address $IP_ETH1" "    netmask $NETMASK_ETH1")
        [ -n "$GATEWAY_ETH1" ] && MANAGED1+=("    gateway $GATEWAY_ETH1")
        [ -n "$DNS_ETH1" ]     && MANAGED1+=("    dns-nameservers $DNS_ETH1")
        write_lan_pair eth1 end1 "$IF1" "$CONF1" allow-hotplug static "${MANAGED1[@]}"
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf updated IP=$IP_ETH1 (merged from ${CONF1})" >> /var/log/sa02m_install.log 2>&1
    else
        # Match installer default: allow-hotplug + dhcp + metric 100. Default
        # route comes from dhclient exit-hook (ensure below), not a hard-coded GW.
        write_lan_pair eth1 end1 "$IF1" "$CONF1" allow-hotplug dhcp "    metric 100"
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf set to dhcp (merged from ${CONF1})" >> /var/log/sa02m_install.log 2>&1
        # OTA-only boards may lack the installer hook; www-data cannot write
        # /etc/dhcp — pinned helper (fixed content, no args). Best-effort.
        if sudo -n /usr/local/sbin/sa02m-ensure-eth1-dhcp-hook.sh >/dev/null 2>&1; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: eth1-default-route dhclient hook ensured" >> /var/log/sa02m_install.log 2>&1
        else
            echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: eth1 dhcp hook ensure skipped (helper/sudoers absent)" >> /var/log/sa02m_install.log 2>&1
        fi
    fi
fi

# ── Timezone (ошибка таймзоны не блокирует установку времени) ──────────────
if [ -n "$TIMEZONE" ]; then
    if timeout_run 8 sudo -n /usr/bin/timedatectl set-timezone "$TIMEZONE" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') timezone set to $TIMEZONE" >> /var/log/sa02m_install.log 2>&1
    else
        timezone_failed=1
        echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: timedatectl set-timezone failed ($TIMEZONE)" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── System time + RTC ───────────────────────────────────────────────────────
SCRIPT_DIR=$(dirname "$0")
# shellcheck source=lib_rtc.sh
. "$SCRIPT_DIR/lib_rtc.sh"

apply_system_time() {
    local dt="$1"
    # При зависшем dbus timedatectl может не возвращаться → nginx 504; везде timeout.
    timeout_run 6 sudo -n /usr/bin/timedatectl set-ntp false >/dev/null 2>&1 || true
    if timeout_run 18 sudo -n /usr/bin/timedatectl set-time "$dt" >/dev/null 2>&1; then
        return 0
    fi
    if timeout_run 10 sudo -n /bin/date -s "$dt" >/dev/null 2>&1; then
        return 0
    fi
    if timeout_run 10 sudo -n /usr/bin/date -s "$dt" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

sync_hwclock_from_sys() {
    timeout_run 12 sync_rtc_from_system >/dev/null 2>&1 || true
}

if [ -n "$DATETIME" ]; then
    if apply_system_time "$DATETIME"; then
        time_ok=1
        sync_hwclock_from_sys
        echo "$(date '+%Y-%m-%d %H:%M:%S') datetime set to $DATETIME" >> /var/log/sa02m_install.log 2>&1
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: set time failed ($DATETIME)" >> /var/log/sa02m_install.log 2>&1
        REDIRECT="error_time"
    fi
fi

if [ "$REDIRECT" = "applied" ]; then
    if [ "$time_ok" = "1" ] && [ "$timezone_failed" = "1" ]; then
        REDIRECT="applied_tz_failed"
    elif [ "$time_ok" = "0" ] && [ "$timezone_failed" = "1" ] && [ -z "$DATETIME" ]; then
        REDIRECT="error_tz"
    fi
fi

# Schedule background bounce before headers — response must reach the browser
# first; ifdown drops the TCP session when the IP changes.
apply_ifaces_live

echo "Status: 302 Found"
echo "Content-type: text/html"
echo "Location: /?status=${REDIRECT}"
echo ""
