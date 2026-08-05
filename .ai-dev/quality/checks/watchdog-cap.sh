#!/bin/bash
# Static gate for the SA-02m hardware watchdog policy.
#
# Policy home (values + rationale): etc/systemd/sa02m-watchdog.conf.
# Defect this closes: RebootWatchdogSec=2min against a sun4i-wdt whose hardware
# cap is 16s — systemd on the board rejects it with EINVAL on every shutdown
# ("Failed to set timeout to 2min: Invalid argument"), so the value never
# applied. Third occurrence of the class (BUGLOG 2026-08-05, and earlier the
# 25s->15s EINVAL); hence a mechanical gate rather than another fix.
#
# Pins (non-vacuous: a missing file or an empty extraction FAILS):
#   1. Every watchdog timeout this repo writes parses to <=16s, or is an
#      explicit disable. Open-world — it sweeps SWEEP_ROOTS rather than only
#      the enumerated list, so a NEW writer is judged too. Durations are
#      NORMALISED first: a naive numeric compare reads "2min" as 2 and would
#      pass the very bug above, and the spaced form "2 min" (valid per
#      systemd.time(7)) must reach the parser intact, not truncated to "2".
#   2. RuntimeWatchdogSec=15s in every copy that writes the drop-in.
#   3. RebootWatchdogSec=0 in every copy that sets it, and the set of copies
#      that set it is exactly the expected list.
#   4. No active ShutdownWatchdogSec= anywhere under SWEEP_ROOTS (deprecated
#      pre-v243 alias of RebootWatchdogSec — two knobs for one policy is how
#      the copies diverged).
#   5. The firstboot-overlay copy is byte-identical to the policy home.
#   6. scripts/01-system.sh installs the policy file from $ETC_REPO and carries
#      no inline watchdog heredoc (pins the dedup against regression).
#   7. Every enumerated path exists (a rename fails loudly instead of passing
#      on zero matches; pin 1's sweep covers the addition case).
#   8. Direct sysfs writes to /sys/class/watchdog/*/timeout are CLAMPED, never
#      a constant over the cap. Second mechanism of the same defect class:
#      pins 1-4 read systemd directives and were blind to `echo 30 >
#      .../watchdog0/timeout` in the feeder (etc/sa02m-watchdog-feed.sh and its
#      heredoc twin in tools/imaging/ssh-flash-safe.sh) — 30s asked of a 16s
#      driver, the rejection swallowed by `|| true`. Two halves: no numeric
#      literal above the cap is written, AND every file that writes the path
#      also reads max_timeout (the clamp itself). Non-vacuous: fewer than the
#      expected number of write sites FAILS.
#      Known limit: a static text pin, so it sees the path spelled literally;
#      a writer that builds the path in a variable escapes it (both shipped
#      writers spell it out, deliberately, so this pin can see them).
#
# Deliberate NON-flags (a gate that cries wolf here gets switched off):
#   - `=0` / `off` disables, including the imaging `systemctl set-property
#     --runtime Manager RuntimeWatchdogSec=0` lines — disabling is always safe.
#   - Commented-out lines, including the imaging
#     `sed 's/^RuntimeWatchdogSec=/#RuntimeWatchdogSec=/'` idiom and the
#     rationale prose in the policy header.
#   - Per-service `WatchdogSec=` in unit files: a different knob (service
#     liveness), not the hardware cap.
#   - READS of the sysfs timeout (`cat …/max_timeout`, `[ -w …/timeout ]`,
#     the hardening installer's diagnostic print): pin 8 judges writes only —
#     a redirect INTO the path. `max_timeout` is the clamp's input, never a
#     violation.
#   - A write whose value is a variable or expression (`echo "$WDT_WANT" >`):
#     that IS the clamped form. Pin 8's other half — the file must also read
#     max_timeout — is what keeps that from being a free pass.
#
# Run: bash .ai-dev/quality/checks/watchdog-cap.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

