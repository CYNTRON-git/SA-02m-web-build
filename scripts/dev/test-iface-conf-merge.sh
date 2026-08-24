#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-iface-conf-merge.sh — regression test for apply.cgi's interfaces.d
# foreign-stanza merge (docs/contracts/ethernet-iface-naming.md §5).
#
# Why this exists: the merge is Bash inside a CGI, so no runner in this repo
# reaches it, and a future edit to apply.cgi could silently re-break vendor-hook
# preservation with every syntax/static gate still green. This is the functional
# half of that contract's gate (the static half is
# .ai-dev/quality/checks/iface-naming-contract.sh).
#
# Method: extract the SHIPPED functions from apply.cgi and retarget the two
# absolute roots (/sys/class/net, /var/log/sa02m_install.log) plus the pinned
# root-write helper (sa02m-iface-conf-write.sh, audit B1) into a scratch tree, so
# the logic under test is byte-for-byte what runs on the device. `sudo` (incl.
# its `-n` flag), the pinned write helper and the lib_net_iface helpers are
# stubbed; nothing touches the real system.
#
# Run: bash scripts/dev/test-iface-conf-merge.sh   (stdlib bash only, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

APPLY=www/network_config/cgi-bin/apply.cgi
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
ND="$T/net"; CD="$T/ifd"; mkdir -p "$ND" "$CD"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

sed -n '/^PRESERVE_MAX_LINES=/,/^timeout_run() {/p' "$APPLY" \
  | sed '$d' \
  | sed "s#/sys/class/net/#$ND/#g; s#/var/log/sa02m_install.log#$T/install.log#g; s#/usr/local/sbin/sa02m-iface-conf-write.sh#iface_conf_write_stub#g" > "$T/fn.sh"
if ! grep -q '^write_lan_pair()' "$T/fn.sh"; then
    echo "FAIL  could not extract the merge functions from $APPLY (did the section markers move?)"
    exit 1
fi

# sudo shim eats the `-n` flag (the pinned-helper call carries it); the pinned
# root-write helper is stubbed to a plain stdin->dest write. This isolates the
# MERGE layer (ordering / bounds / dedup / sibling rule) under test here; the
# helper's OWN guards — the dest-path allow-list, symlink refusal, and the
# CONTENT allow-list that rejects a planted root-exec hook (audit B1 round 2) —
# are exercised end-to-end by .ai-dev/quality/checks/sudoers-pin-contract.sh.
# (So a synthetic foreign line like `post-up KEEPME` is preserved by the merge
# here but WOULD be rejected by the real content validator; real confs carry only
# the allow-listed klogic / default-route hooks.)
sudo() { [ "${1:-}" = "-n" ] && shift; "$@"; }
iface_conf_write_stub() { cat > "$1"; }
lan_iface_conf() { printf '%s/%s.conf' "$CD" "$1"; }
lan_iface_sibling() {
    case "$1" in eth0) printf end0 ;; end0) printf eth0 ;; eth1) printf end1 ;; end1) printf eth1 ;; *) printf '' ;; esac
}
schedule_iface_apply() { APPLIED="${APPLIED:-} $1${2:+:$2}"; }
# shellcheck disable=SC1090
. "$T/fn.sh"

HOOK='post-up /home/klogic/adjust-eth0'
C="$CD/eth0.conf"

# A vendor (KLogic) conf: content in BOTH preservation buckets — comment and
# `no-auto-down` before the stanza, hwaddress/post-up inside it.
klogic_conf() { cat > "$1" <<'EOF'
# Wired adapter #1
allow-hotplug eth0
no-auto-down eth0
iface eth0 inet static
address 192.168.0.136
netmask 255.255.255.0
#gateway 192.168.0.1
#	hwaddress ether 02:53:8B:00:D4:30 # if you want to set MAC manually
	post-up /home/klogic/adjust-eth0 || /bin/true
EOF
}

echo "── preservation ──"
klogic_conf "$C"; APPLIED=""
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 192.168.1.137" "    netmask 255.255.255.0" "    gateway 192.168.1.1"
grep -q "$HOOK" "$C" && ok "vendor post-up hook preserved" || bad "vendor post-up hook lost"
[ "$(grep -c "	$HOOK" "$C")" = 1 ] && ok "post-up preserved verbatim (leading TAB kept)" || bad "post-up whitespace rewritten"
grep -q '^no-auto-down eth0$' "$C" && ok "pre-stanza option preserved" || bad "no-auto-down lost"
grep -q '^# Wired adapter #1$' "$C" && ok "leading comment preserved" || bad "leading comment lost"
grep -q 'hwaddress ether 02:53:8B:00:D4:30' "$C" && ok "commented in-stanza line preserved" || bad "commented hwaddress lost"
grep -q '^    address 192.168.1.137$' "$C" && ok "managed address applied" || bad "managed address not applied"
if grep -qx 'address 192.168.0.136' "$C"; then bad "stale managed address leaked"; else ok "stale managed address replaced"; fi
[ "$(grep -c '^auto eth0$' "$C")" = 1 ] && ok "exactly one stanza header" || bad "header duplicated or missing"
awk 'BEGIN{p=0;e=0}
     /^# Wired adapter #1$/{p=1}
     /^auto eth0$/{if(p!=1)e=1;p=2}
     /^iface eth0 inet static$/{if(p!=2)e=1;p=3}
     /^    gateway/{if(p!=3)e=1;p=4}
     /adjust-eth0/{if(p!=4)e=1;p=5}
     END{exit e}' "$C" \
  && ok "write order: pre-foreign / header / iface / managed / in-foreign" || bad "write order wrong"

