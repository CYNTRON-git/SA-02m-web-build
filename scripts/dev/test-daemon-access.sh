#!/usr/bin/env bash
# test-daemon-access.sh — behavioural harness for usr/local/sbin/sa02m-daemon-access.sh
# (quality row `daemon-access-effect`), the least-privilege grant of the HomeKit
# bridge and the Home Connect client and its install-time / unit-start EFFECT
# check (docs/contracts/homekit-bridge.md §13, docs/contracts/home-connect.md §11).
#
# Why a harness and not a grep: the grant this replaced (POSIX ACLs) passed
# every line check and granted NOTHING on the product RT kernel (no ext4 ACL,
# bench 1.135, 2026-09-28) — the bridge died reading its own conf. Only running
# the access as the daemon proves it. So the SHIPPED helper runs here, every
# absolute path re-pointed into a sandbox of REAL files with real owners and
# modes, and every probe is a real `setpriv` to a non-root uid: nothing the
# kernel does is mocked. What is shimmed is name resolution only — `id` and
# `getent` map sa02m-homekit / sa02m-homeconnect / www-data /
# sa02m-alice-devices to throwaway numeric ids (61001/61002/61033/61003), so
# no account is created on the host.
#
#   A  non-vacuity: the helper exists, the retargeted copy names no absolute
#      board path any more, the shims answer, setpriv exists.
#   B  HomeKit, the new layout: `check` exits 0 with the success line and no
#      ОШИБКА; so does Home Connect.
#   C  HomeKit, the 9cf41b5 layout (the bench blocker: conf dir root:www-data
#      0770, conf and device document 660 root:www-data, the bridge out of
#      www-data): `check` exits 1 and names the conf AND the device document —
#      never the success line. Home Connect likewise (its conf).
#   D  the da06c5c layout (the daemon IN www-data): `check` exits 1 on the
#      membership and on the over-grant (it reads /etc/sa02m_web.env).
#   E  `apply` heals the 9cf41b5 layout, idempotently: conf dir 61033:61001
#      2750, conf 61033:61001 0640, device document 61033:61003 0640, the Alice
#      dir gains o+x, bytes untouched; a second `apply` changes nothing; the
#      bridge then reads its files and still cannot read the client/server
#      Alice confs or the panel credentials. Home Connect likewise.
#   F  the writers after `apply`, run AS www-data (setpriv): the HomeKit and the
#      Home Connect package's own config.save (atomic rename) and the Alice
#      writer (_atomic_write) on the device document — every file stays
#      readable by its daemon (group from the setgid dir / kept by the Alice
#      writer), `check` still exits 0. The Alice writer run as www-data OUTSIDE
#      sa02m-alice-devices hands the document back to group www-data and
#      `check` FAILS: why www-data is a member of that group.
#   G  a symlink planted at the conf (the dir is www-data's): `apply` never
#      follows it — the victim's owner and mode are untouched, an ОШИБКА is
#      printed and the exit is 1.
#   H  sa02m-alice-devices missing: `apply` exits 1 naming the group.
#   I  `apply` never hands a foreign file to www-data: a root:root 0600 file
#      renamed onto the conf or the device-document name, or a file of any
#      other owner, is refused with ОШИБКА and a non-zero exit, owner/mode kept.
#   J  a hard link (to a root:www-data 0640 file — the owner rule would take
#      it; only the nlink check stops it) and a FIFO at either name: `apply`
#      refuses both, `check` fails on the FIFO without hanging.
#   K  Home Connect's deny list: a world-readable device document FAILS
#      `check homeconnect`.
#   L  a NEW conf saved by www-data takes the setgid dir's group, checks pass.
#
# Root is required (chown to foreign uids, setpriv). Not root: re-exec through
# `sudo -n` where that works (the CI runner), else every case is printed as
# SKIP and the run exits 0 — a SKIP is not a pass (quality-gate-rigor.md).
# DAEMON_ACCESS_SRC=<copy> judges another copy of the helper (the RED recipe).
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SRC=${DAEMON_ACCESS_SRC:-$ROOT/usr/local/sbin/sa02m-daemon-access.sh}
HK_TMPF=$ROOT/opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf
HC_TMPF=$ROOT/opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf
HK_PKG=$ROOT/opt/sa02m-homekit
HC_PKG=$ROOT/opt/sa02m-homeconnect
AL_PKG=$ROOT/opt/sa02m-alice

