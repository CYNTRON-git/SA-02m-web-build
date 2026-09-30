#!/usr/bin/env bash
# daemon-least-privilege.sh — quality row `daemon-least-privilege`.
#
# The HomeKit bridge listens on the LAN before it is paired; the Home Connect
# client talks to a foreign cloud. Neither may hold group `www-data`: that
# group reads the panel credentials (/etc/sa02m_web.env 0640 root:www-data),
# the gateway YAML, every Alice conf and the CSRF files (bench-session audit,
# 2026-09-28, MED). Each daemon gets exactly what it needs instead, through
# plain Unix groups (docs/threat-model.md §3 «Мост HomeKit»,
# docs/contracts/homekit-bridge.md §13, docs/contracts/home-connect.md §11):
#   * its /run dir setgid `www-data` (2750) — files the CGI reads inherit the
#     group, no membership needed;
#   * its conf dir www-data:<daemon> 2750 — setgid, so every conf the CGI saves
#     there by atomic rename is the daemon's group; the conf www-data:<daemon>
#     0640 (read only for the daemon);
#   * HomeKit: the Alice device document www-data:sa02m-alice-devices 0640, a
#     group of exactly the bridge and www-data, and /etc/sa02m-alice 0771
#     (traverse without listing).
# NO POSIX ACL anywhere: the product RT kernel has no ext4 ACL support, and
# the ACL grant this replaced passed every line of the previous version of
# this gate while granting nothing on a real board (bench 1.135, 2026-09-28).
# An ACL line is REFUSED as a read path: in either package tmpfiles conf the
# ACL types with any modifier (`a`, `a+`, `a!`, `A`, `A+`, `a~`, …), and
# setfacl/getfacl in the files this gate reads — both units, both installers,
# the helper. It reads no other home: an ACL set elsewhere on the rootfs is
# outside this gate (the effect check on the board would still show a read the
# daemon must not have).
#
# The group can come from TWO homes, both are read (quality-gate-rigor.md
# shape (b)): the unit's User=/Group=/SupplementaryGroups=, and the account's
# own /etc/group membership (the installer's useradd/usermod/gpasswd/adduser).
# No third home ships in this repo (no drop-in for either unit). An ALLOW-LIST,
# not a denylist: the units carry NO SupplementaryGroups= line at all; the
# only membership adds are the two exact lines of the sa02m-alice-devices group
# in 06c (its name pinned too, so it cannot be pointed at www-data), and
# otherwise membership is only ever REMOVED (www-data, the upgrade path).
#
# The grant's files are pinned in usr/local/sbin/sa02m-daemon-access.sh (the
# owner/group/mode it re-asserts and the probes it runs as the daemon), and
# both installers must end with that effect check (after the unit and the CGI,
# failing the module), both units run it before every start.
#
# What this gate proves: the LINES — modes, memberships, no ACL, the effect
# check wired in. It does NOT prove their runtime effect; that is enforced on
# the board by the effect check itself (scripts/06c|06d end fatal, every unit
# start journaled) and exercised by scripts/dev/test-daemon-access.sh (real
# files, a non-root uid: 660 root:www-data FAILS, the new modes PASS).
#
# Non-vacuity: a missing file or any pinned line absent FAILS. Comments are
# blanked first (lib_check.sh), so `#` in front of a pinned line is a delete;
# every PRESENCE pin reads the inline-stripped text (a trailing ` # …` is gone
# too), so `if true # gpasswd -d …` is a delete as well; the fail-IF-PRESENT
# sweeps keep the whole-line strip — the conservative side for a banned-pattern
# search.
#
# RED recorded 2026-09-28 on the pre-fix tree twice: the www-data membership
# (both units SupplementaryGroups=www-data, both installers usermod -a -G
# www-data), and the ACL grant at 9cf41b5 (a+ lines in both tmpfiles confs, no
# effect check, conf dirs root:www-data 0770).
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=lib_check.sh
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

HELPER=usr/local/sbin/sa02m-daemon-access.sh
ALICE_TMPFILES=etc/tmpfiles.d/sa02m-alice.conf