echo "── idempotency ──"
cp "$C" "$T/first"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 192.168.1.137" "    netmask 255.255.255.0" "    gateway 192.168.1.1"
if diff -q "$T/first" "$C" >/dev/null; then ok "second save is byte-identical"; else bad "second save differs"; diff "$T/first" "$C"; fi

echo "── hand-edited header spellings are still recognised as ours ──"
# A double-spaced or indented header must be REGENERATED, not preserved as a
# foreign line — otherwise the file ends up declaring the interface twice.
printf 'auto  eth0\n  allow-hotplug eth0\niface   eth0 inet static\n    address 1.1.1.1\n    post-up KEEPME\n' > "$C"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 10.0.0.6" "    netmask 255.255.255.0"
[ "$(grep -cE '^[[:space:]]*(auto|allow-hotplug)[[:space:]]+eth0' "$C")" = 1 ] \
  && ok "double-spaced/indented headers collapse to one regenerated header" || bad "duplicate stanza header emitted"
grep -q 'KEEPME' "$C" && ok "foreign line still preserved past a loose header" || bad "foreign line lost"
grep -q '^    address 10.0.0.6$' "$C" && ok "loose 'iface   eth0 inet' recognised as our stanza" || bad "loose iface line not recognised"

echo "── no behaviour change on a plain board ──"
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.136\n    netmask 255.255.255.0\n    gateway 192.168.1.1\n    dns-nameservers 77.88.8.8\n' > "$C"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 192.168.1.99" "    netmask 255.255.255.0"
printf 'auto eth0\niface eth0 inet static\n    address 192.168.1.99\n    netmask 255.255.255.0\n' > "$T/expect"
if diff -q "$T/expect" "$C" >/dev/null; then ok "conf with no foreign lines regenerates exactly as before"; else bad "plain conf changed"; diff "$T/expect" "$C"; fi

echo "── malformed input falls back safely ──"
rm -f "$C.sa02m-bak"; printf '# hand-edited junk\nsomething odd\n' > "$C"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 10.0.0.1" "    netmask 255.255.255.0"
if [ -f "$C.sa02m-bak" ] && grep -q 'something odd' "$C.sa02m-bak"; then ok "no iface stanza -> backed up"; else bad "no backup for a malformed conf"; fi
if grep -q 'something odd' "$C"; then bad "malformed content merged forward"; else ok "no iface stanza -> clean regeneration"; fi
rm -f "$C.sa02m-bak"
printf 'auto eth0\niface eth0 inet static\n    address 1.1.1.1\n    netmask 255.255.255.0\niface eth0 inet6 auto\n' > "$C"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 10.0.0.2" "    netmask 255.255.255.0"
[ -f "$C.sa02m-bak" ] && ok "duplicate stanza -> backed up" || bad "no backup for a duplicate stanza"
if grep -q 'inet6' "$C"; then bad "second stanza merged"; else ok "duplicate stanza -> clean regeneration"; fi

echo "── bounds ──"
{ printf 'auto eth0\niface eth0 inet static\n    address 1.1.1.1\n'
  i=1; while [ "$i" -le 200 ]; do printf '    post-up /bin/true # %s\n' "$i"; i=$((i + 1)); done; } > "$C"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 10.0.0.3" "    netmask 255.255.255.0"
n=$(grep -c 'post-up /bin/true' "$C")
[ "$n" -le 64 ] && ok "line-count bound honoured ($n of 200 kept, cap 64)" || bad "line-count bound broken ($n)"
LONG=$(printf 'x%.0s' $(seq 1 600))
printf 'auto eth0\niface eth0 inet static\n    address 1.1.1.1\n    post-up %s\n' "$LONG" > "$C"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 10.0.0.4" "    netmask 255.255.255.0"
if grep -q "$LONG" "$C"; then bad "over-long line kept"; else ok "over-long line dropped (cap 512)"; fi
printf 'auto eth0\r\niface eth0 inet static\r\n    address 1.1.1.1\r\n    post-up KEEPME\r\n    post-up CTRL\001BAD\n' > "$C"
write_lan_pair eth0 end0 eth0 "$C" auto static "    address 10.0.0.5" "    netmask 255.255.255.0"
if grep -q $'\r' "$C"; then bad "CR survived"; else ok "CR stripped"; fi
grep -q 'KEEPME' "$C" && ok "clean foreign line kept alongside a rejected one" || bad "clean foreign line lost"
if grep -q 'CTRL' "$C"; then bad "control-char line kept"; else ok "control-char line dropped"; fi