if [ "$(id -u)" != 0 ]; then
    if [ -z "${DAEMON_ACCESS_NO_SUDO:-}" ] && command -v sudo >/dev/null 2>&1 \
       && sudo -n true 2>/dev/null; then
        exec sudo -n env DAEMON_ACCESS_NO_SUDO=1 ${DAEMON_ACCESS_SRC:+DAEMON_ACCESS_SRC="$DAEMON_ACCESS_SRC"} \
            PATH="$PATH" bash "${BASH_SOURCE[0]}"
    fi
    for c in A B C D E F G H I J K L; do echo "SKIP  $c needs root (chown to foreign uids, setpriv) — not run (a skip is not a pass)"; done
    echo "test-daemon-access: SKIPPED, NOT PASSED (not root, no sudo -n)"
    exit 0
fi

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

SB=$(mktemp -d) && [ -n "$SB" ] && [ -d "$SB" ] \
    || { echo "FAIL  A mktemp -d gave no sandbox directory — nothing can run"; exit 1; }
trap 'rm -rf "$SB"' EXIT
chmod 0755 "$SB"

# ── A: non-vacuity ─────────────────────────────────────────────────────────
for f in "$SRC" "$HK_TMPF" "$HC_TMPF" "$HK_PKG/sa02m_homekit/config.py" \
         "$HC_PKG/sa02m_homeconnect/config.py" "$AL_PKG/sa02m_alice/common/config_store.py"; do
    [ -f "$f" ] || { bad "A missing $f — nothing to judge"; echo "test-daemon-access: $fails FAILURE(S)"; exit 1; }
done
command -v setpriv >/dev/null 2>&1 || { bad "A setpriv (util-linux) missing — the probes cannot run"; exit 1; }

retarget() {  # $1 = src, $2 = dst, $3 = sandbox root
    sed -e "s#/etc/#$3/etc/#g" -e "s#/var/lib/#$3/var/lib/#g" \
        -e "s#/run/#$3/run/#g" -e "s#/opt/#$3/opt/#g" "$1" > "$2"
}

# Accounts: name uid gid groups(space-separated, numeric). Groups: name gid.
write_ids() {  # $1 = sandbox, $2 = daemon-in-www-data (0|1), $3 = www-data-in-devdoc (0|1), $4 = devdoc group exists (0|1)
    local hkg="61001 61003" wg="61033"
    [ "$4" = 1 ] || hkg="61001"
    [ "$2" = 1 ] && hkg="$hkg 61033"
    [ "$3" = 1 ] && [ "$4" = 1 ] && wg="61033 61003"
    local hcg="61002"
    [ "$2" = 1 ] && hcg="61002 61033"
    {
        echo "sa02m-homekit 61001 61001 $hkg"
        echo "sa02m-homeconnect 61002 61002 $hcg"
        echo "www-data 61033 61033 $wg"
        echo "root 0 0 0"
    } > "$1/accounts"
    {
        echo "sa02m-homekit 61001"
        echo "sa02m-homeconnect 61002"
        echo "www-data 61033"
        echo "root 0"
        [ "$4" = 1 ] && echo "sa02m-alice-devices 61003"
    } > "$1/groups"
}

mk_shims() {  # $1 = sandbox
    mkdir -p "$1/shim"
    cat > "$1/shim/id" <<EOF
#!/bin/bash
# id -u|-g|-G NAME from the sandbox table; anything else: refuse.
flag=\$1 name=\${2:-}
while read -r n u g grps; do
    [ "\$n" = "\$name" ] || continue
    case "\$flag" in
        -u) echo "\$u" ;; -g) echo "\$g" ;; -G) echo "\$grps" ;;
        -nG) for x in \$grps; do while read -r gn gi; do [ "\$gi" = "\$x" ] && printf '%s ' "\$gn"; done < "$1/groups"; done; echo ;;
        *) exit 2 ;;
    esac
    exit 0