# name|unit|installer|tmpfiles|user|user-var|run dir|conf dir|conf|allowed membership adds (~-separated, exact)
DAEMONS=(
  "homekit|etc/systemd/system/sa02m-homekit.service|scripts/06c-homekit.sh|opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf|sa02m-homekit|HK_USER|/run/sa02m-homekit|/etc/sa02m-homekit|/etc/sa02m-homekit/sa02m-homekit.conf|gpasswd -a \"\$HK_USER\" \"\$HK_DEVDOC_GROUP\" >>\"\$LOG_FILE\" 2>&1 \\~if gpasswd -a www-data \"\$HK_DEVDOC_GROUP\" >>\"\$LOG_FILE\" 2>&1; then"
  "homeconnect|etc/systemd/system/sa02m-homeconnect.service|scripts/06d-homeconnect.sh|opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf|sa02m-homeconnect|HC_USER|/run/sa02m-homeconnect|/etc/sa02m-homeconnect|/etc/sa02m-homeconnect/sa02m-homeconnect.conf|"
)

# A line adds an account to a group.
ADD_RE='(usermod|useradd)[^#]*(-[a-zA-Z]*G|--groups|--append)|gpasswd[[:space:]]+(-a|--add)[[:space:]]|adduser[[:space:]]+[^-[:space:]]'
# An ACL line in a tmpfiles conf: type a/A followed by any tmpfiles modifier
# (+ ! - = ~ ^) — `a!` is as much a grant as `a+` (review B3).
ACL_TMPFILES_RE='^[[:space:]]*[aA][-+!=~^]*[[:space:]]'

for f in "$HELPER" "$ALICE_TMPFILES"; do
  [ -f "$f" ] || { bad "missing $f"; }
done

