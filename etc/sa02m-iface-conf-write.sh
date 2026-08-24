#!/bin/bash
# sa02m-iface-conf-write.sh — the ONLY root file-write capability granted to
# www-data via sudoers (replaces the former unpinned `sudo tee`, audit B1).
#
# Writes stdin to exactly one LAN interfaces.d conf (or its one-generation
# `.sa02m-bak` backup). Exists because apply.cgi must (re)write a managed conf
# and back it up as root; granting raw `tee` to www-data was an arbitrary
# root file-write. Same four-name allow-list as etc/sa02m-conf-rm.sh.
# Contract: docs/contracts/ethernet-iface-naming.md §5.
#
# THREE guards, all fail-closed:
#   1. destination path — a literal `case` allow-list (4 LAN confs + their
#      .sa02m-bak), symlink refused;
#   2. CONTENT (live .conf only) — every line must match the ifupdown grammar
#      the panel + installer legitimately emit; an exec directive
#      (pre-up/up/post-up/*-down/source/mapping) is allowed ONLY when it EXACTLY
#      matches an enumerated safe form (the klogic adjust hook, the installer
#      default-route hook), parameterised solely by an already-validated
#      IPv4 / allow-listed iface / integer. Any unknown keyword, any other hook,
#      any source/mapping → REJECT, NO write. This closes the B1-round-2 finding:
#      without it, www-data could call this helper DIRECTLY (bypassing apply.cgi's
#      own validation) to plant `pre-up /bin/sh …` in eth0.conf and get root at
#      the next `sudo -n ifup eth0`;
#   3. the `.sa02m-bak` backups are NEVER sourced (the `source *.conf` filter in
#      scripts/02-network.sh excludes them), so they carry no exec risk and skip
#      the content guard — only the live `.conf` targets are content-validated.
#
# Content comes from STDIN only — never from an argument, so no request value can
# reach a shell word.
# Exit: 0 written; 2 validation refused (path, symlink, or content).
set -o pipefail

LOG="/var/log/sa02m_install.log"
dst="${1:-}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-iface-conf-write: $*" >> "$LOG" 2>&1 || true
}

# ── Guard 1: destination path allow-list (4 LAN confs + their .sa02m-bak) ────
case "$dst" in
    /etc/network/interfaces.d/eth0.conf|\
    /etc/network/interfaces.d/eth1.conf|\
    /etc/network/interfaces.d/end0.conf|\
    /etc/network/interfaces.d/end1.conf|\
    /etc/network/interfaces.d/eth0.conf.sa02m-bak|\
    /etc/network/interfaces.d/eth1.conf.sa02m-bak|\
    /etc/network/interfaces.d/end0.conf.sa02m-bak|\
    /etc/network/interfaces.d/end1.conf.sa02m-bak)
        ;;
    *)
        log "REFUSED (not allow-listed): $dst"
        exit 2
        ;;
esac

# A symlink at the destination under a root-written path is an escalation vector.
if [ -L "$dst" ]; then
    log "REFUSED (symlink): $dst"
    exit 2
fi

# Is this a live, ifupdown-sourced .conf (content-validated) or an inert
# .sa02m-bak backup (path-validated only, never sourced)?
is_bak=0
case "$dst" in
    *.sa02m-bak) is_bak=1 ;;
esac

# ── Read stdin to a private temp so the write is byte-exact and validated once ─
tmp=$(mktemp) || { log "REFUSED (mktemp failed): $dst"; exit 2; }
chmod 600 "$tmp" 2>/dev/null || true
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"

# ── Guard 2: content validation (live .conf only) ───────────────────────────
# One IPv4 octet / address; the exec-form parameters. `[.]`/`[|]` keep dots and
# pipes literal in ERE.
_o='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
IP="${_o}[.]${_o}[.]${_o}[.]${_o}"