done < "$1/accounts"
echo "id: '\$name': no such user" >&2
exit 1
EOF
    cat > "$1/shim/getent" <<EOF
#!/bin/bash
[ "\$1" = group ] || exit 2
while read -r n gi; do
    [ "\$n" = "\$2" ] && { echo "\$n:x:\$gi:"; exit 0; }
done < "$1/groups"
exit 2
EOF
    chmod 0755 "$1/shim/id" "$1/shim/getent"
}

# A board tree. $2 = layout: new | acl9cf | www06c
mk_tree() {
    local r=$1 layout=$2
    mkdir -p "$r/etc" "$r/var/lib" "$r/run" "$r/opt/sa02m-homekit/tmpfiles.d" "$r/opt/sa02m-homeconnect/tmpfiles.d"
    chmod 0755 "$r" "$r/etc" "$r/var" "$r/var/lib" "$r/run" "$r/opt" "$r/opt/sa02m-homekit" \
        "$r/opt/sa02m-homekit/tmpfiles.d" "$r/opt/sa02m-homeconnect" "$r/opt/sa02m-homeconnect/tmpfiles.d"
    retarget "$HK_TMPF" "$r/opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf" "$r"
    retarget "$HC_TMPF" "$r/opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf" "$r"
    mkdir -p "$r/etc/sa02m-homekit" "$r/etc/sa02m-homeconnect" "$r/etc/sa02m-alice" \
             "$r/var/lib/sa02m-homekit" "$r/var/lib/sa02m-homeconnect" "$r/run/sa02m-homekit" "$r/run/sa02m-homeconnect"
    printf '[bridge]\nenabled = true\ninterface = eth0\nport = 21064\n' > "$r/etc/sa02m-homekit/sa02m-homekit.conf"
    printf '[account]\nenabled = true\nclient_id =\nhost = api\nlink_requested_at = 0\n' > "$r/etc/sa02m-homeconnect/sa02m-homeconnect.conf"
    printf '{"rooms": [], "groups": [], "devices": []}\n' > "$r/etc/sa02m-alice/sa02m-alice-devices.conf"
    printf '[client]\nclient_enabled = false\n' > "$r/etc/sa02m-alice/sa02m-alice-client.conf"
    printf '[gateway]\n' > "$r/etc/sa02m-alice/sa02m-alice-server.conf"
    printf 'WEB_PASS=secret\n' > "$r/etc/sa02m_web.env"
    chown 0:61033 "$r/etc/sa02m-alice/sa02m-alice-client.conf" "$r/etc/sa02m-alice/sa02m-alice-server.conf" "$r/etc/sa02m_web.env"
    chmod 0660 "$r/etc/sa02m-alice/sa02m-alice-client.conf"
    chmod 0640 "$r/etc/sa02m-alice/sa02m-alice-server.conf" "$r/etc/sa02m_web.env"
    chown 61001:61001 "$r/var/lib/sa02m-homekit"; chmod 0700 "$r/var/lib/sa02m-homekit"
    chown 61002:61002 "$r/var/lib/sa02m-homeconnect"; chmod 0700 "$r/var/lib/sa02m-homeconnect"
    chown 61001:61033 "$r/run/sa02m-homekit"; chmod 2750 "$r/run/sa02m-homekit"
    chown 61002:61033 "$r/run/sa02m-homeconnect"; chmod 2750 "$r/run/sa02m-homeconnect"
    if [ "$layout" = new ]; then
        chown 61033:61001 "$r/etc/sa02m-homekit"; chmod 2750 "$r/etc/sa02m-homekit"
        chown 61033:61002 "$r/etc/sa02m-homeconnect"; chmod 2750 "$r/etc/sa02m-homeconnect"
        chown 61033:61001 "$r/etc/sa02m-homekit/sa02m-homekit.conf"; chmod 0640 "$r/etc/sa02m-homekit/sa02m-homekit.conf"
        chown 61033:61002 "$r/etc/sa02m-homeconnect/sa02m-homeconnect.conf"; chmod 0640 "$r/etc/sa02m-homeconnect/sa02m-homeconnect.conf"
        chown 0:61033 "$r/etc/sa02m-alice"; chmod 0771 "$r/etc/sa02m-alice"
        chown 61033:61003 "$r/etc/sa02m-alice/sa02m-alice-devices.conf"; chmod 0640 "$r/etc/sa02m-alice/sa02m-alice-devices.conf"
    else
        # 9cf41b5 and da06c5c: root:www-data 0770 dirs, 660 root:www-data files.
        chown 0:61033 "$r/etc/sa02m-homekit" "$r/etc/sa02m-homeconnect" "$r/etc/sa02m-alice"
        chmod 0770 "$r/etc/sa02m-homekit" "$r/etc/sa02m-homeconnect" "$r/etc/sa02m-alice"
        chown 0:61033 "$r/etc/sa02m-homekit/sa02m-homekit.conf" "$r/etc/sa02m-homeconnect/sa02m-homeconnect.conf" \
            "$r/etc/sa02m-alice/sa02m-alice-devices.conf"
        chmod 0660 "$r/etc/sa02m-homekit/sa02m-homekit.conf" "$r/etc/sa02m-homeconnect/sa02m-homeconnect.conf" \
            "$r/etc/sa02m-alice/sa02m-alice-devices.conf"
    fi
}