for row in "${DAEMONS[@]}"; do
  IFS='|' read -r name unit inst tmpf user uvar rundir confdir conf allowed <<<"$row"
  for f in "$unit" "$inst" "$tmpf"; do
    [ -f "$f" ] || { bad "$name: missing $f"; continue 2; }
  done
  u=$(stripped_text_inline "$unit")
  uw=$(stripped_text "$unit")
  # ── unit ──
  if text_matches "$u" "^[[:space:]]*User=${user}[[:space:]]*$"; then ok "$name: unit runs as User=$user"
  else bad "$name: $unit has no live User=$user line (nothing to judge)"; fi
  if text_matches "$uw" '^[[:space:]]*SupplementaryGroups='; then
    bad "$name: $unit carries a SupplementaryGroups= line — the daemon needs none (allow-list: empty)"
  else ok "$name: unit carries no SupplementaryGroups="; fi
  if text_matches "$uw" '^[[:space:]]*(User|Group|DynamicUser)=.*www-data'; then
    bad "$name: $unit names www-data in User=/Group="
  else ok "$name: unit names no www-data in User=/Group="; fi
  if text_matches "$u" "^[[:space:]]*ExecStartPre=-\\+/usr/local/sbin/sa02m-daemon-access\\.sh apply ${name}[[:space:]]*$"; then
    ok "$name: unit re-asserts and PROVES the grant before every start (non-fatal: the card shows conf_unreadable)"
  else bad "$name: $unit lacks ExecStartPre=-+/usr/local/sbin/sa02m-daemon-access.sh apply $name"; fi
  if text_matches "$uw" 'setfacl'; then bad "$name: $unit calls setfacl — an ACL is not a read path here (no ACL on the RT kernel)"
  else ok "$name: unit sets no ACL"; fi

  # ── installer: membership — allow-listed adds, the www-data removal ──
  i=$(stripped_text "$inst")          # fail-IF-PRESENT sweeps: whole-line strip
  ip=$(stripped_text_inline "$inst")  # presence pins: trailing comments gone too
  adds=$(grep -E "$ADD_RE" <<<"$i" || true)
  stray=""
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    t=${line#"${line%%[![:space:]]*}"}
    hit=0
    IFS='~' read -r -a allow <<<"$allowed"
    for a in "${allow[@]}"; do [ -n "$a" ] && [ "$t" = "$a" ] && hit=1; done
    [ "$hit" = 1 ] || stray="$stray [$t]"
  done <<<"$adds"
  if [ -n "$stray" ]; then bad "$name: $inst adds an account to a group outside the allow-list:$stray"
  else ok "$name: $inst adds accounts to no group beyond the allow-list"; fi
  IFS='~' read -r -a allow <<<"$allowed"
  for a in "${allow[@]}"; do
    [ -n "$a" ] || continue
    if text_has "$ip" "$a"; then ok "$name: $inst carries the allowed membership line: $a"
    else bad "$name: $inst lacks the membership line: $a"; fi
  done
  if [ "$name" = homekit ]; then
    if text_matches "$ip" '^HK_DEVDOC_GROUP=sa02m-alice-devices$'; then
      ok "$name: the device-document group is sa02m-alice-devices (not www-data)"
    else bad "$name: $inst lacks HK_DEVDOC_GROUP=sa02m-alice-devices — the allowed adds would join an unknown group"; fi
    if text_has "$ip" 'groupadd --system "$HK_DEVDOC_GROUP"'; then ok "$name: $inst creates the device-document group"
    else bad "$name: $inst lacks groupadd --system \"\$HK_DEVDOC_GROUP\""; fi
  fi
  if text_has "$ip" "gpasswd -d \"\$${uvar}\" www-data"; then ok "$name: $inst drops an upgraded account out of www-data"
  else bad "$name: $inst lacks the upgrade removal gpasswd -d \"\$${uvar}\" www-data"; fi
  if text_matches "$ip" "install -d -m 2750 -o \"\\\$${uvar}\" -g www-data ${rundir}([[:space:]]|$)"; then
    ok "$name: $inst creates $rundir setgid 2750 :www-data"
  else bad "$name: $inst does not create $rundir as install -d -m 2750 -o \"\$${uvar}\" -g www-data"; fi
  if text_matches "$ip" "install -d -m 2750 -o www-data -g ${user} ${confdir}([[:space:]]|$)"; then
    ok "$name: $inst creates $confdir 2750 www-data:$user (setgid to the daemon's group)"
  else bad "$name: $inst does not create $confdir as install -d -m 2750 -o www-data -g $user"; fi
  if text_matches "$i" 'setfacl|getfacl'; then bad "$name: $inst still uses setfacl/getfacl — no ACL on the product kernel"
  else ok "$name: $inst uses no ACL tool"; fi
  # The effect check: installed before the unit, run LAST, failing the module.
  if text_has "$ip" "\"\$BASE_DIR/usr/local/sbin/sa02m-daemon-access.sh\" /usr/local/sbin/sa02m-daemon-access.sh"; then
    ok "$name: $inst installs the access helper"
  else bad "$name: $inst does not install usr/local/sbin/sa02m-daemon-access.sh"; fi
  call_ln=$(stripped_first_line "$inst" "/usr/local/sbin/sa02m-daemon-access\\.sh apply ${name} 2>&1\\) \\|\\| [a-z]+_access_rc=\\\$\\?$")
  last_ln=$(stripped_last_line "$inst" '(sa02m_svc_apply "\$UNIT"|_api\.cgi")')
  if [ -z "$call_ln" ]; then
    bad "$name: $inst never runs /usr/local/sbin/sa02m-daemon-access.sh apply $name (the effect check)"
  elif [ -z "$last_ln" ] || [ "$call_ln" -le "$last_ln" ]; then
    bad "$name: $inst runs the effect check at line $call_ln, not after the unit and the CGI (line ${last_ln:-?})"
  else ok "$name: $inst ends with the effect check (line $call_ln, after line $last_ln)"; fi
  if [ -n "$call_ln" ] && text_matches "$ip" "^if \\[ \"\\\$[a-z]+_access_rc\" != 0 \\]; then$" \
     && [ -n "$(awk -v c="$call_ln" 'NR>c && /^[[:space:]]*exit 1[[:space:]]*$/ {print NR; exit}' <<<"$i")" ]; then
    ok "$name: a failed effect check exits the module non-zero"
  else bad "$name: $inst does not exit 1 when the effect check fails"; fi

  # ── tmpfiles: dir modes, and NO ACL line ──
  t=$(stripped_text_inline "$tmpf")
  tw=$(stripped_text "$tmpf")
  if text_matches "$tw" "$ACL_TMPFILES_RE"; then
    bad "$name: $tmpf carries an ACL line (type a/A, any modifier) — refused as a read path (the RT kernel has no ext4 ACL)"
  else ok "$name: $tmpf carries no ACL line"; fi
  if text_matches "$t" "^d[[:space:]]+${rundir}[[:space:]]+2750[[:space:]]+${user}[[:space:]]+www-data[[:space:]]"; then
    ok "$name: tmpfiles makes $rundir 2750 $user:www-data"
  else bad "$name: $tmpf has no 'd $rundir 2750 $user www-data' line"; fi
  if text_matches "$t" "^d[[:space:]]+${confdir}[[:space:]]+2750[[:space:]]+www-data[[:space:]]+${user}[[:space:]]"; then
    ok "$name: tmpfiles makes $confdir 2750 www-data:$user"
  else bad "$name: $tmpf has no 'd $confdir 2750 www-data $user' line"; fi
done

# ── the helper: the file grants and the probes, as the daemon ──
h=$(stripped_text_inline "$HELPER")
hw=$(stripped_text "$HELPER")
for pin in \
  'ops+=("f:$CONF:0640:$WEB_UID:$DAEMON_GID:${WEB_GID:-x},$DAEMON_GID")' \
  'ops+=("f:$DEVDOC:0640:$WEB_UID:$DEVDOC_GID:${WEB_GID:-x},$DEVDOC_GID")' \
  'if not (st.st_uid == uid or (st.st_uid == 0 and st.st_gid in root_gids)):' \
  'if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:' \
  'ops+=("x:$ALICE_DIR")' \
  'DEVDOC_GROUP=sa02m-alice-devices' \
  'probe yes "$DAEMON" r "$CONF"' \
  'probe yes "$DAEMON" r "$DEVDOC"' \
  'probe yes "$WEB_USER" r "$CONF"' \
  'probe yes "$WEB_USER" w "$CONF_DIR"' \
  'probe no "$DAEMON" r "$f"' \
  'setpriv) timeout 5 setpriv --reuid="$uid" --regid="$gid" --groups="${grps// /,}"' \
  '*" ${WEB_GID:-none} "*) fail "$DAEMON состоит в группе $WEB_USER'; do
  if text_has "$h" "$pin"; then ok "helper: $pin"
  else bad "helper: $HELPER lacks the pinned line: $pin"; fi
done
if text_matches "$h" '^[[:space:]]+DENY_READ="/etc/sa02m_web\.env /etc/sa02m-alice/sa02m-alice-client\.conf /etc/sa02m-alice/sa02m-alice-server\.conf"$'; then
  ok "helper: the bridge is probed NOT to read the panel credentials and the other Alice confs"
else bad "helper: $HELPER lacks the bridge's DENY_READ list (panel credentials + client/server Alice confs)"; fi
if text_matches "$h" '^[[:space:]]+DENY_READ="/etc/sa02m_web\.env /etc/sa02m-alice/sa02m-alice-devices\.conf"$'; then
  ok "helper: Home Connect is probed NOT to read the panel credentials and the Alice device document"
else bad "helper: $HELPER lacks the Home Connect DENY_READ list (panel credentials + the Alice device document)"; fi
# The FINAL failure exit, bound to its own block: the `fails -gt 0` block must
# exit 1 before its `fi` — an unrelated `exit 1` elsewhere does not count
# (review B3: the missing-account branch satisfied the previous pin).
fexit=$(awk '/^if \[ "\$fails" -gt 0 \]; then$/ {inb=1; next}
             inb && /^fi$/ {print (hit ? "yes" : "no"); inb=0; done=1; exit}
             inb && /^[[:space:]]+exit 1[[:space:]]*$/ {hit=1}
             END {if (!done) print "none"}' <<<"$h")
if [ "$fexit" = yes ]; then ok "helper: the final \`fails -gt 0\` block exits 1"
else bad "helper: $HELPER's final \`if [ \"\$fails\" -gt 0 ]\` block does not exit 1 (found: $fexit)"; fi
if text_matches "$hw" 'setfacl'; then bad "helper: $HELPER calls setfacl — no ACL grant"
else ok "helper: grants no ACL"; fi

# ── the Alice dir: traverse without listing ──
a=$(stripped_text_inline "$ALICE_TMPFILES")
if text_matches "$a" '^d[[:space:]]+/etc/sa02m-alice[[:space:]]+0771[[:space:]]+root[[:space:]]+www-data[[:space:]]'; then
  ok "alice: tmpfiles makes /etc/sa02m-alice 0771 root:www-data (traverse for the bridge, no listing)"
else bad "alice: $ALICE_TMPFILES has no 'd /etc/sa02m-alice 0771 root www-data' line"; fi

if [ "$fails" -gt 0 ]; then
  echo "daemon-least-privilege: $fails FAILURE(S)"
  exit 1
fi
echo "daemon-least-privilege: all checks passed"
