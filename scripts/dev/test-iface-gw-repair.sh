#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-iface-gw-repair.sh — regression test for the installer's gateway/DNS
# repair (scripts/02-network.sh ensure_gw_dns; contract §3, subsection
# «Ремонт gateway/dns-nameservers установщиком»).
#
# Why this exists: the repair edits the file that decides whether the board has
# a default route, it runs on EVERY install as root, and its worst failure mode
# is a gateway written into the wrong subnet — `ifup` then fails with "Network
# is unreachable", networking.service goes FAILED and the board is unreachable
# (the 1.0.3.38 class through a new door). The defect class here is PARSING and
# IDEMPOTENCY against formatting variance (indented vs not, `no-auto-down`
# before the stanza, interleaved comments and post-up hooks), which a static
# grep gate cannot see — so this is a functional harness, in the style the repo
# already chose twice for interfaces.d code (test-iface-conf-merge.sh,
# test-iface-migration.sh).
#
# Method: extract the SHIPPED ensure_gw_dns and retarget its one absolute root
# (/etc/network/interfaces.d) into a scratch tree; the subnet helpers come from
# the SHIPPED scripts/lib.sh. `log` is stubbed to a capture buffer so the WARN
# paths are asserted, not assumed. Nothing touches the real system.
#
# Non-vacuous: a failed/over-wide extraction, an un-retargeted path, a missing
# lib.sh helper, or a case whose fixture stopped matching FAILS — none of them
# pass on zero matches.
#
# Drive-to-failure recipe (how this harness was proven to catch a break):
#   cp scripts/02-network.sh /tmp/broken.sh
#   # neuter one guard, e.g. `elif false && ! same_ipv4_subnet …`, drop the `#?`
#   # from the presence regex, or replace `[ "$anchor" -ge 0 ] || anchor=$start`
#   # with `anchor=$start`
#   GW_REPAIR_SRC=/tmp/broken.sh bash scripts/dev/test-iface-gw-repair.sh
# GW_REPAIR_LIB does the same for scripts/lib.sh (the subnet helpers and the
# resolvconf head), so neither override ever edits the real tree.
# Every guard in ensure_gw_dns has been neutered one at a time and the run
# checked; the battery and its results are reported with the change, not
# summarised here.
#
# Known limit, stated rather than papered over: neutering the `cmp -s` render
# comparison changes NO observable behaviour, because the function already
# returns before rendering when neither key is missing. It is defence in depth,
# not the idempotency mechanism, so no case here can catch its NEUTERING — only
# its REMOVAL, which extract_guard below pins. Do not read that note as "the
# only redundant guard": any guard whose inputs a LATER guard also rejects is
# equally unneuterable, which is why the fail-closed branches below are asserted
# on their WARN TEXT (each branch must say what it actually saw) rather than on
# "nothing was written" alone — that is what keeps them individually pinned.
#
# Run: bash scripts/dev/test-iface-gw-repair.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# ── Fault injection ────────────────────────────────────────────────────────
# The write path can only be exercised by making a command fail, and a real
# failure is not portably injectable: some filesystems ignore the read-only
# directory bit, and rename(2) over a read-only FILE legitimately succeeds. So
# the commands are stubbed.
#
# Every stub is defined HERE — once, above every call site — and is INERT
# unless BREAK names it. Defining them inside the case that needed them made
# the file's behaviour depend on textual position: a `cp` written above the
# definition got the real command, one below got the stub (shellcheck SC2218,
# caught by CI, not by the local shellcheck version). Nothing was actually
# resolving wrong, but the hazard is removed rather than documented.
#
# Belt as well as braces: the harness's own bookkeeping calls `command cp`
# explicitly, so injection can only ever reach the code under test — a future
# case cannot break its own fixtures by leaving BREAK set.
BREAK=""
cp_calls=0
breaking() { case " $BREAK " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

mktemp() { if breaking mktemp; then return 1; fi; command mktemp "$@"; }
wc()     { if breaking wc; then echo 1; return 0; fi; command wc "$@"; }
cp() {
    if breaking cp; then return 1; fi
    # cp-2nd: the backup succeeds, the later ROLLBACK fails (the double fault).
    if breaking cp-2nd; then
        cp_calls=$((cp_calls + 1))
        if [ "$cp_calls" -ge 2 ]; then return 1; fi
    fi
    command cp "$@"
}
mv() {
    if breaking mv; then return 1; fi
    # mv-empty: the replace "succeeds" but lands the wrong content, so only
    # reading the file back can catch it.
    if breaking mv-empty; then : > "$3"; command rm -f "$2"; return 0; fi
    command mv "$@"
}
# Presence stub: sa02m_write_resolvconf_head guards on `command -v resolvconf`,
# which a dev box does not have. Never called, only detected.
resolvconf() { :; }

SRC=${GW_REPAIR_SRC:-scripts/02-network.sh}
LIB=${GW_REPAIR_LIB:-scripts/lib.sh}
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
CD="$T/ifd"; mkdir -p "$CD"
C="$CD/eth0.conf"
GW=192.168.1.1
DNS="77.88.8.8 77.88.8.1"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── the subnet math under test comes from the SHIPPED lib ──────────────────
LOG_FILE="$T/install.log"
# shellcheck source=/dev/null
. "$LIB"
for helper in ipv4_to_int netmask_is_contiguous same_ipv4_subnet sa02m_write_resolvconf_head; do
    declare -F "$helper" >/dev/null || { echo "FAIL  $LIB does not define $helper"; exit 1; }
done

# ── extract the shipped repair ─────────────────────────────────────────────
sed -n '/^ensure_gw_dns() {/,/^}$/p' "$SRC" \
  | sed "s#/etc/network/interfaces\.d#$CD#g" > "$T/fn.sh"
extract_guard() {
    grep -q '^ensure_gw_dns() {' "$T/fn.sh" || return 1
    [ "$(tail -n1 "$T/fn.sh")" = "}" ] || return 1
    # the safety machinery must be INSIDE the extracted body, not left behind
    grep -q 'same_ipv4_subnet' "$T/fn.sh" || return 1
    grep -q 'cmp -s' "$T/fn.sh" || return 1
    grep -q 'sa02m-gwbak' "$T/fn.sh" || return 1
    grep -q 'sa02m-gwtmp' "$T/fn.sh" || return 1   # temp file beside the conf (same-fs rename)
    grep -q 'mv -f' "$T/fn.sh" || return 1         # atomic replace, not an in-place truncate
    grep -q 'nlines' "$T/fn.sh" || return 1        # the read-back post-condition (re-reads the written file)
    # and the sandbox retarget must have taken, or this test would edit /etc
    grep -q "$CD" "$T/fn.sh" || return 1
    ! grep -q '/etc/network/interfaces\.d' "$T/fn.sh" || return 1
}
if ! extract_guard; then
    echo "FAIL  could not extract a complete, sandboxed ensure_gw_dns from $SRC (did the function or its markers move?)"
    exit 1
fi

# `log` is stubbed AFTER lib.sh is sourced so the capture buffer wins.
LOGGED=""
log() { LOGGED="${LOGGED}$1 ${2:-}"$'\n'; }
# shellcheck disable=SC1090
. "$T/fn.sh"

warned()   { printf '%s' "$LOGGED" | grep -q '^WARN '; }
warned_re(){ printf '%s' "$LOGGED" | grep -qE "^WARN .*$1"; }
repair()   { LOGGED=""; GW_REPAIR_ADDED_GW=0; ensure_gw_dns eth0 "$GW" "$DNS"; }
count()    { grep -cE "$1" "$C"; }
unchanged(){ cmp -s "$C" "$T/before"; }
snapshot() { command cp "$C" "$T/before"; rm -f "$C.sa02m-gwbak"; }

# A real board conf: KLogic shapes (comment + no-auto-down before the stanza,
# hwaddress/post-up inside it) are the fixtures this repair must survive.
plain_conf() { cat > "$C" <<'EOF'
auto eth0
iface eth0 inet static
    address 192.168.1.136
    netmask 255.255.255.0
# добавлено сторонним инструментом
    hwaddress ether 02:53:8B:00:D4:30
    post-up /home/klogic/adjust-eth0 || /bin/true
EOF
}

echo "── 1. both keys absent -> both added, everything else verbatim ──"
plain_conf; snapshot; repair
[ "$(count '^    gateway 192\.168\.1\.1$')" = 1 ] && ok "gateway added with the stanza's 4-space indent" || bad "gateway not added / wrong indent"
[ "$(count '^    dns-nameservers 77\.88\.8\.8 77\.88\.8\.1$')" = 1 ] && ok "dns-nameservers added" || bad "dns-nameservers not added"
[ "$(count '^    address 192\.168\.1\.136$')" = 1 ] && ok "address untouched" || bad "address changed"
[ "$(count '^    netmask 255\.255\.255\.0$')" = 1 ] && ok "netmask untouched" || bad "netmask changed"
grep -q '^    post-up /home/klogic/adjust-eth0 || /bin/true$' "$C" && ok "vendor post-up preserved verbatim" || bad "vendor post-up lost or rewritten"
grep -q '^# добавлено сторонним инструментом$' "$C" && ok "in-stanza comment preserved" || bad "comment lost"
grep -q '^    hwaddress ether 02:53:8B:00:D4:30$' "$C" && ok "hwaddress preserved" || bad "hwaddress lost"
awk 'BEGIN{p=0;e=0}
     /^auto eth0$/{p=1}
     /^iface eth0 inet static$/{if(p!=1)e=1;p=2}
     /^    address /{if(p!=2)e=1;p=3}
     /^    netmask /{if(p!=3)e=1;p=4}
     /^    gateway /{if(p!=4)e=1;p=5}
     /^    dns-nameservers /{if(p!=5)e=1;p=6}
     /adjust-eth0/{if(p!=6)e=1;p=7}
     END{exit (e || p!=7)}' "$C" \
  && ok "insertion point: after the last address line, nothing reordered" || bad "stanza reordered / inserted in the wrong place"
[ -f "$C.sa02m-gwbak" ] && ok "one-generation backup taken as .sa02m-gwbak" || bad "no .sa02m-gwbak backup"
[ -f "$C.sa02m-bak" ] && bad "clobbered the panel's own .sa02m-bak restore point" || ok "the panel's .sa02m-bak name is not touched"
[ "$GW_REPAIR_ADDED_GW" = 1 ] && ok "GW_REPAIR_ADDED_GW=1 signals the live-route caller" || bad "GW_REPAIR_ADDED_GW not set"

echo "── 2. re-run is byte-identical and takes no backup ──"
command cp "$C" "$T/after1"; rm -f "$C.sa02m-gwbak"
repair
if cmp -s "$T/after1" "$C"; then ok "second run byte-identical"; else bad "second run churned the conf"; diff "$T/after1" "$C"; fi
[ -f "$C.sa02m-gwbak" ] && bad "no-op run still took a backup (mtime churn)" || ok "no-op run takes no backup"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "no-op run reports nothing added" || bad "no-op run claimed it added a gateway"
repair
if cmp -s "$T/after1" "$C"; then ok "third run still byte-identical"; else bad "third run churned the conf"; fi

echo "── 3. unindented stanza -> insertion matches that (zero) indent ──"
printf 'auto eth0\niface eth0 inet static\naddress 192.168.1.136\nnetmask 255.255.255.0\n' > "$C"
snapshot; repair
[ "$(count '^gateway 192\.168\.1\.1$')" = 1 ] && ok "gateway inserted at column 0, like its address line" || bad "indent not derived from the address line"
[ "$(count '^dns-nameservers ')" = 1 ] && ok "dns-nameservers inserted at column 0" || bad "dns indent wrong"
command cp "$C" "$T/after1"; repair
cmp -s "$T/after1" "$C" && ok "unindented layout is idempotent too" || bad "unindented re-run churned"

echo "── 4. no-auto-down before the stanza -> preserved, insertion still inside ──"
cat > "$C" <<'EOF'
# Wired adapter #1
allow-hotplug eth0
no-auto-down eth0
iface eth0 inet static
	address 192.168.1.136
	netmask 255.255.255.0
	post-up /home/klogic/adjust-eth0 || /bin/true
EOF
snapshot; repair
grep -q '^no-auto-down eth0$' "$C" && ok "pre-stanza no-auto-down preserved" || bad "no-auto-down lost"
grep -q '^# Wired adapter #1$' "$C" && ok "leading comment preserved" || bad "leading comment lost"
[ "$(count $'^\tgateway 192\\.168\\.1\\.1$')" = 1 ] && ok "gateway inserted INSIDE the stanza with the TAB indent" || bad "tab-indented insertion wrong"
awk 'BEGIN{p=0;e=0}
     /^no-auto-down eth0$/{p=1}
     /^iface eth0 inet static$/{if(p!=1)e=1;p=2}
     /^\tnetmask /{if(p!=2)e=1;p=3}
     /^\tgateway /{if(p!=3)e=1;p=4}
     END{exit (e || p!=4)}' "$C" \
  && ok "insertion lands after netmask, below the iface line" || bad "insertion escaped the stanza"

echo "── 5. gateway present -> never modified; only the absent dns is added ──"
plain_conf
sed -i 's#^    netmask 255.255.255.0$#    netmask 255.255.255.0\n    gateway 192.168.1.254#' "$C"
snapshot; repair
[ "$(count '^    gateway 192\.168\.1\.254$')" = 1 ] && ok "operator gateway kept verbatim" || bad "operator gateway modified"
[ "$(count '^[[:space:]]*gateway ')" = 1 ] && ok "no second gateway line added" || bad "gateway duplicated"
[ "$(count '^    dns-nameservers ')" = 1 ] && ok "absent dns added alone" || bad "dns not added alone"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "no live route claimed when the gateway was already there" || bad "GW_REPAIR_ADDED_GW set without adding a gateway"

echo "── 5b. a conf needing nothing is not even diagnosed ──"
# Both keys present AND a netmask we cannot parse: the repair has no work to do,
# so it must return before the parse guards rather than WARN about a file it was
# never going to touch. (Pins the early return; without it this logs noise.)
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask 255.255.0.255\n    gateway 192.168.1.1\n    dns-nameservers 8.8.8.8\n' > "$C"
snapshot; repair
unchanged && ok "conf with both keys untouched" || bad "wrote into a conf that needed nothing"
warned && bad "WARNed about a conf it had no work in" || ok "no diagnosis for a conf needing nothing"

echo "── 6. commented-out key counts as present -> nothing added ──"
# The address MUST be in the candidate gateway's subnet: on an out-of-subnet
# fixture the subnet guard refuses the write on its own, and this case would
# then pass even with the commented-key rule deleted — a case that cannot fail.
cat > "$C" <<'EOF'
auto eth0
iface eth0 inet static
address 192.168.1.136
netmask 255.255.255.0
#gateway 192.168.1.1
#dns-nameservers 192.168.1.1
EOF
snapshot; repair
unchanged && ok "a deliberately commented-out key is left alone (conf byte-identical)" || { bad "wrote over a commented-out key"; diff "$T/before" "$C"; }
[ -f "$C.sa02m-gwbak" ] && bad "backed up a conf it did not change" || ok "no backup for an unchanged conf"
# Control: the ONLY thing stopping a write here is the commented-key rule — the
# same fixture with the two comment lines removed must repair.
sed '/^#/d' "$T/before" > "$C"
repair
[ "$GW_REPAIR_ADDED_GW" = 1 ] && ok "control: the same fixture repairs once the comments are gone (case 6 is not vacuous)" || bad "control failed — case 6 would pass with the commented-key rule deleted"

echo "── 7. inet dhcp -> no write ──"
printf 'allow-hotplug eth0\niface eth0 inet dhcp\n    metric 100\n' > "$C"
snapshot; repair
unchanged && ok "dhcp stanza untouched (DHCP owns its route)" || bad "wrote a static gateway into a dhcp stanza"
# The stanza type must be what stops it — not the absent address/netmask, which
# would refuse this same fixture for an unrelated reason.
printf '%s' "$LOGGED" | grep -qE "^INFO .*inet static" && ok "the dhcp stanza type is the stated reason (INFO, not the address-missing WARN)" || { bad "dhcp refused for the wrong reason"; printf '%s' "$LOGGED"; }
# And a (synthetic) dhcp stanza that IS fully parseable is still refused, so the
# guard is the stanza type rather than a parse failure.
printf 'allow-hotplug eth0\niface eth0 inet dhcp\n    address 192.168.1.136\n    netmask 255.255.255.0\n' > "$C"
snapshot; repair
unchanged && ok "parseable dhcp stanza still refused" || bad "wrote into a parseable dhcp stanza"

echo "── 8. two stanzas for the same iface -> no write, WARN ──"
printf 'auto eth0\niface eth0 inet static\n    address 1.1.1.1\n    netmask 255.255.255.0\niface eth0 inet static\n    address 2.2.2.2\n' > "$C"
snapshot; repair
unchanged && ok "unparseable (duplicate-stanza) conf untouched" || bad "merged into a duplicate-stanza conf"
warned_re 'стансов' && ok "duplicate stanza logs a WARN" || bad "duplicate stanza logged no WARN"

echo "── 9. out-of-subnet candidate -> gateway refused, dns still added ──"
printf 'auto eth0\niface eth0 inet static\n    address 10.5.0.7\n    netmask 255.255.255.0\n' > "$C"
snapshot; repair
[ "$(count '^[[:space:]]*gateway ')" = 0 ] && ok "out-of-subnet gateway NOT written (the board-unreachable guard)" || bad "wrote a gateway outside the stanza's subnet"
[ "$(count '^    dns-nameservers ')" = 1 ] && ok "dns-nameservers still added (a DNS line cannot strand the board)" || bad "dns not added"
warned_re 'вне подсети' && ok "out-of-subnet logs a WARN naming both values" || bad "out-of-subnet logged no WARN"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "no live route attempted for a refused gateway" || bad "GW_REPAIR_ADDED_GW set on a refused gateway"
# and the in-subnet counterpart must still pass, or the guard is just "never write"
printf 'auto eth0\niface eth0 inet static\n    address 10.5.0.7\n    netmask 255.255.255.0\n' > "$C"
LOGGED=""; ensure_gw_dns eth0 10.5.0.1 "$DNS"
[ "$(count '^    gateway 10\.5\.0\.1$')" = 1 ] && ok "an IN-subnet gateway on the same fixture IS written (guard is not a blanket refusal)" || bad "in-subnet gateway refused — the guard rejects everything"

echo "── 10. malformed / unparseable stanza -> nothing written at all ──"
# Each branch must also SAY what it actually saw: the install log is this code's
# only failure surface, and a WARN that misdescribes the file sends the next
# debugger down the wrong path. Asserting the text is also what keeps these
# branches distinguishable from one another (they all end in "write nothing").
for m in "255.255.0.255" "abc" "0.0.0.0"; do
    printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask %s\n' "$m" > "$C"
    snapshot; repair
    unchanged && ok "netmask '$m': nothing written (fail closed)" || bad "netmask '$m': wrote into a stanza it could not parse"
    warned_re 'не разбираются' && ok "netmask '$m': WARN names the unparseable pair" || { bad "netmask '$m': wrong or missing WARN"; printf '%s' "$LOGGED"; }
done
printf 'auto eth0\niface eth0 inet static\n    address 999.1.1.1\n    netmask 255.255.255.0\n' > "$C"
snapshot; repair
unchanged && ok "malformed address: nothing written (fail closed)" || bad "malformed address: wrote anyway"
warned_re "address '999\.1\.1\.1'" && ok "malformed address: WARN quotes the offending value" || { bad "malformed address: WARN does not name it"; printf '%s' "$LOGGED"; }
printf 'auto eth0\niface eth0 inet static\n    post-up /bin/true\n' > "$C"
snapshot; repair
unchanged && ok "stanza with no address line: nothing written (fail closed)" || bad "wrote into a stanza with no address"
warned_re 'нет строки address' && ok "no-address WARN says exactly that" || { bad "no-address WARN misdescribes the file"; printf '%s' "$LOGGED"; }
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n' > "$C"
snapshot; repair
unchanged && ok "address with no mask at all: nothing written (fail closed)" || bad "wrote without a mask"
warned_re 'нет маски' && ok "no-mask WARN says the address has no mask (not 'no address')" || { bad "no-mask WARN misdescribes the file"; printf '%s' "$LOGGED"; }

echo "── 10b. CIDR address form (valid modern ifupdown, no netmask line) ──"
# `address 192.168.1.136/24` with no `netmask` line is a valid and common stanza.
# Treating it as unparseable would silently skip the repair on a healthy conf.
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136/24\n' > "$C"
snapshot; repair
[ "$(count '^    gateway 192\.168\.1\.1$')" = 1 ] && ok "CIDR stanza: gateway added" || { bad "CIDR stanza: gateway not added"; printf '%s' "$LOGGED"; }
[ "$(count '^    dns-nameservers ')" = 1 ] && ok "CIDR stanza: dns-nameservers added" || bad "CIDR stanza: dns not added"
[ "$(count '^    address 192\.168\.1\.136/24$')" = 1 ] && ok "CIDR address line untouched" || bad "CIDR address line rewritten"
# and the subnet guard must still work off the prefix, not be bypassed by it
printf 'auto eth0\niface eth0 inet static\n    address 10.5.0.7/24\n' > "$C"
snapshot; repair
[ "$(count '^[[:space:]]*gateway ')" = 0 ] && ok "CIDR stanza: out-of-subnet gateway still refused" || bad "CIDR parsing bypassed the subnet guard"
[ "$(count '^    dns-nameservers ')" = 1 ] && ok "CIDR stanza: dns still added on refusal" || bad "CIDR stanza: dns dropped"
# a /25 prefix must narrow the subnet exactly as a dotted netmask does
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136/25\n' > "$C"
snapshot; repair
[ "$(count '^[[:space:]]*gateway ')" = 0 ] && ok "/25 prefix: 192.168.1.1 correctly outside 192.168.1.128/25" || bad "/25 prefix widened the subnet"
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.10/25\n' > "$C"
snapshot; repair
[ "$(count '^    gateway 192\.168\.1\.1$')" = 1 ] && ok "/25 prefix: an in-subnet gateway is still written" || bad "/25 prefix refuses everything"
# a malformed or contradictory prefix is refused, and named
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136/99\n' > "$C"
snapshot; repair
unchanged && ok "prefix /99: nothing written" || bad "prefix /99 accepted"
warned_re 'префикс' && ok "prefix /99: WARN names the prefix" || { bad "prefix /99: WARN does not name it"; printf '%s' "$LOGGED"; }
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136/24\n    netmask 255.255.255.128\n' > "$C"
snapshot; repair
unchanged && ok "prefix and netmask disagreeing: nothing written" || bad "wrote with a contradictory prefix/netmask pair"
warned_re 'противоречат' && ok "contradiction is named in the WARN" || { bad "contradiction not named"; printf '%s' "$LOGGED"; }

echo "── 10c. a duplicated key inside one stanza is unparseable ──"
# The one construction that got past the subnet guard: two netmask lines, the
# guard reading the last one. ifupdown itself rejects a duplicated option, so
# treating it as unparseable matches ifupdown AND fails closed.
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask 255.255.255.128\n    netmask 255.255.255.0\n' > "$C"
snapshot; repair
[ "$(count '^[[:space:]]*gateway ')" = 0 ] && ok "duplicate netmask: no gateway written (the guard is not defeatable)" || bad "duplicate netmask defeated the subnet guard"
unchanged && ok "duplicate netmask: nothing written at all" || bad "duplicate netmask: wrote something"
warned_re 'повторяется ключ' && ok "duplicate key is named in the WARN" || { bad "duplicate key not named"; printf '%s' "$LOGGED"; }
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    address 10.5.0.7\n    netmask 255.255.255.0\n' > "$C"
snapshot; repair
unchanged && ok "duplicate address: nothing written" || bad "duplicate address: wrote something"

echo "── 11. comment-only conf (the panel's retired form) -> no write ──"
printf '# Retired by the SA-02m web panel: this interface is now eth0.\n# Previous content: eth0.conf.sa02m-bak\n' > "$C"
snapshot; repair
unchanged && ok "stanza-free conf untouched" || bad "wrote into a stanza-free conf"

echo "── 12. absent conf and the documented opt-out ──"
rm -f "$C" "$C.sa02m-gwbak"
repair
[ ! -f "$C" ] && ok "absent conf is not created by the repair (creation is the write path's job)" || bad "repair created a conf"
plain_conf; snapshot
LOGGED=""; GW_REPAIR_ADDED_GW=0; SA02M_SKIP_GW_REPAIR=1 ensure_gw_dns eth0 "$GW" "$DNS"
unchanged && ok "SA02M_SKIP_GW_REPAIR=1 leaves the conf byte-identical" || bad "opt-out did not stop the repair"
repair
unchanged && bad "control: the same fixture was not repaired without the opt-out (case 12 is vacuous)" || ok "control: the same fixture IS repaired without the opt-out"

echo "── 13. a second interface's conf is never touched ──"
plain_conf
printf 'allow-hotplug eth1\niface eth1 inet dhcp\n    metric 100\n' > "$CD/eth1.conf"
command cp "$CD/eth1.conf" "$T/eth1.before"
repair
cmp -s "$T/eth1.before" "$CD/eth1.conf" && ok "eth1.conf untouched while repairing eth0" || bad "repair reached into another interface's conf"
rm -f "$CD/eth1.conf"

echo "── 13c. a sibling stanza with identical lines does not confuse the read-back ──"
# The post-write check must count inside the SAME stanza body the decision was
# made from. Counting over the whole file, a byte-identical `gateway` line in a
# neighbouring stanza reads as a duplicate and rolls back a perfectly good write.
cat > "$C" <<'EOF'
auto eth0
iface eth0 inet static
    address 192.168.1.136
    netmask 255.255.255.0

auto eth9
iface eth9 inet static
    address 192.168.1.200
    netmask 255.255.255.0
    gateway 192.168.1.1
    dns-nameservers 77.88.8.8 77.88.8.1
EOF
snapshot; repair
[ "$GW_REPAIR_ADDED_GW" = 1 ] && ok "sibling stanza: the write is not rolled back" || { bad "sibling stanza rolled back a correct write"; printf '%s' "$LOGGED"; }
[ "$(count '^    gateway 192\.168\.1\.1$')" = 2 ] && ok "sibling stanza: both stanzas now carry their own gateway" || bad "sibling stanza: wrong gateway line count"
awk '/^iface eth0 inet static$/{p=1} p&&/^    gateway 192\.168\.1\.1$/{found=1;exit} /^iface eth9/{if(p&&!found)exit 1}' "$C" \
  && ok "sibling stanza: the added gateway landed in the eth0 body" || bad "sibling stanza: gateway landed in the wrong stanza"
grep -q '^    address 192\.168\.1\.200$' "$C" && ok "sibling stanza left untouched" || bad "sibling stanza modified"

echo "── 13b. a failed write never reports success ──"
# The write path drives the live-route half: GW_REPAIR_ADDED_GW=1 makes the
# caller run `ip route replace` and write a resolvconf head. If the conf write
# failed and we still logged OK, that head would name a gateway that is NOT in
# the conf — exactly the stale-head pathology sa02m_write_resolvconf_head exists
# to remove. So: no success log and no flag unless the file really changed.
# Failures are injected by stubbing the command, which is deterministic
# everywhere (a chmod-based injection is not: some filesystems ignore mode bits,
# and rename(2) over a read-only file legitimately succeeds anyway).
logged_ok() { printf '%s' "$LOGGED" | grep -q '^OK '; }

plain_conf; snapshot
BREAK=mktemp; repair; BREAK=""
unchanged && ok "mktemp failure: conf untouched" || bad "mktemp failure: conf changed"
logged_ok && bad "mktemp failure: logged OK anyway" || ok "mktemp failure: no success log"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "mktemp failure: no live-route flag" || bad "mktemp failure: set the live-route flag"
warned && ok "mktemp failure: WARN logged" || bad "mktemp failure: silent"

# A truncated render (ENOSPC mid-write — the board this change targets had a
# full /var/log) must be caught before the conf is replaced. Real ENOSPC is not
# injectable here, so the line count it is detected by is: under-report it and
# the repair must refuse.
plain_conf; snapshot
BREAK=wc; repair; BREAK=""
unchanged && ok "short render: conf untouched" || bad "short render: replaced the conf with an incomplete file"
logged_ok && bad "short render: logged OK anyway" || ok "short render: no success log"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "short render: no live-route flag" || bad "short render: set the live-route flag"
[ -z "$(find "$CD" -name '*.sa02m-gwtmp.*' 2>/dev/null)" ] && ok "short render: temp file cleaned up" || bad "short render: left a temp file behind"

plain_conf; snapshot
BREAK=cp; repair; BREAK=""
unchanged && ok "backup failure: conf untouched (no replacement without a rollback point)" || bad "backup failure: replaced the conf anyway"
logged_ok && bad "backup failure: logged OK anyway" || ok "backup failure: no success log"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "backup failure: no live-route flag" || bad "backup failure: set the live-route flag"
warned && ok "backup failure: WARN logged" || bad "backup failure: silent"

plain_conf; snapshot
BREAK=mv; repair; BREAK=""
unchanged && ok "replace failure: conf untouched" || bad "replace failure: conf changed"
logged_ok && bad "replace failure: logged OK anyway" || ok "replace failure: no success log"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "replace failure: no live-route flag" || bad "replace failure: set the live-route flag"
warned && ok "replace failure: WARN logged" || bad "replace failure: silent"
[ -z "$(find "$CD" -name '*.sa02m-gwtmp.*' 2>/dev/null)" ] && ok "replace failure: temp file cleaned up" || bad "replace failure: left a temp file behind"

# The write "succeeds" but lands the wrong content: only reading the file back
# can catch it, and the conf must be restored from the backup.
plain_conf; snapshot
BREAK=mv-empty; repair; BREAK=""
unchanged && ok "silently wrong write: conf restored from .sa02m-gwbak" || { bad "silently wrong write: conf left corrupted"; diff "$T/before" "$C"; }
logged_ok && bad "silently wrong write: logged OK on an unverified write" || ok "silently wrong write: no success log"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "silently wrong write: no live-route flag (no head naming an absent gateway)" || bad "silently wrong write: set the live-route flag"
warned && ok "silently wrong write: WARN logged" || bad "silently wrong write: silent"

# The gateway and dns halves of the read-back must each stand alone: on a
# fixture where BOTH are written, either check catches a wrong write, so neither
# is individually pinned. These two fixtures write exactly one key each.
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask 255.255.255.0\n    dns-nameservers 8.8.8.8\n' > "$C"
snapshot
BREAK=mv-empty; repair; BREAK=""
unchanged && ok "wrong write, gateway-only repair: conf restored" || bad "wrong write, gateway-only repair: conf left corrupted"
logged_ok && bad "wrong write, gateway-only repair: logged OK" || ok "wrong write, gateway-only repair: no success log"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "wrong write, gateway-only repair: no live-route flag" || bad "wrong write, gateway-only repair: set the flag"

printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask 255.255.255.0\n    gateway 192.168.1.1\n' > "$C"
snapshot
BREAK=mv-empty; repair; BREAK=""
unchanged && ok "wrong write, dns-only repair: conf restored" || bad "wrong write, dns-only repair: conf left corrupted"
logged_ok && bad "wrong write, dns-only repair: logged OK" || ok "wrong write, dns-only repair: no success log"

# Double fault: the write lands wrong AND the rollback fails. The log must not
# claim the conf was restored — at this point its state is genuinely unknown, and
# the message is the only thing a human has to finish the job by hand.
plain_conf; snapshot
cp_calls=0; BREAK="cp-2nd mv-empty"; repair; BREAK=""
logged_ok && bad "failed rollback: logged OK" || ok "failed rollback: no success log"
[ "$GW_REPAIR_ADDED_GW" = 0 ] && ok "failed rollback: no live-route flag" || bad "failed rollback: set the live-route flag"
printf '%s' "$LOGGED" | grep -q 'восстановлен из' && bad "failed rollback: claims the conf was restored when it was not" || ok "failed rollback: does not claim a restore that did not happen"
printf '%s' "$LOGGED" | grep -q 'НЕОПРЕДЕЛЁННОЕ' && ok "failed rollback: reports an indeterminate config" || { bad "failed rollback: does not report the indeterminate state"; printf '%s' "$LOGGED"; }
printf '%s' "$LOGGED" | grep -qF "$C.sa02m-gwbak" && ok "failed rollback: names the backup path for manual recovery" || bad "failed rollback: does not name the backup path"
printf '%s' "$LOGGED" | grep -qF "$C:" && ok "failed rollback: names the conf path" || bad "failed rollback: does not name the conf path"
rm -f "$C.sa02m-gwbak"

# Control: with nothing stubbed the same fixture still repairs, so the four
# cases above are not passing because the fixture stopped working.
plain_conf; repair
[ "$GW_REPAIR_ADDED_GW" = 1 ] && ok "control: the same fixture repairs normally when nothing is broken" || bad "control failed — the injection cases are vacuous"

# Mode/owner must survive the replacement. Only assertable where the filesystem
# honours mode bits (Windows checkouts do not) — same idiom as the uboot harness.
plain_conf
chmod 640 "$C" 2>/dev/null || true
if [ "$(stat -c '%a' "$C" 2>/dev/null)" = "640" ]; then
    repair
    [ "$(stat -c '%a' "$C" 2>/dev/null)" = "640" ] && ok "file mode preserved across the atomic replace" || bad "replacement changed the file mode"
else
    printf 'note  mode preservation not assertable here (filesystem ignores mode bits)\n'
fi
chmod 644 "$C" 2>/dev/null || true

echo "── 14. sa02m_write_resolvconf_head (scripts/lib.sh) ──"
# The second half of the same defect: 01-system.sh derived the head's nameserver
# from `ip route show default`, and on an EMPTY result simply skipped the branch
# — so a head naming a gateway that no longer exists survived silently. It is the
# FIRST nameserver, so every lookup then pays its timeout.
# Retarget the head onto the scratch tree and stub `resolvconf` so the function's
# own presence guard passes on a dev box that has none.
SA02M_RESOLVCONF_HEAD="$T/resolvconf/head"
H="$SA02M_RESOLVCONF_HEAD"

rm -rf "$T/resolvconf"
LOGGED=""; sa02m_write_resolvconf_head 192.168.1.1
grep -q '^nameserver 192\.168\.1\.1$' "$H" 2>/dev/null && ok "head written with the gateway as first nameserver" || bad "head not written"
grep -qF "$SA02M_RESOLVCONF_HEAD_MARK" "$H" 2>/dev/null && ok "head carries our marker comment" || bad "head has no marker (a later run could not recognise it as ours)"

LOGGED=""; sa02m_write_resolvconf_head ""
[ ! -f "$H" ] && ok "no default route: our stale head is removed" || bad "stale head survived (every lookup pays its timeout)"
warned && ok "stale-head removal is logged, not silent" || bad "stale head removed silently"

mkdir -p "$(dirname "$H")"
printf '# hand-written by the operator\nnameserver 10.0.0.53\n' > "$H"
command cp "$H" "$T/foreign.head"
LOGGED=""; sa02m_write_resolvconf_head ""
cmp -s "$T/foreign.head" "$H" && ok "a head we did not write is never touched" || bad "removed a foreign resolvconf head"
warned && bad "WARNed about a foreign head it correctly left alone" || ok "foreign head produces no diagnosis"

echo "---"
if [ "$fails" = 0 ]; then echo "iface-gw-repair: all checks passed"; else echo "iface-gw-repair: $fails check(s) FAILED"; fi
exit "$fails"