# One sandbox per case: tree + ids + shims + retargeted helper.
new_case() {  # $1 = name, $2 = layout, $3 daemon-in-www, $4 www-in-devdoc, $5 devdoc-exists
    local c=$SB/$1
    mkdir -p "$c"; chmod 0755 "$c"
    mk_tree "$c" "$2"
    write_ids "$c" "$3" "$4" "$5"
    mk_shims "$c"
    retarget "$SRC" "$c/helper.sh" "$c"
    echo "$c"
}
run_helper() {  # $1 = case dir, $2 = verb, $3 = kind ; prints output, returns rc
    PATH="$1/shim:$PATH" timeout 120 bash "$1/helper.sh" "$2" "$3" 2>&1
}
mode_of() { stat -c '%u:%g %a' "$1"; }

C=$(new_case a new 0 1 1)
if grep -nE '(^|[^A-Za-z0-9_.-])/(etc|var/lib|run|opt)/' "$C/helper.sh" | grep -v "$C" >/dev/null; then
    bad "A the retargeted helper still names an absolute board path: $(grep -nE '(^|[^A-Za-z0-9_.-])/(etc|var/lib|run|opt)/' "$C/helper.sh" | grep -v "$C" | head -3)"
else ok "A the retargeted helper names no board path outside the sandbox"; fi
if [ "$(PATH="$C/shim:$PATH" id -u sa02m-homekit)" = 61001 ] \
   && [ "$(PATH="$C/shim:$PATH" getent group sa02m-alice-devices)" = "sa02m-alice-devices:x:61003:" ]; then
    ok "A the id/getent shims answer from the sandbox table"
else bad "A the shims do not answer — every later case would judge the host's accounts"; fi

# ── B: the new layout passes ───────────────────────────────────────────────
for kind in homekit homeconnect; do
    rc=0; out=$(run_helper "$C" check "$kind") || rc=$?
    if [ "$rc" = 0 ] && grep -q '^доступ sa02m-' <<<"$out" && ! grep -q 'ОШИБКА' <<<"$out"; then
        ok "B $kind: the new layout passes check ($(grep -m1 '^доступ' <<<"$out"))"
    else bad "B $kind: the new layout: rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-400)"; fi
done

# ── C: the 9cf41b5 layout (the bench blocker) fails, naming the files ──────
C=$(new_case c acl9cf 0 1 1)
rc=0; out=$(run_helper "$C" check homekit) || rc=$?
if [ "$rc" = 1 ] && grep -q "sa02m-homekit не может читать $C/etc/sa02m-homekit/sa02m-homekit.conf" <<<"$out" \
   && grep -q "sa02m-homekit не может читать $C/etc/sa02m-alice/sa02m-alice-devices.conf" <<<"$out" \
   && ! grep -q '^доступ ' <<<"$out"; then
    ok "C homekit: 660 root:www-data FAILS check (exit 1), naming the conf and the device document, no success line"