HW_CAP_SEC=16          # sun4i-wdt on the Allwinner A40i, from dmesg on device
POLICY_HOME=etc/systemd/sa02m-watchdog.conf
OVERLAY=tools/imaging/firstboot-overlay/etc/systemd/system.conf.d/sa02m-watchdog.conf

# Every file that writes, patches or disables a manager watchdog timeout.
FILES=(
    "$POLICY_HOME"
    "$OVERLAY"
    scripts/01-system.sh
    tools/system-hardening/install.sh
    tools/imaging/patch-firstboot-image.sh
    tools/imaging/autorun.sh
    tools/imaging/autorun-fel.sh
    tools/imaging/ssh-flash-safe.sh
    tools/imaging/make-image.sh
    tools/imaging/stream-after-cleanup.sh
    tools/imaging/compress-bin.sh
    tools/imaging/capture-image-win.py
    tools/debian-rootfs/pack-sa02m-image.sh
)

# Copies that must carry the runtime value literally (no repo available there).
RUNTIME_VALUE_FILES=(
    "$POLICY_HOME"
    "$OVERLAY"
    tools/system-hardening/install.sh
    tools/imaging/patch-firstboot-image.sh
    tools/imaging/autorun.sh
    tools/imaging/autorun-fel.sh
)
# Copies that set the shutdown/reboot watchdog at all.
REBOOT_VALUE_FILES=("${RUNTIME_VALUE_FILES[@]}")

# Roots pin 1 and pin 4 sweep. The shipped surface only — a NEW file setting a
# watchdog timeout must be judged even though this gate has never heard of it
# (the enumerated list above catches a rename, never an addition).
# docs/ is deliberately absent: docs/bugs/BUGLOG.md quotes `RebootWatchdogSec=2min`
# and `RuntimeWatchdogSec=25s` as the DEFECTS it records — sweeping it would make
# the gate fail on its own incident history. .ai-dev/ is absent for the same
# reason (this script and its registry row name the bad values in prose).
# A README under a swept root IS pinned on purpose: tools/system-hardening/
# README.md documenting `RebootWatchdogSec=2min` was one of the stale-prose
# copies this change fixed.
SWEEP_ROOTS=(etc scripts tools opt www install.sh)

