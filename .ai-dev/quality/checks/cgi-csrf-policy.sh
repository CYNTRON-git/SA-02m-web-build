#!/usr/bin/env bash
# cgi-csrf-policy — SET-WIDE static gate for docs/decisions/selective-csrf-policy.md:
# every session-authed CGI that runs a root helper or mutates state is POST-only
# and checks X-SA02M-CSRF (web_csrf_validate / web_csrf_require) BEFORE the
# mutation. Audit 2026-09-16 (M2) found two endpoints outside the policy —
# mqtt_scan.cgi (root bus scanner on POST AND GET, no token) and
# web_update_check.cgi (root update check on POST and on GET ?force=1, no
# token) — while the threat model said the class «держится (1.0.5.72)» and the
# only mechanical row (web-update-csrf-contract) covered ONE endpoint. This gate
# makes the claim set-wide and keeps it true: a NEW mutating CGI cannot pass the
# registry without the token.
#
# LEDGER (the one home for the inventory — decided per file, not discovered):
#   MUTATING        cgi|anchor  — the anchor is a literal on the MUTATING line
#                   (the sudo / mosquitto_pub / I2C write / upload receive);
#   READ_ONLY_SUDO  cgi|reason  — invokes sudo but only reads (no token needed);
#   EXCEPTIONS      cgi|reason  — documented policy exception (logout);
#   EXCLUDED        cgi|reason  — not session-authed / not an endpoint of the class.
#
# PINS per MUTATING row, read COMMENT-STRIPPED via lib_check.sh (capture, then
# match — never `| grep -q`, trap 1 of lib_check.sh):
#   (i)   >=1 live web_csrf_(validate|require) line;
#   (ii)  >=1 live REQUEST_METHOD line (the POST-only half);
#   (iii) the FIRST live csrf line precedes the anchor line (token before the
#         mutation — the web-update-csrf-contract ordering shape, set-wide);
#   (iv)  the anchor literal matches EXACTLY ONE live line — a moved or
#         duplicated anchor FAILS naming the file (re-anchor consciously).
# OPEN-WORLD SWEEP: every *.cgi in the directory whose live text carries a
# trigger token — a `sudo` word (also inside a python list: ["sudo", …]),
# mosquitto_pub, systemctl start|stop|restart|reload|enable|disable|mask|unmask,
# a redirect or tee into /etc/, or the hw_set.cgi write primitives
# sa02m_hw_i2c_write_channel_web / sa02m_hw_gpio_write_channel — must be in
# MUTATING, READ_ONLY_SUDO or EXCEPTIONS; a hit outside the ledger FAILS
# («new mutating CGI <x> is not in the CSRF ledger»).
# NON-VACUITY: a ledger row naming an absent file FAILS; MUTATING floor 25,
# READ_ONLY_SUDO floor 3; an empty *.cgi sweep FAILS; lib_check.sh absent FAILS.
#
# WHAT IT DOES NOT PROVE: that the POST branch is the ONLY branch reaching the
# anchor — a static ordering pin cannot see control flow. That is the
# behavioural row's job (cgi-csrf-behaviour, scripts/dev/test-cgi-csrf-behaviour.sh)
# for the two fixed endpoints, and reading for the rest. Also: a mutation through
# a primitive not in the trigger set is caught only by review (the ledger lists
# mutators by declaration, the sweep by the known primitives).
# OVERLAP: web-update-csrf-contract stays — its region check (csrf BETWEEN the
# legacy-OTA marker and the launch) is stricter than the anchor ordering here.
#
# PROVEN RED (2026-09-17, 1.0.6.49): against the 1.0.6.48 cgi-bin (git archive
# origin/main, CGI_DIR=<copy>) exactly two rows FAIL — mqtt_scan.cgi («no live
# web_csrf_validate/require line — the mutation at line 66 runs without the
# token») and web_update_check.cgi (same, mutation at line 25); the other 23
# MUTATING rows ok, sweep 28/42 all ledgered. GREEN on the fixed tree (25/25).
# Comment-out cases (mqtt_scan / web_update_check / services_ctrl csrf lines)
# registered in comment-mutation-proof.
#
# Run: bash .ai-dev/quality/checks/cgi-csrf-policy.sh
#      CGI_DIR=<dir> judges another copy of cgi-bin (the RED run).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
[ -r .ai-dev/quality/checks/lib_check.sh ] || { echo "cgi-csrf-policy: FAIL — lib_check.sh absent"; exit 1; }
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh

CGI_DIR="${CGI_DIR:-www/network_config/cgi-bin}"
fails=0
ok()  { printf 'cgi-csrf-policy: ok    %s\n' "$*"; }
bad() { printf 'cgi-csrf-policy: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

# ── the ledger ──────────────────────────────────────────────────────────────
MUTATING='
apply.cgi|sudo -n /usr/local/sbin/sa02m-iface-conf-write.sh "$conf"
cloud.cgi|sudo -n "$CLOUD_TRIGGER" pair
cmd_exec.cgi|sudo -n -u root "$HELPER"
cpu_profile.cgi|sudo -n "$CTL" set "$PROFILE"
gateway_config.cgi|sudo /usr/local/sbin/sa02m-gateway-config-apply.sh
gateway_ctrl.cgi|sudo /usr/bin/systemctl "$1" sa02m-serial-gateway
hw_set.cgi|sa02m_hw_i2c_write_channel_web "$CH" "$VAL"
kernel_ctrl.cgi|sudo -n "$CTL" set "$PROFILE"
mplc_project_deploy.cgi|nohup sudo -n "$HELPER" "$STAGED_ZIP"
mqtt_config.cgi|sudo /usr/local/sbin/sa02m-mqtt-config-apply.sh
mqtt_ctrl.cgi|sudo -n '"'"'$CTL'"'"' '"'"'$_act'"'"' mqtt-bridge
mqtt_scan.cgi|sudo /usr/bin/python3 "$SCAN_PY"
mqtt_set.cgi|timeout 5 mosquitto_pub
reboot.cgi|sudo -n /usr/local/sbin/sa02m-web-reboot.sh
restart.cgi|sudo -n /usr/local/sbin/sa02m-web-restart-services.sh
sa02m_alice_api.cgi|sudo -n /usr/local/sbin/sa02m-alice-web-trigger.sh "$ACTION"
services_ctrl.cgi|nohup sudo -n "$CTL" "$ACTION" "$SID"
storage_format_set.cgi|sudo -n /usr/local/sbin/sa02m-set-storage-auto-format "$VAL"
variant.cgi|sudo /usr/local/sbin/sa02m-apply-variant.sh "$VARIANT"
web_creds.cgi|sudo /usr/local/sbin/sa02m-commit-web-env
web_factory_reset.cgi|sudo -n /usr/bin/systemctl start sa02m-factory-reset.service
web_update_apply.cgi|nohup sudo -n /usr/local/sbin/sa02m-web-update-apply
web_update_cancel.cgi|["sudo", "-n", "/usr/bin/systemctl", "stop", "sa02m-update.service"]
web_update_check.cgi|sudo -n /usr/local/sbin/sa02m-web-update-check --manual
web_update_upload.cgi|UPLOAD_JSON=$(
'
# Notes on three anchors: services_ctrl.cgi runs `sudo CTL list` (a read) on
# GET at :58 BEFORE the token at :71 — the anchor is the ACTION launch, not the
# first sudo. web_update_upload.cgi runs `sudo inspect` (a read of an already
# uploaded package) on its GET branch before the token; the mutation is the
# multipart receive on the POST branch (UPLOAD_JSON=$( … receive_multipart_file)
# after web_csrf_require. cpu_profile / kernel_ctrl read `status --json` on GET;
# the anchor is the `set` on the POST branch.
READ_ONLY_SUDO='
mqtt_status.cgi|sudo -n cat /etc/sa02m_mqtt.env + sudo -n sa02m-mqtt-external-info.py: reads only, nothing written
status.cgi|sudo -n CTL list + sudo -n rs485-stats-helper driver/inuse: reads only (dashboard poll)
web_backup.cgi|GET-only by design (refuses non-GET), streams sa02m-web-backup.sh output; the script writes only its own $TMP
'
EXCEPTIONS='
logout.cgi|self-scoped (revokes only the caller own session) — selective-csrf-policy.md §logout
'
EXCLUDED='
login.cgi|mints the session; not session-authed
index.cgi|302 redirect, no work
'
MUTATING_FLOOR=25
READ_ONLY_FLOOR=3

# Trigger tokens (ERE on a comment-stripped line). The sudo form also catches
# the python-list spelling ["sudo", …] that a `sudo ` (trailing space) grep misses.
TRIGGER_ERE='(^|[^A-Za-z0-9_./-])sudo([^A-Za-z0-9_-]|$)|mosquitto_pub|systemctl[[:space:]]+(-n[[:space:]]+)?(start|stop|restart|reload|enable|disable|mask|unmask)([^-]|$)|>>?[[:space:]]*/etc/|tee[[:space:]]+(-a[[:space:]]+)?/etc/|sa02m_hw_i2c_write_channel_web|sa02m_hw_gpio_write_channel'

# ── helpers (capture-then-match; no early-exit pipe) ────────────────────────
rows_of() {  # $1 = ledger text → "cgi|rest" lines, blanks dropped
    printf '%s\n' "$1" | awk -F'|' 'NF >= 2 && $1 != "" { print }'
}
names_of() { rows_of "$1" | cut -d'|' -f1; }
in_list() {  # $1 = name, $2 = newline list
    case $'\n'"$2"$'\n' in *$'\n'"$1"$'\n'*) return 0 ;; *) return 1 ;; esac
}

# ── non-vacuity floors ──────────────────────────────────────────────────────
[ -d "$CGI_DIR" ] || { echo "cgi-csrf-policy: FAIL — $CGI_DIR is not a directory"; exit 1; }
all_cgis=$(cd "$CGI_DIR" && ls -- *.cgi 2>/dev/null)
n_all=$(printf '%s\n' "$all_cgis" | grep -c '\.cgi$')
[ "$n_all" -gt 0 ] || { echo "cgi-csrf-policy: FAIL — no *.cgi under $CGI_DIR (empty sweep)"; exit 1; }