else bad "C homekit: 660 root:www-data: rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-500)"; fi
rc=0; out=$(run_helper "$C" check homeconnect) || rc=$?
if [ "$rc" = 1 ] && grep -q "sa02m-homeconnect не может читать $C/etc/sa02m-homeconnect/sa02m-homeconnect.conf" <<<"$out" \
   && ! grep -q '^доступ ' <<<"$out"; then
    ok "C homeconnect: 660 root:www-data FAILS check (exit 1), naming the conf"
else bad "C homeconnect: 660 root:www-data: rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-500)"; fi

# ── D: the da06c5c layout (daemon in www-data) fails on membership ─────────
C=$(new_case d www06c 1 1 1)
rc=0; out=$(run_helper "$C" check homekit) || rc=$?
if [ "$rc" = 1 ] && grep -q 'sa02m-homekit состоит в группе www-data' <<<"$out" \
   && grep -q "sa02m-homekit может читать $C/etc/sa02m_web.env — лишний доступ" <<<"$out"; then
    ok "D homekit in www-data FAILS check: the membership and the read of the panel credentials are named"
else bad "D homekit in www-data: rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-500)"; fi

# ── E: apply heals the 9cf41b5 layout, idempotently ────────────────────────
C=$(new_case e acl9cf 0 1 1)
before=$(cksum < "$C/etc/sa02m-homekit/sa02m-homekit.conf")$(cksum < "$C/etc/sa02m-alice/sa02m-alice-devices.conf")
rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
after=$(cksum < "$C/etc/sa02m-homekit/sa02m-homekit.conf")$(cksum < "$C/etc/sa02m-alice/sa02m-alice-devices.conf")
got="$(mode_of "$C/etc/sa02m-homekit")|$(mode_of "$C/etc/sa02m-homekit/sa02m-homekit.conf")|$(mode_of "$C/etc/sa02m-alice/sa02m-alice-devices.conf")|$(stat -c '%a' "$C/etc/sa02m-alice")"
want="61033:61001 2750|61033:61001 640|61033:61003 640|771"
if [ "$rc" = 0 ] && [ "$got" = "$want" ] && [ "$before" = "$after" ] && grep -q '^доступ sa02m-homekit' <<<"$out"; then
    ok "E homekit: apply heals the 9cf41b5 layout ($got), bytes untouched, check passes"
else bad "E homekit: apply: rc=$rc, got $got want $want, bytes same=$([ "$before" = "$after" ] && echo yes || echo NO), output: $(tr '\n' '|' <<<"$out" | cut -c1-400)"; fi
rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
got2="$(mode_of "$C/etc/sa02m-homekit")|$(mode_of "$C/etc/sa02m-homekit/sa02m-homekit.conf")|$(mode_of "$C/etc/sa02m-alice/sa02m-alice-devices.conf")|$(stat -c '%a' "$C/etc/sa02m-alice")"
if [ "$rc" = 0 ] && [ "$got2" = "$want" ]; then ok "E homekit: a second apply is a no-op ($got2)"
else bad "E homekit: second apply: rc=$rc, got $got2"; fi
deny=0
for f in sa02m-alice/sa02m-alice-client.conf sa02m-alice/sa02m-alice-server.conf sa02m_web.env; do
    setpriv --reuid=61001 --regid=61001 --groups=61001,61003 -- test -r "$C/etc/$f" && deny=1
done
if [ "$deny" = 0 ]; then ok "E homekit: after apply the bridge still cannot read the client/server Alice confs or the panel credentials"
else bad "E homekit: after apply the bridge reads a conf the grant must not reach"; fi
rc=0; out=$(run_helper "$C" apply homeconnect) || rc=$?
got="$(mode_of "$C/etc/sa02m-homeconnect")|$(mode_of "$C/etc/sa02m-homeconnect/sa02m-homeconnect.conf")"
if [ "$rc" = 0 ] && [ "$got" = "61033:61002 2750|61033:61002 640" ] && grep -q '^доступ sa02m-homeconnect' <<<"$out"; then
    ok "E homeconnect: apply heals the 9cf41b5 layout ($got), check passes"
