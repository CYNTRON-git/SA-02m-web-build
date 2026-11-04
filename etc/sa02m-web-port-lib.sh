#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-web-port-lib.sh — the panel's nginx port: the ONE home for its default,
# the /etc/sa02m_web_port.conf format, the reader, and the grammar of the
# `listen <port> default_server;` line in the rendered site.
#
# Why a file: `install.sh --port N` used to render nginx on N and save it
# nowhere, so the next re-render (www-only deploy, --refresh, offline pack)
# put the panel back on 9999, and the OTA health gate probed 9999 on a board
# serving N and rolled back every update (backlog 2026-09-24, audit L4).
#
# POSIX sh, SOURCED (never executed), SILENT — every function reports through
# its exit status; callers log in their own voice. Sourced by scripts/lib.sh
# (installer, bash) from the repo tree, and softly by the update runner from
# /usr/local/lib (installed there by scripts/03-webserver.sh and
# scripts/update-www-only.sh; OTA/offline packs deploy it too).
#
# The file: one line `SA02M_WEB_PORT=<N>`, written ONLY by the installer
# (scripts/lib.sh sa02m_web_port_persist — `--port`, or the one-time carry-over
# of a pre-fix `--port` board). ABSENT = the default: a board that never used
# `--port` has no file and behaves exactly as before the file existed. The file
# is never `source`d: it is read line by line, and anything but a valid port
# reads as the default, so a tampered file can never reach a shell word.
# ═══════════════════════════════════════════════════════════════════════════

SA02M_WEB_PORT_DEFAULT=9999
SA02M_WEB_PORT_CONF="${SA02M_WEB_PORT_CONF:-/etc/sa02m_web_port.conf}"

# sa02m_web_port_valid <value> → 0 iff <value> is a TCP port 1..65535 written
# as plain decimal (no sign, no leading zero, no spaces). The length test runs
# first so an oversized number never reaches shell arithmetic.
sa02m_web_port_valid() {
    case "${1:-}" in
        ''|*[!0-9]*|0*) return 1 ;;
    esac
    [ "${#1}" -le 5 ] || return 1
    [ "$1" -le 65535 ]
}

# sa02m_web_port → prints the port the panel is served on. rc 0: the file is
# absent (default) or carries a valid port. rc 3: the file exists but is
# unreadable, has no SA02M_WEB_PORT line, or its value is not a port — the
# DEFAULT is printed and the caller WARNs (fail to the documented default,
# never to a half-parsed value). The first SA02M_WEB_PORT line wins; CR is
# tolerated (a file edited on Windows).
sa02m_web_port() {
    if [ ! -e "$SA02M_WEB_PORT_CONF" ]; then
        printf '%s\n' "$SA02M_WEB_PORT_DEFAULT"
        return 0
    fi
    _swp_val=""
    _swp_cr=$(printf '\r')
    if [ -r "$SA02M_WEB_PORT_CONF" ]; then
        while IFS= read -r _swp_line || [ -n "$_swp_line" ]; do
            _swp_line=${_swp_line%"$_swp_cr"}
            case "$_swp_line" in
                SA02M_WEB_PORT=*) _swp_val=${_swp_line#SA02M_WEB_PORT=}; break ;;
            esac
        done < "$SA02M_WEB_PORT_CONF"
    fi
    if sa02m_web_port_valid "$_swp_val"; then
        printf '%s\n' "$_swp_val"
        return 0
    fi
    printf '%s\n' "$SA02M_WEB_PORT_DEFAULT"
    return 3
}

# sa02m_web_port_from_site <file> → prints the port of the first
# `listen <N> default_server;` line of a RENDERED site file; rc 1 when the file
# is absent or carries no such line with a valid port. Used once, to carry over
# a board installed with `--port` before the port was saved.
sa02m_web_port_from_site() {
    [ -f "${1:-}" ] || return 1
    _swp_all=$(sed -n 's/^[[:space:]]*listen[[:space:]]\{1,\}\([0-9]\{1,\}\)[[:space:]]\{1,\}default_server[[:space:]]*;.*$/\1/p' "$1" 2>/dev/null) || return 1
    _swp_first=${_swp_all%%
*}
    sa02m_web_port_valid "$_swp_first" || return 1
    printf '%s\n' "$_swp_first"
}

# sa02m_web_port_set_listen <port> — filter stdin → stdout: every
# `listen <N> default_server;` line of a rendered site re-pointed at <port>,
# everything else byte-for-byte. rc 2 on an invalid <port> (nothing written).
# The runner uses it on the offline pack's site, which the packer renders at
# the default port because it cannot know the board's.
sa02m_web_port_set_listen() {
    sa02m_web_port_valid "${1:-}" || return 2
    sed "s/^\([[:space:]]*listen[[:space:]]\{1,\}\)[0-9]\{1,\}\([[:space:]]\{1,\}default_server[[:space:]]*;\)/\1$1\2/"
}