n_mut=$(rows_of "$MUTATING" | grep -c .)
n_ro=$(rows_of "$READ_ONLY_SUDO" | grep -c .)
if [ "$n_mut" -ge "$MUTATING_FLOOR" ]; then ok "ledger: $n_mut MUTATING rows (floor $MUTATING_FLOOR)"
else bad "ledger: $n_mut MUTATING rows < floor $MUTATING_FLOOR — a row was dropped; deleting a mutating endpoint lowers the floor consciously"; fi
if [ "$n_ro" -ge "$READ_ONLY_FLOOR" ]; then ok "ledger: $n_ro READ_ONLY_SUDO rows (floor $READ_ONLY_FLOOR)"
else bad "ledger: $n_ro READ_ONLY_SUDO rows < floor $READ_ONLY_FLOOR"; fi

for ledger in MUTATING READ_ONLY_SUDO EXCEPTIONS EXCLUDED; do
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        [ -f "$CGI_DIR/$name" ] || bad "$ledger row names an absent file: $name (a deleted endpoint is removed from the ledger consciously)"
    done <<<"$(names_of "${!ledger}")"
done

# ── pins per MUTATING row ───────────────────────────────────────────────────
while IFS='|' read -r name anchor; do
    [ -n "$name" ] || continue
    f="$CGI_DIR/$name"
    [ -f "$f" ] || continue   # already reported above
    text=$(stripped_text "$f")
    [ -n "$text" ] || { bad "$name: empty after comment-stripping — nothing to pin"; continue; }

    # (iv) the anchor: exactly one live line
    anchor_hits=$(grep -nF -- "$anchor" <<<"$text")
    n_anchor=$(printf '%s\n' "$anchor_hits" | grep -c .)
    if [ "$n_anchor" -ne 1 ]; then
        bad "$name: mutation anchor '$anchor' matches $n_anchor live line(s), expected exactly 1 — re-anchor this ledger row consciously"
        continue
    fi
    anchor_ln=${anchor_hits%%:*}

    # (i) a live csrf line, (ii) a live REQUEST_METHOD line
    csrf_hits=$(grep -nE 'web_csrf_(validate|require)' <<<"$text")
    n_csrf=$(printf '%s\n' "$csrf_hits" | grep -c .)
    n_method=$(grep -cE 'REQUEST_METHOD' <<<"$text")
    if [ "$n_csrf" -eq 0 ]; then
        bad "$name: no live web_csrf_validate/require line — the mutation at line $anchor_ln runs without the X-SA02M-CSRF token (policy: selective-csrf-policy.md)"
        continue
    fi
    if [ "$n_method" -eq 0 ]; then
        bad "$name: no live REQUEST_METHOD line — the endpoint is not method-gated (POST-only is half the policy)"
        continue
    fi
    # (iii) token before the mutation
    first_csrf=${csrf_hits%%$'\n'*}; first_csrf=${first_csrf%%:*}
    if [ "$first_csrf" -lt "$anchor_ln" ]; then
        ok "$name: csrf check at line $first_csrf precedes the mutation at line $anchor_ln; REQUEST_METHOD gated"
    else
        bad "$name: first live csrf check at line $first_csrf is NOT before the mutation at line $anchor_ln"
    fi
done <<<"$(rows_of "$MUTATING")"

# ── open-world sweep ────────────────────────────────────────────────────────
ledgered=$(printf '%s\n%s\n%s\n' "$(names_of "$MUTATING")" "$(names_of "$READ_ONLY_SUDO")" "$(names_of "$EXCEPTIONS")")
n_hits=0
while IFS= read -r name; do
    [ -n "$name" ] || continue
    text=$(stripped_text "$CGI_DIR/$name")
    hits=$(grep -nE "$TRIGGER_ERE" <<<"$text")
    [ -n "$hits" ] || continue
    n_hits=$((n_hits + 1))
    if in_list "$name" "$ledgered"; then
        continue
    fi
    first=${hits%%$'\n'*}
    bad "new mutating CGI $name is not in the CSRF ledger — trigger at line ${first%%:*}: ${first#*:} (add a MUTATING row with its anchor, or a READ_ONLY_SUDO/EXCEPTIONS row with the reason)"
done <<<"$all_cgis"
if [ "$n_hits" -ge "$((MUTATING_FLOOR + READ_ONLY_FLOOR))" ]; then
    ok "sweep: $n_all CGIs read, $n_hits carry a trigger token, all ledgered"
else
    bad "sweep: only $n_hits of $n_all CGIs carry a trigger token (floor $((MUTATING_FLOOR + READ_ONLY_FLOOR))) — the trigger set or the stripping stopped seeing the files"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "cgi-csrf-policy: ALL OK — $n_mut mutating CGIs are POST-only + token-before-mutation; $n_ro read-only sudo, 1 exception, ledgered"
    exit 0
fi
echo "cgi-csrf-policy: $fails FAILURE(S)"
exit 1