else bad "E homeconnect: apply: rc=$rc, got $got, output: $(tr '\n' '|' <<<"$out" | cut -c1-400)"; fi

# ── F: the real writers, run as www-data after apply ───────────────────────
# Each package copied into the sandbox (0755) so the throwaway uid can import it.
cp -r "$HK_PKG/sa02m_homekit" "$HC_PKG/sa02m_homeconnect" "$C/"
mkdir -p "$C/alice" && cp -r "$AL_PKG/sa02m_alice" "$C/alice/"
chmod -R a+rX "$C/sa02m_homekit" "$C/sa02m_homeconnect" "$C/alice"
as_web() {  # $1 = groups, then python code
    local grps=$1; shift
    (cd "$C" && setpriv --reuid=61033 --regid=61033 --groups="$grps" -- env \
        SA02M_HOMEKIT_ETC="$C/etc/sa02m-homekit" SA02M_HOMECONNECT_ETC="$C/etc/sa02m-homeconnect" \
        PYTHONDONTWRITEBYTECODE=1 python3 -c "$1")
}
as_web 61033,61003 "
import sys; sys.path[:0] = ['$C', '$C/alice']
from sa02m_homekit import config as hk
c = hk.load(); c.port = 30000; hk.save(c)
from sa02m_homeconnect import config as hc
d = hc.load(); d.enabled = False; hc.save(d)
from sa02m_alice.common import config_store as cs
cs._atomic_write('$C/etc/sa02m-alice/sa02m-alice-devices.conf', '{\"rooms\": [], \"groups\": [], \"devices\": [1]}')
" > "$C/writers.out" 2>&1 || bad "F the writers failed as www-data: $(tr '\n' '|' < "$C/writers.out" | cut -c1-400)"
got="$(mode_of "$C/etc/sa02m-homekit/sa02m-homekit.conf")|$(mode_of "$C/etc/sa02m-homeconnect/sa02m-homeconnect.conf")|$(mode_of "$C/etc/sa02m-alice/sa02m-alice-devices.conf")"
if [ "$got" = "61033:61001 640|61033:61002 640|61033:61003 640" ] \
   && grep -q 'port = 30000' "$C/etc/sa02m-homekit/sa02m-homekit.conf" \
   && grep -q '"devices": \[1\]' "$C/etc/sa02m-alice/sa02m-alice-devices.conf"; then
    ok "F an atomic-rename save by www-data (HomeKit conf, Home Connect conf, Alice device document) keeps each daemon's group and 0640 ($got)"
else bad "F www-data saves: got $got (want 61033:61001 640|61033:61002 640|61033:61003 640)"; fi
rc=0; out=$(run_helper "$C" check homekit) || rc=$?; rc2=0; out2=$(run_helper "$C" check homeconnect) || rc2=$?
if [ "$rc" = 0 ] && [ "$rc2" = 0 ]; then ok "F after the www-data saves both checks still pass"
else bad "F after the saves: homekit rc=$rc ($(tr '\n' '|' <<<"$out" | cut -c1-300)), homeconnect rc=$rc2"; fi
as_web 61033 "
import sys; sys.path[:0] = ['$C/alice']
from sa02m_alice.common import config_store as cs
cs._atomic_write('$C/etc/sa02m-alice/sa02m-alice-devices.conf', '{\"rooms\": [], \"groups\": [], \"devices\": [2]}')
" >/dev/null 2>&1
rc=0; out=$(run_helper "$C" check homekit) || rc=$?
if [ "$(mode_of "$C/etc/sa02m-alice/sa02m-alice-devices.conf")" = "61033:61033 640" ] && [ "$rc" = 1 ] \
   && grep -q "не может читать $C/etc/sa02m-alice/sa02m-alice-devices.conf" <<<"$out"; then
    ok "F an Alice save by www-data OUTSIDE sa02m-alice-devices loses the group and check FAILS — the membership is load-bearing"
else bad "F non-member Alice save: $(mode_of "$C/etc/sa02m-alice/sa02m-alice-devices.conf"), check rc=$rc"; fi