fails=0
fail() { printf 'watchdog-cap: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'watchdog-cap: ok    %s\n' "$*"; }

# Uncommented lines only (systemd conf, shell and python all comment with #).
uncommented() { grep -vE '^[[:space:]]*#' "$1" 2>/dev/null; }

# Duration -> seconds, honouring systemd's suffixes. Empty output = unparseable.
# This is the line the defect turns on: "2min" must come back as 120, not 2.
# Whitespace inside the value is stripped first: systemd.time(7) allows a space
# between value and unit ("2 min"), and that form must reach the cap check as
# 120s, never as a bare 2.
dur_secs() {
    printf '%s' "$1" | awk '
        {
            gsub(/[[:space:]]+/, "")
            if (!match($0, /^[0-9]+(\.[0-9]+)?/)) exit 1
            num = substr($0, 1, RLENGTH) + 0
            unit = substr($0, RLENGTH + 1)
            m = -1
            if (unit == "" || unit == "s" || unit == "sec" || unit == "secs" \
                || unit == "second" || unit == "seconds")            m = 1
            else if (unit == "min" || unit == "m" || unit == "minute" \
                || unit == "minutes")                                m = 60
            else if (unit == "h" || unit == "hr" || unit == "hour" \
                || unit == "hours")                                  m = 3600
            else if (unit == "ms" || unit == "msec")                 m = 0.001
            else if (unit == "us" || unit == "usec")                 m = 0.000001
            if (m < 0) exit 1
            printf "%.6f", num * m
        }'
}

# ── 7. Every enumerated path exists ────────────────────────────────────────
missing=0
for f in "${FILES[@]}"; do
    [ -f "$f" ] || { fail "$f missing — file renamed/removed? update this gate"; missing=$((missing + 1)); }
done
if [ "$missing" = 0 ]; then
    pass "all ${#FILES[@]} enumerated watchdog-writing files present"
fi

# ── 1. Every value parses to <= the hardware cap, or is an explicit disable ──
# Open-world: every file under SWEEP_ROOTS that sets one of the three keys, in
# union with the enumerated list, so a NEW writer is judged too.
#
# The value pattern deliberately allows whitespace between number and unit
# ("2 min" — valid per systemd.time(7)) but only ever before LETTERS, never
# before a bare number. That is what keeps `set-property … RuntimeWatchdogSec=0
# 2>/dev/null` (the imaging disable) ending at `0` instead of swallowing the
# redirect, and stops a trailing comment or an adjacent quoted directive from
# being absorbed.
VAL_RE='(Runtime|Reboot|Shutdown)WatchdogSec=[0-9]*\.?[0-9]*([[:space:]]*[A-Za-z]+)?([[:space:]]*[0-9]+\.?[0-9]*[[:space:]]*[A-Za-z]+)*'

swept_files=()
while IFS= read -r p; do
    [ -n "$p" ] && swept_files+=("$p")
done < <(grep -rlIE '(Runtime|Reboot|Shutdown)WatchdogSec=' "${SWEEP_ROOTS[@]}" 2>/dev/null | sort -u)

scan_files=()
while IFS= read -r p; do
    [ -n "$p" ] && scan_files+=("$p")
done < <(
    {
        printf '%s\n' "${swept_files[@]}"
        printf '%s\n' "${FILES[@]}"
    } | sort -u
)

checked=0
fails_before_pin1=$fails
for f in "${scan_files[@]}"; do
    [ -f "$f" ] || continue
    while IFS= read -r tok; do
        [ -n "$tok" ] || continue
        key=${tok%%=*}
        val=${tok#*=}
        # `RuntimeWatchdogSec=` with no value / `.` — the sed comment-out and
        # sed-replace idioms, not a setting. Ignored on purpose (§ NON-flags).
        case "$val" in
            ''|'.') continue ;;
        esac
        checked=$((checked + 1))
        case "$val" in
            0|off|no|false) continue ;;
        esac
        secs=$(dur_secs "$val")
        if [ -z "$secs" ]; then
            fail "$f: $key=$val — unparseable duration; this gate cannot judge it, extend dur_secs()"
        elif ! awk -v s="$secs" -v cap="$HW_CAP_SEC" 'BEGIN { exit !(s <= cap) }'; then
            fail "$f: $key=$val (${secs%.*}s) exceeds the ${HW_CAP_SEC}s sun4i-wdt hardware cap — systemd rejects it with EINVAL and the value never applies (see $POLICY_HOME)"
        fi
    done < <(uncommented "$f" | grep -oE "$VAL_RE")
done
# Non-vacuous both ways: the SWEEP itself must resolve the shipped writers. A
# dead sweep (missing grep -I, renamed roots) would otherwise fall back to the
# enumerated union and pass silently — the closed-world behaviour this replaced.
if [ "${#swept_files[@]}" -lt 10 ]; then
    fail "the repo sweep over ${SWEEP_ROOTS[*]} resolved only ${#swept_files[@]} file(s) — expected >=10; the sweep roots or the grep drifted, update this gate"
elif [ "$checked" -lt 10 ]; then
    fail "only $checked watchdog value(s) extracted across ${#scan_files[@]} files — expected >=10; the extraction pattern drifted, update this gate"
elif [ "$fails" -ne "$fails_before_pin1" ]; then
    # Do not claim the set is within cap when the loop above just said it is not.
    printf 'watchdog-cap: ---   %s watchdog value(s) across %s swept file(s) checked; %s over cap/unparseable (above)\n' \
        "$checked" "${#scan_files[@]}" "$((fails - fails_before_pin1))"
else
    pass "$checked watchdog value(s) across ${#scan_files[@]} swept file(s) are <= ${HW_CAP_SEC}s or an explicit disable"
