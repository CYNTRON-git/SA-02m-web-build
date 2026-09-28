#!/usr/bin/env bash
# daemon-least-privilege.sh — quality row `daemon-least-privilege`.
#
# The HomeKit bridge listens on the LAN before it is paired; the Home Connect
# client talks to a foreign cloud. Neither may hold group `www-data`: that
# group reads the panel credentials (/etc/sa02m_web.env 0640 root:www-data),
# the gateway YAML, every Alice conf and the CSRF files (bench-session audit,
# 2026-09-28, MED). Each daemon gets exactly what it needs instead
# (docs/threat-model.md §3 «Мост HomeKit», docs/contracts/homekit-bridge.md §13,
# docs/contracts/home-connect.md §11):
#   * its /run dir setgid `www-data` (2750) — files the CGI reads inherit the
#     group, no membership needed;
#   * read on its own conf and (HomeKit) the Alice device document through
#     POSIX ACLs in its tmpfiles conf — a DEFAULT ACL on the directory, so an
#     atomic rename (a new inode) inherits the grant instead of dropping it;
#   * the unit re-applies that tmpfiles conf before every start.
#
# The group can come from TWO homes, both are read (quality-gate-rigor.md
# shape (b)): the unit's User=/Group=/SupplementaryGroups=, and the account's
# own /etc/group membership (the installer's useradd/usermod/gpasswd/adduser).
# No third home ships in this repo (no drop-in for either unit). An ALLOW-LIST, not a denylist:
# the units carry NO SupplementaryGroups= line at all, and the installers touch
# the daemon's group membership only to REMOVE www-data (the upgrade path).
#
# Non-vacuity: a missing file, a unit without its User= line, an installer
# without the removal line, a tmpfiles conf without the setgid /run line or the
# ACL lines, a unit without the tmpfiles ExecStartPre — each FAILS. Comments are
# blanked first (lib_check.sh), so `#` in front of a pinned line is a delete.
# Every PRESENCE pin reads the inline-stripped text (a trailing ` # …` is gone
# too), so `if true # gpasswd -d …` is a delete as well; the installer's
# fail-IF-PRESENT sweep keeps the whole-line strip — the conservative side for
# a banned-pattern search.
#
# What this gate does NOT prove: that the ACL lines take effect. A writer that
# creates a file 0600 and never chmods it gets mask::--- and the named r--
# entry is void, every pin still green — the runtime effect is a bench item
# (docs/deployment.md «Проверка на стенде»).
#
# RED recorded 2026-09-28 on the pre-fix tree (both units carried
# SupplementaryGroups=www-data, both installers usermod -a -G www-data).
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=lib_check.sh
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# name|unit|installer|tmpfiles|user|user-var|run dir|conf dir|extra ACL dir|files read
# "files read" = the existing inodes the default ACL cannot reach (created
# before it): an upgraded board needs an access ACL on each.
DAEMONS=(
  "homekit|etc/systemd/system/sa02m-homekit.service|scripts/06c-homekit.sh|opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf|sa02m-homekit|HK_USER|/run/sa02m-homekit|/etc/sa02m-homekit|/etc/sa02m-alice|/etc/sa02m-homekit/sa02m-homekit.conf /etc/sa02m-alice/sa02m-alice-devices.conf"
  "homeconnect|etc/systemd/system/sa02m-homeconnect.service|scripts/06d-homeconnect.sh|opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf|sa02m-homeconnect|HC_USER|/run/sa02m-homeconnect|/etc/sa02m-homeconnect||/etc/sa02m-homeconnect/sa02m-homeconnect.conf"
)