# One conf line -> 0 allowed, 1 rejected. Classified by its FIRST keyword: exec
# directives need an EXACT enumerated form; inert config keywords accept a
# value-shape (they cannot make ifupdown run a command); anything else fails.
iface_line_ok() {
    local l="$1" t kw
    [[ "$l" =~ ^[[:space:]]*$ ]] && return 0          # blank
    [[ "$l" =~ ^[[:space:]]*# ]] && return 0          # comment
    t="${l#"${l%%[![:space:]]*}"}"                    # strip leading whitespace
    kw="${t%%[[:space:]]*}"
    case "$kw" in
        pre-up|up|post-up|pre-down|down|post-down|source|source-directory|mapping)
            # klogic hook: (post-up|up) /home/klogic/adjust-<if> [ || /bin/true | || true ]
            [[ "$l" =~ ^[[:space:]]*(post-up|up)[[:space:]]+/home/klogic/adjust-(eth0|eth1|end0|end1)([[:space:]]+[|][|][[:space:]]+(/bin/true|true))?[[:space:]]*$ ]] && return 0
            # installer default-route hook: post-up ip route replace default via <ip> dev <if> metric <int> [ || true ]
            [[ "$l" =~ ^[[:space:]]*post-up[[:space:]]+ip[[:space:]]+route[[:space:]]+replace[[:space:]]+default[[:space:]]+via[[:space:]]+${IP}[[:space:]]+dev[[:space:]]+(eth0|eth1|end0|end1)[[:space:]]+metric[[:space:]]+[0-9]+([[:space:]]+[|][|][[:space:]]+true)?[[:space:]]*$ ]] && return 0
            return 1
            ;;
        auto|allow-hotplug|allow-auto|no-auto-down)
            [[ "$l" =~ ^[[:space:]]*(auto|allow-hotplug|allow-auto|no-auto-down)[[:space:]]+[A-Za-z][A-Za-z0-9._-]*[[:space:]]*$ ]] && return 0
            return 1 ;;
        iface)
            [[ "$l" =~ ^[[:space:]]*iface[[:space:]]+[A-Za-z][A-Za-z0-9._-]*[[:space:]]+inet[[:space:]]+(static|dhcp|manual|loopback)[[:space:]]*$ ]] && return 0
            return 1 ;;
        address)
            [[ "$l" =~ ^[[:space:]]*address[[:space:]]+${IP}(/[0-9][0-9]?)?[[:space:]]*$ ]] && return 0
            return 1 ;;
        netmask|gateway|broadcast|network|pointopoint)
            [[ "$l" =~ ^[[:space:]]*(netmask|gateway|broadcast|network|pointopoint)[[:space:]]+${IP}[[:space:]]*$ ]] && return 0
            return 1 ;;
        dns-nameservers)
            [[ "$l" =~ ^[[:space:]]*dns-nameservers([[:space:]]+${IP})+[[:space:]]*$ ]] && return 0
            return 1 ;;
        dns-search|dns-domain)
            [[ "$l" =~ ^[[:space:]]*(dns-search|dns-domain)[[:space:]]+[A-Za-z0-9][A-Za-z0-9._-]*[[:space:]]*$ ]] && return 0
            return 1 ;;
        metric|mtu)
            [[ "$l" =~ ^[[:space:]]*(metric|mtu)[[:space:]]+[0-9]+[[:space:]]*$ ]] && return 0
            return 1 ;;
        hwaddress)
            [[ "$l" =~ ^[[:space:]]*hwaddress[[:space:]]+ether[[:space:]]+[0-9A-Fa-f][0-9A-Fa-f](:[0-9A-Fa-f][0-9A-Fa-f]){5}[[:space:]]*$ ]] && return 0
            return 1 ;;
        *)
            return 1 ;;   # unknown keyword — fail-closed
    esac
}

if [ "$is_bak" = 0 ]; then
    # Bounds (independent of apply.cgi, since this helper is directly callable).
    if [ "$(wc -c < "$tmp")" -gt 32768 ] || [ "$(wc -l < "$tmp")" -gt 200 ]; then
        log "REFUSED (content over bounds): $dst"
        exit 2
    fi
    # Reject any control char except tab and the line-separating newline.
    if LC_ALL=C tr -d '\t' < "$tmp" | LC_ALL=C grep -q '[[:cntrl:]]'; then
        log "REFUSED (control char in content): $dst"
        exit 2
    fi
    while IFS= read -r _line || [ -n "$_line" ]; do
        if ! iface_line_ok "$_line"; then
            log "REFUSED (line not allow-listed): $dst :: $_line"
            exit 2
        fi
    done < "$tmp"
fi

# ── Write (byte-exact copy of the validated stdin) ──────────────────────────
if cat "$tmp" > "$dst"; then
    chmod 644 "$dst" 2>/dev/null || true
    log "wrote: $dst"
    exit 0
fi
log "REFUSED (write failed): $dst"
exit 2
