#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# test-mqtt-acl-audio.sh — quality row `mqtt-acl-audio`: the external MQTT
# listener (1884, `mqttuser`) cannot command the audio recorder directly
# (docs/contracts/audio-recorder.md §20).
#
# Why `topic deny` in the user block and not `pattern deny`: on the board's
# mosquitto 2.0.18 a `pattern deny /devices/sa02m-audio/controls/+/on` did NOT
# stop `mqttuser` (who has `topic readwrite /devices/#`) — a throwaway broker
# on bench 1.135 (2026-10-15) delivered the denied publish; a `topic deny`
# inside the `user mqttuser` block blocked it, before or after the grant.
# This harness pins that shape; it does not run a broker.
#
# Cases:
#   A  the template etc/mosquitto/acl_default.conf carries the exact deny line
#      INSIDE the `user mqttuser` block (after that line, before any other
#      `user` line), live (not commented), and no `pattern deny` for the
#      recorder (the shape that does not work).
#   B  the migration in scripts/15-audio.sh, extracted between its
#      `>>> ACL MIGRATION` / `<<< ACL MIGRATION` markers and run on sandbox
#      ACLs with `log`, `sa02m_atomic_install`, `systemctl` and `timeout`
#      as recording shims:
#      B1 the 05-mqtt.sh-era ACL ⇒ the line inserted right after
#         `user mqttuser`, nothing else changed, ONE bounded reload;
#      B2 a second run ⇒ byte-identical, no install, no reload (idempotent);
#      B3 a commented-out deny line does not count ⇒ inserted;
#      B4 only a `pattern deny` present ⇒ the `topic deny` still inserted;
#      B5 no `user mqttuser` block ⇒ untouched, a WARN, no reload;
#      B6 a symlink at the ACL path ⇒ the target untouched, a WARN;
#      B7 no ACL at all ⇒ nothing written, no reload;
#      B8 broker inactive ⇒ the file updated, no reload.
#   C  scripts/15-audio.sh calls the migration on /etc/mosquitto/acl/default.conf
#      on a live line after the block.
# RED (recorded in the row): the `print d` of the insert removed ⇒ B1/B3/B4;
# the call commented out ⇒ C; the template line commented out ⇒ A.
#
# Run: bash scripts/dev/test-mqtt-acl-audio.sh
#      ACL_TEMPLATE=<file> / AUDIO_INSTALLER=<file> judge other copies.
# ═══════════════════════════════════════════════════════════════════════════
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TPL="${ACL_TEMPLATE:-$HERE/etc/mosquitto/acl_default.conf}"
INST="${AUDIO_INSTALLER:-$HERE/scripts/15-audio.sh}"
DENY='topic deny /devices/sa02m-audio/controls/+/on'
fails=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$TPL" "$INST"; do
    [ -r "$f" ] || { echo "mqtt-acl-audio: FAIL — cannot read $f"; exit 1; }
done