for row in "${DAEMONS[@]}"; do
  IFS='|' read -r name unit inst tmpf user uvar rundir confdir extra files <<<"$row"
  for f in "$unit" "$inst" "$tmpf"; do
    [ -f "$f" ] || { bad "$name: missing $f"; continue 2; }
  done
  u=$(stripped_text_inline "$unit")
  # ── unit ──
  if text_matches "$u" "^[[:space:]]*User=${user}[[:space:]]*$"; then ok "$name: unit runs as User=$user"
  else bad "$name: $unit has no live User=$user line (nothing to judge)"; fi
  if text_matches "$u" '^[[:space:]]*SupplementaryGroups='; then
    bad "$name: $unit carries a SupplementaryGroups= line — the daemon needs none (allow-list: empty)"
  else ok "$name: unit carries no SupplementaryGroups="; fi
  if text_matches "$u" '^[[:space:]]*(User|Group|DynamicUser)=.*www-data'; then
    bad "$name: $unit names www-data in User=/Group="
  else ok "$name: unit names no www-data in User=/Group="; fi
  if text_has "$u" "/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/${tmpf##*/}" \
     && text_matches "$u" '^[[:space:]]*ExecStartPre=[-+]{2}/usr/bin/systemd-tmpfiles --create '; then
    ok "$name: unit re-applies its tmpfiles conf (modes + ACLs) before every start"
  else bad "$name: $unit lacks ExecStartPre=-+/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/${tmpf##*/}"; fi
  # ── installer: membership only ever removed ──
  i=$(stripped_text "$inst")          # fail-IF-PRESENT sweep: whole-line strip
  ip=$(stripped_text_inline "$inst")  # presence pins: trailing comments gone too
  if text_matches "$i" "(usermod|useradd)[^#]*(-[a-zA-Z]*G|--groups|--append)" \
     || text_matches "$i" '(gpasswd[[:space:]]+-a|gpasswd[[:space:]]+--add|adduser[[:space:]]+[^-])'; then
    bad "$name: $inst adds the daemon account to a group (useradd -G / usermod -G / gpasswd -a / adduser)"
  else ok "$name: $inst adds the daemon account to no group"; fi
  if text_has "$ip" "gpasswd -d \"\$${uvar}\" www-data"; then ok "$name: $inst drops an upgraded account out of www-data"
  else bad "$name: $inst lacks the upgrade removal gpasswd -d \"\$${uvar}\" www-data"; fi
  if text_matches "$ip" "install -d -m 2750 -o \"\\\$${uvar}\" -g www-data ${rundir}([[:space:]]|$)"; then
    ok "$name: $inst creates $rundir setgid 2750 :www-data"
  else bad "$name: $inst does not create $rundir as install -d -m 2750 -o \"\$${uvar}\" -g www-data"; fi
  # ── tmpfiles: the replacement grants ──
  t=$(stripped_text_inline "$tmpf")
  if text_matches "$t" "^d[[:space:]]+${rundir}[[:space:]]+2750[[:space:]]+${user}[[:space:]]+www-data[[:space:]]"; then
    ok "$name: tmpfiles makes $rundir 2750 $user:www-data"
  else bad "$name: $tmpf has no 'd $rundir 2750 $user www-data' line"; fi
  for d in "$confdir" $extra; do
    if text_matches "$t" "^a\+[[:space:]]+${d}[[:space:]]+(-[[:space:]]+){4}user:${user}:--x,default:user:${user}:r--,default:mask::rwx$"; then
      ok "$name: tmpfiles grants $user traverse on $d and read on every file created there (default ACL)"
    else bad "$name: $tmpf lacks 'a+ $d - - - - user:$user:--x,default:user:$user:r--,default:mask::rwx'"; fi
  done
  for f in $files; do
    if text_matches "$t" "^a\+[[:space:]]+${f}[[:space:]]+(-[[:space:]]+){4}user:${user}:r--$"; then
      ok "$name: tmpfiles grants $user read on the existing $f"
    else bad "$name: $tmpf lacks 'a+ $f - - - - user:$user:r--'"; fi
  done
done

if [ "$fails" -gt 0 ]; then
  echo "daemon-least-privilege: $fails FAILURE(S)"
  exit 1
fi
echo "daemon-least-privilege: all checks passed"