fi

# ── 2. RuntimeWatchdogSec=15s in every copy that writes the value ──────────
for f in "${RUNTIME_VALUE_FILES[@]}"; do
    [ -f "$f" ] || continue
    vals=$(uncommented "$f" | grep -oE 'RuntimeWatchdogSec=[0-9A-Za-z.]*' | grep -vE '=$|=\.' | sort -u)
    if [ -z "$vals" ]; then
        fail "$f no longer sets RuntimeWatchdogSec — it is one of the copies that must carry the value literally (no repo available in its context)"
    elif [ "$vals" != "RuntimeWatchdogSec=15s" ]; then
        fail "$f sets [$(printf '%s' "$vals" | tr '\n' ' ')] — every copy must match the policy home: $POLICY_HOME carries RuntimeWatchdogSec=15s"
    else
        pass "$f: RuntimeWatchdogSec=15s"
    fi
done

# ── 3. RebootWatchdogSec=0 in every copy that sets it ──────────────────────
for f in "${REBOOT_VALUE_FILES[@]}"; do
    [ -f "$f" ] || continue
    vals=$(uncommented "$f" | grep -oE 'RebootWatchdogSec=[0-9A-Za-z.]*' | grep -vE '=$|=\.' | sort -u)
    if [ -z "$vals" ]; then
        fail "$f no longer sets RebootWatchdogSec — the shutdown watchdog must be explicitly disabled, not left to the systemd default (10min)"
    elif [ "$vals" != "RebootWatchdogSec=0" ]; then
        fail "$f sets [$(printf '%s' "$vals" | tr '\n' ' ')] — the shutdown watchdog is disabled on purpose; see the trade-off in $POLICY_HOME"
    else
        pass "$f: RebootWatchdogSec=0"
    fi
done

# ── 4. No active ShutdownWatchdogSec= in the shipped surface ───────────────
shutdown_hits=$(grep -rnI 'ShutdownWatchdogSec' "${SWEEP_ROOTS[@]}" 2>/dev/null \
    | grep -vE ':[[:space:]]*#')
if [ -n "$shutdown_hits" ]; then
    fail "ShutdownWatchdogSec is set somewhere (deprecated pre-v243 alias of RebootWatchdogSec — one policy, one knob): $(printf '%s' "$shutdown_hits" | tr '\n' ' ')"
else
    pass "no active ShutdownWatchdogSec= under ${SWEEP_ROOTS[*]}"
fi

# ── 5. The firstboot-overlay copy is byte-identical to the policy home ─────
if [ ! -f "$POLICY_HOME" ] || [ ! -f "$OVERLAY" ]; then
    fail "cannot compare overlay against policy home — one of them is missing"
elif cmp -s "$POLICY_HOME" "$OVERLAY"; then
    pass "$OVERLAY is byte-identical to $POLICY_HOME"
else
    fail "$OVERLAY drifted from $POLICY_HOME — the staged media overlay is shipped as-is, no packer regenerates it; copy the policy home over it"
fi

# ── 6. 01-system.sh installs the policy file, no inline heredoc ────────────
INST=scripts/01-system.sh
if [ ! -f "$INST" ]; then
    fail "$INST missing — cannot check the installer"
else
    if grep -q 'install -m 644 "\$ETC_REPO/systemd/sa02m-watchdog.conf"' "$INST"; then
        pass "$INST installs the watchdog drop-in from \$ETC_REPO"
    else
        fail "$INST no longer installs \$ETC_REPO/systemd/sa02m-watchdog.conf — the main install path would silently ship a different policy again (the 2026-08-05 finding)"
    fi
    if uncommented "$INST" | grep -qE 'cat[[:space:]]*>[^>]*system\.conf\.d/sa02m-watchdog\.conf'; then
        fail "$INST re-grew an inline watchdog heredoc — it competes with the policy home for the same target path"
    else
        pass "$INST carries no inline watchdog heredoc"
    fi
fi