echo "A. template"
in_block=$(awk -v d="$DENY" '
    /^[[:space:]]*user[[:space:]]+/ { cur = $2; next }
    { line = $0; sub(/^[[:space:]]+/, "", line); sub(/[[:space:]]+$/, "", line) }
    line == d && cur == "mqttuser" { hit = 1 }
    END { print hit ? "yes" : "no" }' "$TPL")
if [ "$in_block" = yes ]; then ok "A1 the deny line is live inside the user mqttuser block"
else bad "A1 $TPL has no live '$DENY' inside the user mqttuser block"; fi
if grep -Eq '^[[:space:]]*pattern[[:space:]]+deny[[:space:]]+/devices/sa02m-audio' "$TPL"; then
    bad "A2 $TPL uses a pattern deny for the recorder — not applied to a user with a grant (mosquitto 2.0.18)"
else ok "A2 no pattern deny for the recorder (the shape that does not work)"; fi

SB="$(mktemp -d "${TMPDIR:-/tmp}/mqtt-acl.XXXXXX")" || { echo "mqtt-acl-audio: FAIL — mktemp"; exit 1; }
trap 'rm -rf -- "$SB"' EXIT
FN="$SB/fn.sh"
sed -n '/^# >>> ACL MIGRATION/,/^# <<< ACL MIGRATION/p' "$INST" > "$FN"
if ! grep -q '^au_acl_migrate() {' "$FN" || ! bash -n "$FN" 2>/dev/null; then
    bad "B0 the ACL MIGRATION block of $INST did not extract into a parsing function"
    echo "mqtt-acl-audio: $fails FAILURE(S)"; exit 1
fi
CALLS="$SB/calls"
run_mig() {  # $1 = acl path, $2 = broker active (0|1)
    : > "$CALLS"
    (
        LOG_FILE="$SB/log"
        log() { printf 'log %s\n' "$*" >> "$CALLS"; }
        sa02m_atomic_install() {
            local src dst
            while [ $# -gt 2 ]; do shift; done
            src=$1 dst=$2
            printf 'install %s\n' "$dst" >> "$CALLS"
            cp -- "$src" "$dst"
        }
        systemctl() {
            if [ "$1" = is-active ]; then [ "$ACTIVE" = 1 ]; return; fi
            printf 'systemctl %s %s\n' "$*" "${VIA_TIMEOUT:-UNBOUNDED}" >> "$CALLS"
        }
        timeout() { shift; VIA_TIMEOUT=BOUNDED "$@"; }
        ACTIVE=$2
        # shellcheck source=/dev/null
        . "$FN"
        au_acl_migrate "$1"
    )
}
count() { grep -c "^$1" "$CALLS" 2>/dev/null; }
OLD=$'# SA-02m — Mosquitto ACL (шаблон)\n\nuser mqttuser\ntopic readwrite /devices/#\ntopic read $SYS/#\n'

echo "B. migration"
acl=$SB/acl1; printf '%s' "$OLD" > "$acl"
run_mig "$acl" 1
want=$(printf '%s' "$OLD" | awk -v d="$DENY" '{ print } /^user mqttuser$/ { print d }')
if [ "$(cat "$acl")" = "$want" ] && [ "$(count install)" = 1 ] \
   && [ "$(grep -c '^systemctl reload mosquitto BOUNDED$' "$CALLS")" = 1 ]; then
    ok "B1 the line inserted after user mqttuser, nothing else changed, one bounded reload"
else bad "B1 old ACL: $(tr '\n' '|' < "$acl") calls: $(tr '\n' '|' < "$CALLS")"; fi
cp "$acl" "$SB/acl1.before"
run_mig "$acl" 1
if cmp -s "$acl" "$SB/acl1.before" && [ "$(count install)" = 0 ] && [ "$(count systemctl)" = 0 ]; then
    ok "B2 a second run changes nothing and reloads nothing (idempotent)"
else bad "B2 re-run: calls: $(tr '\n' '|' < "$CALLS")"; fi
acl=$SB/acl3; printf 'user mqttuser\n# %s\ntopic readwrite /devices/#\n' "$DENY" > "$acl"
run_mig "$acl" 1
if [ "$(grep -cxF "$DENY" "$acl")" = 1 ]; then ok "B3 a commented-out deny line does not count — the live line is inserted"
else bad "B3 commented line: $(tr '\n' '|' < "$acl")"; fi
acl=$SB/acl4; printf 'pattern deny /devices/sa02m-audio/controls/+/on\nuser mqttuser\ntopic readwrite /devices/#\n' > "$acl"
run_mig "$acl" 1
if [ "$(grep -cxF "$DENY" "$acl")" = 1 ]; then ok "B4 a pattern deny alone does not count — the topic deny is inserted"
else bad "B4 pattern only: $(tr '\n' '|' < "$acl")"; fi
acl=$SB/acl5; printf 'user scada\ntopic readwrite /devices/#\n' > "$acl"; cp "$acl" "$SB/acl5.before"
run_mig "$acl" 1
if cmp -s "$acl" "$SB/acl5.before" && grep -q '^log WARN' "$CALLS" && [ "$(count systemctl)" = 0 ]; then
    ok "B5 no mqttuser block: untouched, a WARN, no reload"
else bad "B5 no mqttuser: calls: $(tr '\n' '|' < "$CALLS")"; fi
printf '%s' "$OLD" > "$SB/victim"; cp "$SB/victim" "$SB/victim.before"
ln -s "$SB/victim" "$SB/acl6" 2>/dev/null
if [ -L "$SB/acl6" ]; then
    run_mig "$SB/acl6" 1
    if cmp -s "$SB/victim" "$SB/victim.before" && grep -q '^log WARN' "$CALLS" && [ "$(count install)" = 0 ]; then
        ok "B6 a symlink at the ACL path: the target untouched, a WARN"
    else bad "B6 symlink: calls: $(tr '\n' '|' < "$CALLS")"; fi
else
    printf '  SKIP  B6 this host cannot make a symlink — not run (a skip is not a pass)\n'
fi
run_mig "$SB/absent" 1
if [ ! -e "$SB/absent" ] && [ "$(count install)" = 0 ] && [ "$(count systemctl)" = 0 ]; then
    ok "B7 no ACL: nothing written, nothing reloaded"
else bad "B7 absent ACL: calls: $(tr '\n' '|' < "$CALLS")"; fi
acl=$SB/acl8; printf '%s' "$OLD" > "$acl"
run_mig "$acl" 0
if [ "$(grep -cxF "$DENY" "$acl")" = 1 ] && [ "$(count systemctl)" = 0 ]; then
    ok "B8 broker inactive: the file updated, no reload"
else bad "B8 inactive broker: calls: $(tr '\n' '|' < "$CALLS")"; fi

echo "C. the installer calls it"
blk_end=$(grep -n '^# <<< ACL MIGRATION' "$INST" | head -n1 | cut -d: -f1)
call=$(grep -n '^au_acl_migrate /etc/mosquitto/acl/default\.conf[[:space:]]*$' "$INST" | head -n1 | cut -d: -f1)
if [ -n "$call" ] && [ -n "$blk_end" ] && [ "$call" -gt "$blk_end" ]; then
    ok "C 15-audio.sh runs the migration on /etc/mosquitto/acl/default.conf (line $call)"
else bad "C 15-audio.sh never calls au_acl_migrate /etc/mosquitto/acl/default.conf after the block"; fi

if [ "$fails" -eq 0 ]; then
    echo "mqtt-acl-audio: all checks passed"
    exit 0
fi
echo "mqtt-acl-audio: $fails FAILURE(S)"
exit 1
