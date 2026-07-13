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
