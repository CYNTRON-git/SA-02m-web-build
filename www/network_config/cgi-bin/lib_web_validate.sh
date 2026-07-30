#!/bin/bash
# Shared allow-list validators for CGI input reaching a shell word, path, or
# config file. Every request-derived value used in a command, a file write, or
# a device path MUST pass one of these BEFORE use (web-code-rigor.md ## Bash CGI
# floors). A validator prints nothing; it returns 0 (valid) / 1 (reject).

# IPv4 dotted-quad, each octet 0-255.
valid_ipv4() {
    local ip="$1" o1 o2 o3 o4
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    o1=${BASH_REMATCH[1]}; o2=${BASH_REMATCH[2]}; o3=${BASH_REMATCH[3]}; o4=${BASH_REMATCH[4]}
    for o in "$o1" "$o2" "$o3" "$o4"; do
        [ "$o" -le 255 ] 2>/dev/null || return 1
    done
    return 0
}

# One or more space-separated IPv4 addresses (DNS list). Empty is NOT valid here
# — callers treat empty as "field omitted" before calling.
valid_ipv4_list() {
    local list="$1" one
    [ -n "$list" ] || return 1
    for one in $list; do
        valid_ipv4 "$one" || return 1
    done
    return 0
}

# A DNS hostname (letters, digits, dot, hyphen; 1-253 chars). No shell metachars,
# so safe to interpolate into a config value or URL.
valid_hostname() {
    local h="$1"
    [ "${#h}" -ge 1 ] && [ "${#h}" -le 253 ] || return 1
    [[ "$h" =~ ^[A-Za-z0-9.-]+$ ]] || return 1
    return 0
}

# A serial device path under /dev (ttySx, ttyUSBx, COMx, RS-485-x). Rejects
# traversal and metachars.
valid_serial_dev() {
    local d="$1"
    [[ "$d" =~ ^/dev/[A-Za-z0-9_-]+$ ]] || return 1
    return 0
}

# A bounded positive integer: valid_uint <value> <min> <max>.
valid_uint() {
    local v="$1" lo="$2" hi="$3"
    [[ "$v" =~ ^[0-9]+$ ]] || return 1
    [ "$v" -ge "$lo" ] && [ "$v" -le "$hi" ] 2>/dev/null || return 1
    return 0
}

# ── Subnet-membership helpers (gateway-in-subnet validation) ────────────────
# Pure-bash integer math (no bc). Inputs are assumed already valid_ipv4-checked
# by the caller; helpers re-split defensively. Values feed only into $(( ))
# arithmetic — never a shell word, path, or config write.

# Print the 32-bit integer for a dotted-quad. force base-10 on every octet with
# 10#: valid_ipv4 accepts a leading-zero octet (e.g. 010) which unforced $(( ))
# would read as octal (8) and so disagree with the literal string written to the
# config. 10# makes the arithmetic match the decimal reading. (Leading-zero
# octets are a pre-existing valid_ipv4 edge, not introduced here.)
ipv4_to_int() {
    local ip="$1"
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    printf '%s' "$(( (10#${BASH_REMATCH[1]})*16777216 + (10#${BASH_REMATCH[2]})*65536 + (10#${BASH_REMATCH[3]})*256 + 10#${BASH_REMATCH[4]} ))"
}

# Return 0 iff <mask> is a non-zero contiguous netmask (a run of 1-bits then
# 0-bits, e.g. 255.255.255.0). Rejects 0.0.0.0 and non-contiguous masks
# (e.g. 255.255.0.255).
netmask_is_contiguous() {
    local m inv
    m=$(ipv4_to_int "$1") || return 1
    [ "$m" -ne 0 ] || return 1
    inv=$(( (~m) & 0xFFFFFFFF ))
    (( (inv & (inv + 1)) == 0 ))
}

# Return 0 iff <ip> and <gw> share the subnet defined by <mask>. Assumes <mask>
# already passed netmask_is_contiguous (caller ordering). All operands come from
# ipv4_to_int (base-10-forced integers).
same_ipv4_subnet() {
    local ip gw m
    ip=$(ipv4_to_int "$1") || return 1
    gw=$(ipv4_to_int "$2") || return 1
    m=$(ipv4_to_int "$3")  || return 1
    (( (ip & m) == (gw & m) ))
}