# ── G: a planted symlink at the conf is never followed ─────────────────────
C=$(new_case g acl9cf 0 1 1)
victim=$C/victim
printf 'root-secret\n' > "$victim"; chown 0:0 "$victim"; chmod 0600 "$victim"
rm -f "$C/etc/sa02m-homekit/sa02m-homekit.conf"
ln -s "$victim" "$C/etc/sa02m-homekit/sa02m-homekit.conf"
rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
if [ "$(mode_of "$victim")" = "0:0 600" ] && [ -L "$C/etc/sa02m-homekit/sa02m-homekit.conf" ] \
   && grep -q "ОШИБКА: $C/etc/sa02m-homekit/sa02m-homekit.conf" <<<"$out" && [ "$rc" = 1 ]; then
    ok "G a symlink planted at the conf: apply refuses it (ОШИБКА), never re-modes the victim (0:0 600), rc=1"
else bad "G planted symlink: victim now $(mode_of "$victim"), rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-400)"; fi

# ── H: no sa02m-alice-devices group ────────────────────────────────────────
C=$(new_case h acl9cf 0 0 0)
rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
if [ "$rc" = 1 ] && grep -q 'группы sa02m-alice-devices нет' <<<"$out"; then
    ok "H without the sa02m-alice-devices group apply exits 1 and names it"
else bad "H missing group: rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-400)"; fi

# ── I: apply never hands a foreign file to www-data (review A1) ────────────
# www-data owns the conf dir and can rename ANY root-only file it can reach in
# a non-sticky dir onto the conf name; apply must refuse a file whose owner is
# not www-data and not root with group www-data / the target group.
for target in conf devdoc; do
    C=$(new_case "i-$target" new 0 1 1)
    case $target in
        conf)   f=$C/etc/sa02m-homekit/sa02m-homekit.conf ;;
        devdoc) f=$C/etc/sa02m-alice/sa02m-alice-devices.conf ;;
    esac
    rm -f "$f"; printf 'root-only-secret\n' > "$f"; chown 0:0 "$f"; chmod 0600 "$f"
    rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
    if [ "$(mode_of "$f")" = "0:0 600" ] && [ "$rc" != 0 ] && grep -q "ОШИБКА: $f" <<<"$out" \
       && ! grep -q '^доступ ' <<<"$out"; then
        ok "I $target: a root:root 0600 file renamed onto the name is refused (ОШИБКА, rc=$rc), owner/mode kept"
    else bad "I $target: root:root file at the name: now $(mode_of "$f"), rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-400)"; fi
done
C=$(new_case i-other new 0 1 1)
f=$C/etc/sa02m-homekit/sa02m-homekit.conf
chown 4242:4242 "$f"; chmod 0644 "$f"
rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
if [ "$(mode_of "$f")" = "4242:4242 644" ] && [ "$rc" != 0 ] && grep -q "ОШИБКА: $f" <<<"$out"; then
    ok "I other: a file of any other owner (4242:4242) is refused and reported"
else bad "I other: foreign owner: now $(mode_of "$f"), rc=$rc"; fi

# ── J: a hard link or a FIFO at the conf / document name (review A3, A4) ───
# The hard link points at a root:www-data 0640 file — the panel credentials'
# shape — which the owner rule alone would accept: only the nlink check keeps
# apply from handing it to www-data and the daemon's group.
for target in conf devdoc; do
    C=$(new_case "j-$target" new 0 1 1)
    case $target in
        conf)   f=$C/etc/sa02m-homekit/sa02m-homekit.conf ;;
        devdoc) f=$C/etc/sa02m-alice/sa02m-alice-devices.conf ;;
    esac
    victim=$C/etc/sa02m_web.env
    rm -f "$f"; ln "$victim" "$f"
    rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
    if [ "$(mode_of "$victim")" = "0:61033 640" ] && [ "$rc" != 0 ] && grep -q "ОШИБКА: $f" <<<"$out"; then
        ok "J $target: a hard link to a root:www-data file is refused — the linked file keeps 0:61033 640, rc=$rc"
    else bad "J $target: hard link: victim now $(mode_of "$victim"), rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-300)"; fi
    C=$(new_case "jf-$target" new 0 1 1)
    case $target in
        conf)   f=$C/etc/sa02m-homekit/sa02m-homekit.conf ;;
        devdoc) f=$C/etc/sa02m-alice/sa02m-alice-devices.conf ;;
    esac
    rm -f "$f"; mkfifo -m 0640 "$f"; chown 61033:61001 "$f"
    rc=0; out=$(run_helper "$C" check homekit) || rc=$?
    if [ "$rc" = 1 ] && grep -q "не может читать $f" <<<"$out" && ! grep -q '^доступ ' <<<"$out"; then
        ok "J $target: a FIFO at the name FAILS check (a daemon opening it would block), no hang"
    else bad "J $target: FIFO: check rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-300)"; fi
    rc=0; out=$(run_helper "$C" apply homekit) || rc=$?
    if [ "$rc" != 0 ] && [ -p "$f" ] && grep -q "ОШИБКА: $f" <<<"$out"; then
        ok "J $target: apply refuses the FIFO (not a regular file), no hang"
    else bad "J $target: FIFO apply: rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-300)"; fi