# ── 8. Direct sysfs timeout writes are clamped, not a constant over the cap ─
# The token immediately before the redirect is the value written:
#   `echo 30 > /sys/class/watchdog/watchdog0/timeout`   -> 30        (literal)
#   `echo "$WDT_WANT" >/sys/class/watchdog/watchdog0/timeout` -> $WDT_WANT
# A read (`cat …/max_timeout`, `[ -w …/timeout ]`) has no redirect INTO the
# path and never matches; `max_timeout` cannot match either — the path segment
# after the device dir must be exactly `timeout`.
WD_SYSFS_WRITE_RE='[^[:space:]>|;&]+[[:space:]]*>>?[[:space:]]*/sys/class/watchdog/[^/[:space:]]+/timeout'
WD_SYSFS_MAX_RE='/sys/class/watchdog/[^/[:space:]]+/max_timeout'
WD_SYSFS_MIN_SITES=2   # etc/sa02m-watchdog-feed.sh + the ssh-flash-safe.sh twin

wd_sysfs_files=()
while IFS= read -r p; do
    [ -n "$p" ] && wd_sysfs_files+=("$p")
done < <(grep -rlIE "$WD_SYSFS_WRITE_RE" "${SWEEP_ROOTS[@]}" 2>/dev/null | sort -u)

wd_writes=0
fails_before_pin8=$fails
for f in "${wd_sysfs_files[@]:-}"; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    file_writes=0
    while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        # Value = everything left of the first redirect, minus spaces/quotes.
        val=$(printf '%s' "${hit%%>*}" | tr -d "[:space:]\"'")
        wd_writes=$((wd_writes + 1))
        file_writes=$((file_writes + 1))
        case "$val" in
            ''|*[!0-9]*) continue ;;   # variable/expression — the clamped form
        esac
        if [ "$val" -gt "$HW_CAP_SEC" ]; then
            fail "$f: writes '$val' to /sys/class/watchdog/*/timeout — a constant above the ${HW_CAP_SEC}s sun4i-wdt cap; the driver rejects the request outright (and the error is swallowed by \`|| true\`). Clamp it: read max_timeout and write min(30, max_timeout)"
        fi
    done < <(uncommented "$f" | grep -oE "$WD_SYSFS_WRITE_RE")
    # The clamp itself. Deliberately an UNCOMMENTED read of the sysfs path, not
    # a bare `max_timeout` substring: the fix's own comment names max_timeout in
    # prose, so a substring test would let a file satisfy this half by talking
    # about the clamp while writing a constant.
    if [ "$file_writes" -gt 0 ] && ! uncommented "$f" | grep -qE "$WD_SYSFS_MAX_RE"; then
        fail "$f: writes /sys/class/watchdog/*/timeout without reading max_timeout — the requested timeout must be clamped to the driver cap, never asked for blind"
    fi
done

if [ "$wd_writes" -lt "$WD_SYSFS_MIN_SITES" ]; then
    fail "only $wd_writes direct sysfs watchdog-timeout write(s) found across ${SWEEP_ROOTS[*]} — expected >=${WD_SYSFS_MIN_SITES} (the feeder and its ssh-flash-safe.sh twin); the writers moved or the extraction pattern drifted, update this gate"
elif [ "$fails" -ne "$fails_before_pin8" ]; then
    # Do not claim the writes are clamped when the loop above just said otherwise.
    printf 'watchdog-cap: ---   %s direct sysfs timeout write(s) across %s file(s) checked; %s unclamped/over cap (above)\n' \
        "$wd_writes" "${#wd_sysfs_files[@]}" "$((fails - fails_before_pin8))"
else
    pass "$wd_writes direct sysfs timeout write(s) across ${#wd_sysfs_files[@]} file(s) are clamped to max_timeout (no constant above ${HW_CAP_SEC}s)"
fi

[ "$fails" = 0 ] || printf 'watchdog-cap: %s check(s) failed — policy home: %s\n' "$fails" "$POLICY_HOME"
exit "$fails"