echo "── sibling rule (the migration-undoing bug this replaces) ──"
rm -f "$CD"/*.conf "$CD"/*.sa02m-bak
klogic_conf "$CD/end0.conf"; sed -i 's/eth0/end0/g' "$CD/end0.conf"
mkdir -p "$ND/end0"; APPLIED=""
write_lan_pair eth0 end0 end0 "$CD/end0.conf" auto static "    address 192.168.1.50" "    netmask 255.255.255.0"
grep -q '^auto eth0$' "$CD/eth0.conf" && ok "canonical conf written on a legacy board" || bad "canonical conf not written"
grep -q '^allow-hotplug end0$' "$CD/end0.conf" && ok "legacy mirror is allow-hotplug (ifup -a skips it post-rename)" || bad "legacy mirror header wrong"
if grep -q '^auto end0$' "$CD/end0.conf"; then bad "legacy mirror kept auto"; else ok "legacy auto replaced"; fi
if grep -q 'adjust-end0' "$CD/eth0.conf" && grep -q 'adjust-end0' "$CD/end0.conf"; then ok "both confs carry identical foreign content"; else bad "foreign content differs between the pair"; fi
[ "$APPLIED" = " end0" ] && ok "bounces the live legacy interface" || bad "bounced the wrong interface ($APPLIED)"
rmdir "$ND/end0"; APPLIED=""
write_lan_pair eth0 end0 eth0 "$CD/eth0.conf" auto static "    address 192.168.1.51" "    netmask 255.255.255.0"
if grep -qE '^[[:space:]]*(auto|allow-|iface)' "$CD/end0.conf"; then bad "retired legacy conf still declares a stanza"; else ok "legacy conf retired (stanza-free, ifup -a ignores it)"; fi
[ -f "$CD/end0.conf.sa02m-bak" ] && ok "legacy conf backed up before retirement" || bad "retirement without a backup"
[ "$APPLIED" = " eth0" ] && ok "bounces the canonical interface once migrated" || bad "bounced the wrong interface ($APPLIED)"
grep -q 'address 192.168.1.51' "$CD/eth0.conf" && ok "canonical conf still updated" || bad "canonical conf not updated"
cp "$CD/end0.conf.sa02m-bak" "$T/bak1"
write_lan_pair eth0 end0 eth0 "$CD/eth0.conf" auto static "    address 192.168.1.51" "    netmask 255.255.255.0"
if diff -q "$T/bak1" "$CD/end0.conf.sa02m-bak" >/dev/null; then ok "retirement is idempotent (backup not churned)"; else bad "re-save churned the retirement backup"; fi

echo "── sa02m-conf-rm helper (plan §5: refusal surface, portably) ──"
# The helper's write/delete good-path and the symlink refusal need a root-owned
# /etc/network/interfaces.d and were exercised on the bench (2026-07-29);
# portably we pin the REFUSAL surface (no side effects — validation precedes
# any write) plus the structural invariants of the sudoers capability.
HLP="etc/sa02m-conf-rm.sh"   # cwd = repo root (cd at the top of this harness)
bash "$HLP" /etc/passwd                                   >/dev/null 2>&1; [ $? -eq 2 ] && ok "refuses non-allow-listed path" || bad "accepted /etc/passwd"
bash "$HLP" '/etc/network/interfaces.d/../../passwd'      >/dev/null 2>&1; [ $? -eq 2 ] && ok "refuses traversal" || bad "accepted traversal"
bash "$HLP" /etc/network/interfaces.d/eth2.conf           >/dev/null 2>&1; [ $? -eq 2 ] && ok "refuses eth2.conf (not in the 4-name list)" || bad "accepted eth2.conf"
bash "$HLP" ''                                            >/dev/null 2>&1; [ $? -eq 2 ] && ok "refuses empty argument" || bad "accepted empty argument"
if [ ! -e /etc/network/interfaces.d/eth1.conf ]; then
    bash "$HLP" /etc/network/interfaces.d/eth1.conf       >/dev/null 2>&1; [ $? -eq 0 ] && ok "absent allow-listed conf: idempotent exit 0" || bad "absent conf not idempotent"
else
    ok "absent-conf case skipped (real conf present on this host)"
fi
grep -q 'case "\$conf" in' "$HLP" && ok "validation is a literal case (no regex/glob)" || bad "case validation missing"
grep -q -- '-L "\$conf"' "$HLP" && ok "symlink refusal present before rm" || bad "symlink refusal missing"
awk '/cp -f "\$conf"/{seen=1} /rm -f "\$conf"/{if(!seen){exit 1}}' "$HLP" && ok "backup precedes rm" || bad "rm without preceding backup"

echo "---"
if [ "$fails" = 0 ]; then echo "iface-conf-merge: all checks passed"; else echo "iface-conf-merge: $fails check(s) FAILED"; fi
exit "$fails"