done

# ── K: the Home Connect deny list is enforced (review A3) ──────────────────
# A device document world-readable (0644) is something Home Connect must not
# read: check homeconnect FAILS naming it.
C=$(new_case k new 0 1 1)
chmod 0644 "$C/etc/sa02m-alice/sa02m-alice-devices.conf"
rc=0; out=$(run_helper "$C" check homeconnect) || rc=$?
if [ "$rc" = 1 ] && grep -q "sa02m-homeconnect может читать $C/etc/sa02m-alice/sa02m-alice-devices.conf — лишний доступ" <<<"$out"; then
    ok "K Home Connect reading the Alice device document FAILS check (its deny list is probed)"
else bad "K Home Connect deny list: rc=$rc, output: $(tr '\n' '|' <<<"$out" | cut -c1-300)"; fi

# ── L: a NEW conf saved by www-data takes the setgid dir's group ───────────
# (F only saves over an existing conf — the preserve path.) The real names
# WEB_USER / DAEMON_GROUP resolve on the host or not at all; either way the
# non-root writer's chown is refused and the dir's group stands.
C=$(new_case l new 0 1 1)
cp -r "$HK_PKG/sa02m_homekit" "$HC_PKG/sa02m_homeconnect" "$C/"
chmod -R a+rX "$C/sa02m_homekit" "$C/sa02m_homeconnect"
rm -f "$C/etc/sa02m-homekit/sa02m-homekit.conf" "$C/etc/sa02m-homeconnect/sa02m-homeconnect.conf"
(cd "$C" && setpriv --reuid=61033 --regid=61033 --groups=61033,61003 -- env \
    SA02M_HOMEKIT_ETC="$C/etc/sa02m-homekit" SA02M_HOMECONNECT_ETC="$C/etc/sa02m-homeconnect" \
    PYTHONDONTWRITEBYTECODE=1 python3 -c "
import sys; sys.path[:0] = ['$C']
from sa02m_homekit import config as hk; hk.save(hk.BridgeConfig(enabled=True))
from sa02m_homeconnect import config as hc; hc.save(hc.ClientConfig())
") > "$C/l.out" 2>&1 || bad "L the new-file saves failed as www-data: $(tr '\n' '|' < "$C/l.out" | cut -c1-300)"
got="$(mode_of "$C/etc/sa02m-homekit/sa02m-homekit.conf" 2>/dev/null)|$(mode_of "$C/etc/sa02m-homeconnect/sa02m-homeconnect.conf" 2>/dev/null)"
rc=0; run_helper "$C" check homekit >/dev/null || rc=$?; rc2=0; run_helper "$C" check homeconnect >/dev/null || rc2=$?
if [ "$got" = "61033:61001 640|61033:61002 640" ] && [ "$rc" = 0 ] && [ "$rc2" = 0 ]; then
    ok "L a NEW conf saved by www-data is 61033:<daemon> 0640 (setgid dir's group) and both checks pass"
else bad "L new-file saves: got $got, check rc=$rc/$rc2"; fi

if [ "$fails" -gt 0 ]; then
    echo "test-daemon-access: $fails FAILURE(S)"
    exit 1
fi
echo "test-daemon-access: all checks passed"
