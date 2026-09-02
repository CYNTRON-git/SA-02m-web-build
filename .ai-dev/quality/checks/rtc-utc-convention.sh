#!/bin/bash
# Static gate for the SA-02m RTC time-frame convention.
#
# NOT exempt from comment-mutation-proof, and the reason it used to give was
# wrong: the sweep's fail-IF-PRESENT half is only pin 1. Pins 2-4 are
# presence-FLOORED (HWCLOCK_MIN_SITES, I2C_WRITE_MIN_FNS, DATE_SET_MIN_SITES),
# and the awk pass skips comment lines — so commenting the `--systohc --utc`
# write sites out of lib_rtc.sh drops the resolved count below the floor and
# this gate goes RED. That is now a registered case in comment-mutation-proof,
# not a claim (review Q8, 1.0.6.24).
#
# Convention home (rationale + the divergence it caused): the comment block in
# etc/sa02m-pre-start.sh at the DS3231 I2C load. In one line: the battery-backed
# chip holds UTC, so every conversion between system time and chip time names
# UTC explicitly.
#
# Defect this closes: www/network_config/cgi-bin/lib_rtc.sh wrote the chip with
# LOCAL time (`date '+%Y-%m-%d %H:%M:%S'`) while the read half treated it as UTC.
# sa02m-rtc-sync.timer fires every 30 min and the I2C fallback runs whenever
# /dev/rtc1 is absent, so the chip moved by one timezone on each write and the
# skew reached system time at the next boot. Second occurrence of the class in
# two releases — the pre-start read half was fixed first, the lib_rtc write half
# survived it — hence a mechanical gate rather than a third fix.
#
# Why the frame must be SPELLED, never inherited: nothing in this repo writes
# /etc/adjtime (pin 5 keeps it that way) — the file belongs to the base image.
# `hwclock` with no frame flag reads it, and `timedatectl set-local-rtc`, a
# different Armbian build, or a restored backup can flip it. An implicit read
# frame beside a hard-coded UTC write frame IS the asymmetry that produced the
# bug, so --utc on a READ site is load-bearing, not decoration: it makes the
# read/write pair self-consistent independent of a file the repo does not own.
# The trade this accepts, deliberately: on a board whose chip really did hold
# local time our read is now wrong by the offset instead of right. The repo
# declares ONE convention and enforces it on both sides rather than inheriting
# whichever frame an unowned file happens to state.
#
# Pins (non-vacuous: a missing file, a dead sweep or an empty extraction FAILS):
#   1. Every enumerated RTC file exists (a rename fails loudly instead of
#      passing on zero matches; pins 2-4 sweep for the addition case).
#   2. Every hwclock invocation that reads or writes the chip carries --utc.
#      Open-world over SWEEP_ROOTS. Call sites are resolved through the variable
#      each file binds the binary to (`HWC=/sbin/hwclock`, `hc=$(command -v
#      hwclock)`) — NOT ONE shipped call site spells `hwclock` on the invocation
#      line, so a literal grep sees nothing and would pass vacuously.
#   3. Every I2C chip-write function derives its timestamp with `date -u`.
#      Open-world: it sweeps for `write_*_i2c_datetime` DEFINITIONS across the
#      roots, so a new chip writer in a new file is judged too.
#   4. Every `date -s` fed by a chip-read value carries -u (the pre-start load
#      path — a bare `date -s` there is precisely how the divergence reached
#      system time).
#   5. Prohibition: nothing in the repo flips the frame to local
#      (`timedatectl set-local-rtc 1`, or LOCAL written into /etc/adjtime).
#   6. Prohibition: no chip-time-vs-system-time comparison that compares strings
#      instead of computing an epoch. `hwclock --show` prints LOCAL time with a
#      `+03:00` offset; a parser that drops the offset reports a false one-
#      timezone skew — that false reading already cost a debugging round on this
#      very fix. A comparison must go through `date -d … +%s`.
#
# Deliberate NON-flags (a gate that cries wolf here gets switched off):
#   - fake-hwclock: a timestamp file, no timezone frame at all. Masked out
#     before any matching, so its `save`/unit lines never reach a pin.
#   - Commented-out lines everywhere, and docs/ + .ai-dev/ are outside
#     SWEEP_ROOTS: CHANGELOG and BUGLOG quote the bad `date '+…'` form as the
#     defect they record, and this header names it in prose.
#   - `date -s` on an operator-supplied wall-clock value (apply.cgi's «Время»
#     form): that IS local time by contract. Pin 4 only judges an argument whose
#     variable name carries an RTC token.
#   - Year-plausibility tests (`[ "$DS3231_YEAR" -ge 2020 ]`): a chip value
#     against a constant, not against system time. Pin 6 fires only when the
#     other operand is a system-time source.
#   - The sudoers grant naming /sbin/hwclock: a path in an allow-list, not an
#     invocation — it carries no action flag, so pin 2 never sees it.
#   - `--localtime` is NOT accepted as "explicit" by pin 2. The repo declares one
#     convention; a local-frame chip access is the regression, not a variant.
#
# Regex style: bracket expressions ([$], [{], [+]) rather than backslash
# escapes throughout — several patterns reach awk through -v, which would eat a
# backslash before awk ever compiled the regex.
#
# Run: bash .ai-dev/quality/checks/rtc-utc-convention.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

CONVENTION_HOME=etc/sa02m-pre-start.sh
RTC_LIB=www/network_config/cgi-bin/lib_rtc.sh

# Every file that reads or writes the external RTC's time.
FILES=(
    "$RTC_LIB"
    "$CONVENTION_HOME"
    etc/sa02m-rtc-sync.sh
    www/network_config/cgi-bin/apply.cgi
)

# Roots the open-world pins sweep. Shipped surface only (see § NON-flags for
# why docs/ and .ai-dev/ are absent).
SWEEP_ROOTS=(etc scripts tools opt www install.sh)

# Non-vacuity floors. At the time of writing: 4 hwclock accesses in lib_rtc.sh
# (--show x2, --systohc x2) + 2 in sa02m-pre-start.sh (--show, -s); 2 I2C write
# functions (DS3231, PCF8563); 1 chip-sourced `date -s` (the pre-start load).
# Dropping below a floor means the sites moved — update this gate deliberately
# rather than letting it pass on a shrinking set.
HWCLOCK_MIN_SITES=6
I2C_WRITE_MIN_FNS=2
DATE_SET_MIN_SITES=1

UTC_FLAG_RE='(^|[[:space:]])(-u|--utc|--universal)([[:space:]]|$)'
RTC_VAR_RE='[$][{]?[A-Za-z0-9_]*(RTC|rtc|DS3231|ds3231|PCF8563|pcf8563)[A-Za-z0-9_]*[}]?'

fails=0
fail() { printf 'rtc-utc: FAIL  %s\n' "$*"; fails=$((fails + 1)); }
pass() { printf 'rtc-utc: ok    %s\n' "$*"; }
note() { printf 'rtc-utc: ---   %s\n' "$*"; }

# COMMENT-BLINDNESS AUDIT (1.0.6.24): this gate was already comment-safe — every
# sweep and every pin below runs through uncommented(). It keeps its OWN stripper
# rather than sourcing lib_check.sh because the same pass also neutralises
# fake-hwclock (a domain filter the shared lib has no business carrying).
#
# Uncommented lines only (shell, systemd conf and nginx conf all comment with #).
# fake-hwclock is neutralised in the same pass: it saves a timestamp to a file
# and has no frame, so its mentions must never reach the hwclock matcher.
uncommented() {
    grep -vE '^[[:space:]]*#' "$1" 2>/dev/null | sed 's/fake-hwclock/fake_clock_saver/g'
}

# ── 1. Every enumerated RTC file exists ────────────────────────────────────
missing=0
for f in "${FILES[@]}"; do
    [ -f "$f" ] || { fail "$f missing — file renamed/removed? update this gate"; missing=$((missing + 1)); }
done
[ "$missing" = 0 ] && pass "all ${#FILES[@]} enumerated RTC file(s) present"

# ── 2. Every hwclock chip access states the UTC frame ──────────────────────
# hwclock action options. The short forms are only ever tested on a line that
# already resolves to the hwclock binary, so `shutdown -r now` in the sudoers
# heredoc and `date -s` elsewhere cannot be mistaken for one.
HWC_ACTION_RE='(^|[[:space:]])(--show|--systohc|--hctosys|--set|--systz|--adjust|--predict|-w|-s|-r|-a)([[:space:]]|$)'

# Names a file binds the hwclock binary to. Without this the scan is blind:
# every shipped call site invokes "$HWC" / "$hc", never the literal path.
hwclock_vars() {
    uncommented "$1" \
        | grep -oE '(^|[[:space:]]|&&|\|\|)[A-Za-z_][A-Za-z0-9_]*=[^;&|]*hwclock' \
        | sed -E 's/^[^A-Za-z_]*//; s/=.*//' \
        | sort -u
}

hwc_files=()
while IFS= read -r p; do
    [ -n "$p" ] && hwc_files+=("$p")
done < <(grep -rlI 'hwclock' "${SWEEP_ROOTS[@]}" 2>/dev/null | sort -u)

hwc_sites=0
declare -A hwc_per_file=()
fails_before_pin2=$fails
for f in "${hwc_files[@]:-}"; do
    [ -n "${f:-}" ] && [ -f "$f" ] || continue
    # A line invokes hwclock when it names the binary or one of this file's
    # hwclock-bound variables.
    ref_re='hwclock'
    while IFS= read -r v; do
        [ -n "$v" ] || continue
        ref_re="${ref_re}|[\$][{]?${v}[}]?"
    done < <(hwclock_vars "$f")

    # One awk pass per file. The per-line shell loop this replaces spawned three
    # greps per line and did not finish inside two minutes on this repo.
    while IFS= read -r verdict; do
        [ -n "$verdict" ] || continue
        line=${verdict#*	}
        hwc_sites=$((hwc_sites + 1))
        hwc_per_file["$f"]=$(( ${hwc_per_file["$f"]:-0} + 1 ))
        case "$verdict" in
            BAD*)
                fail "$f: hwclock chip access without an explicit UTC frame: $line — with no flag it falls back to /etc/adjtime, a file this repo does not own; add --utc (convention home: $CONVENTION_HOME)"
                ;;
        esac
    done < <(awk -v ref="$ref_re" -v act="$HWC_ACTION_RE" -v utc="$UTC_FLAG_RE" '
        /^[[:space:]]*#/ { next }
        {
            line = $0
            gsub(/fake-hwclock/, "fake_clock_saver", line)
            if (line ~ ref && line ~ act) {
                sub(/^[[:space:]]+/, "", line)
                printf "%s\t%s\n", (line ~ utc ? "OK" : "BAD"), line
            }
        }
    ' "$f")
done

if [ "$hwc_sites" -lt "$HWCLOCK_MIN_SITES" ]; then
    fail "only $hwc_sites hwclock chip access(es) resolved across ${#hwc_files[@]} file(s) — expected >=${HWCLOCK_MIN_SITES}; the call sites moved or the variable resolution drifted, update this gate"
elif [ "$fails" -ne "$fails_before_pin2" ]; then
    note "$hwc_sites hwclock chip access(es) checked; $((fails - fails_before_pin2)) without an explicit UTC frame (above)"
else
    pass "$hwc_sites hwclock chip access(es) across ${#hwc_files[@]} swept file(s) carry --utc"
fi
# Both known callers must still contribute — a file that stops matching means
# the variable resolution broke, not that the code got safer.
for f in "$RTC_LIB" "$CONVENTION_HOME"; do
    [ "${hwc_per_file["$f"]:-0}" -gt 0 ] \
        || fail "$f contributed 0 hwclock access(es) — it is a known caller; the resolution or the file drifted, update this gate"
done

# ── 3. I2C chip writes derive their timestamp with `date -u` ───────────────
# Open-world over the roots: a new write_<chip>_i2c_datetime in a new file is
# judged too, not only the two lib_rtc.sh functions.
FN_DEF_RE='^write_[A-Za-z0-9_]*_i2c_datetime\(\)'

# Body of a shell function, by literal prefix match rather than a dynamic regex:
# building one would need \( \) \{, and awk strips those escapes before compiling
# (gawk warns, mawk on the CI runner reads `{` as an interval instead).
fn_body() {
    awk -v fn="$2" '
        substr($0, 1, length(fn) + 2) == fn "()" && $0 ~ /[{][[:space:]]*$/ { inside = 1; next }
        inside && /^[}]/ { inside = 0 }
        inside { print }
    ' "$1"
}

i2c_fns=0
fails_before_pin3=$fails
while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    file=${hit%%:*}
    fn=${hit#*:}
    fn=${fn%%(*}
    i2c_fns=$((i2c_fns + 1))
    stamps=0
    while IFS= read -r inv; do
        [ -n "$inv" ] || continue
        stamps=$((stamps + 1))
        # Here-string, not `printf … | grep -qE` — no producer to SIGPIPE under
        # pipefail. $inv is a single short line (borderline-immune), converted
        # for uniformity with the rest of the sweep. (.ai-dev/notes/quality-gate-environment.md)
        if ! grep -qE "$UTC_FLAG_RE" <<<"$inv"; then
            fail "$file: $fn() builds the chip timestamp in local time (\`${inv}…\`) while the chip holds UTC — every write then moves the clock by the timezone offset; use \`date -u\` (convention home: $CONVENTION_HOME)"
        fi
    done < <(fn_body "$file" "$fn" | grep -vE '^[[:space:]]*#' | grep -oE 'date[^)]*[+]%Y-%m-%d')
    if [ "$stamps" -eq 0 ]; then
        fail "$file: $fn() no longer builds a %Y-%m-%d timestamp — the writer moved or the extraction drifted; this gate can no longer judge its frame, update it"
    fi
done < <(grep -rnIE "$FN_DEF_RE" "${SWEEP_ROOTS[@]}" 2>/dev/null | sed 's/:[0-9]*:/:/' | sort -u)

if [ "$i2c_fns" -lt "$I2C_WRITE_MIN_FNS" ]; then
    fail "only $i2c_fns I2C chip-write function(s) found across ${SWEEP_ROOTS[*]} — expected >=${I2C_WRITE_MIN_FNS} (DS3231 + PCF8563); the writers moved or the definition pattern drifted, update this gate"
elif [ "$fails" -eq "$fails_before_pin3" ]; then
    pass "$i2c_fns I2C chip-write function(s) build their timestamp with date -u"
fi

# ── Shared prefilter for pins 4 and 6 ──────────────────────────────────────
# Every uncommented line naming an RTC-ish variable. Both pins are cheap over
# this (~40 lines); sweeping the roots line-by-line instead is not.
rtc_ref_lines=$(grep -rnIE "$RTC_VAR_RE" "${SWEEP_ROOTS[@]}" 2>/dev/null \
    | grep -vE ':[0-9]+:[[:space:]]*#')

# ── 4. `date -s` fed by a chip-read value carries -u ───────────────────────
date_set_sites=0
fails_before_pin4=$fails
while IFS= read -r verdict; do
    [ -n "$verdict" ] || continue
    date_set_sites=$((date_set_sites + 1))
    case "$verdict" in
        BAD*)
            fail "${verdict#BAD	} — the chip holds UTC, so a bare \`date -s\` lands the timezone offset in system time (convention home: $CONVENTION_HOME)"
            ;;
    esac
done < <(printf '%s\n' "$rtc_ref_lines" | awk -v utc="$UTC_FLAG_RE" -F: '
    NF < 3 { next }
    {
        file = $1; ln = $2
        # By offset, not a dynamic regex built from a path (see fn_body).
        content = substr($0, length(file) + length(ln) + 3)
        if (content !~ /(^|[[:space:]])date([[:space:]]|$)/) next
        if (content !~ /(^|[[:space:]])-s([[:space:]]|$)/) next
        sub(/^[[:space:]]+/, "", content)
        printf "%s\t%s:%s sets the system clock from a chip-read value without -u: %s\n", \
            (content ~ utc ? "OK" : "BAD"), file, ln, content
    }
')

if [ "$date_set_sites" -lt "$DATE_SET_MIN_SITES" ]; then
    fail "no chip-sourced \`date -s\` found across ${SWEEP_ROOTS[*]} — expected >=${DATE_SET_MIN_SITES} (the $CONVENTION_HOME DS3231 load); the load path moved or the extraction drifted, update this gate"
elif [ "$fails" -eq "$fails_before_pin4" ]; then
    pass "$date_set_sites chip-sourced \`date -s\` site(s) carry -u"
fi

# ── 5. Nothing flips the chip frame to local ───────────────────────────────
local_hits=$(grep -rnIE 'set-local-rtc[[:space:]]+(1|true|yes|on)|adjtime[^A-Za-z]*LOCAL|LOCAL[^A-Za-z]*adjtime' \
    "${SWEEP_ROOTS[@]}" 2>/dev/null | grep -vE ':[0-9]+:[[:space:]]*#')
if [ -n "$local_hits" ]; then
    fail "the chip frame is switched to local somewhere — the whole read/write pair assumes UTC: $(printf '%s' "$local_hits" | tr '\n' ' ')"
else
    pass "nothing under ${SWEEP_ROOTS[*]} switches the chip frame to local (set-local-rtc / adjtime LOCAL)"
fi

# ── 6. Chip-vs-system comparisons compute an epoch ─────────────────────────
CMP_OP_RE='[[:space:]](=|!=|==|-gt|-lt|-ge|-le|-eq|-ne)[[:space:]]'
SYS_TIME_RE='[$][(](date|/bin/date)|[$][{]?[A-Za-z0-9_]*(SYS|NOW|EPOCH)[A-Za-z0-9_]*[}]?'
EPOCH_RE='[+]%s|date[[:space:]]+-d|date[[:space:]]+--date'
cmp_sites=0
fails_before_pin6=$fails
while IFS= read -r verdict; do
    [ -n "$verdict" ] || continue
    cmp_sites=$((cmp_sites + 1))
    case "$verdict" in
        BAD*)
            fail "${verdict#BAD	} — hwclock prints local time with a +HH:MM offset, so a string compare reports a false one-timezone skew; convert both sides with \`date -d … +%s\` first"
            ;;
    esac
done < <(printf '%s\n' "$rtc_ref_lines" | awk -v cmp="$CMP_OP_RE" -v sys="$SYS_TIME_RE" -v epoch="$EPOCH_RE" -F: '
    NF < 3 { next }
    {
        file = $1; ln = $2
        # By offset, not a dynamic regex built from a path (see fn_body).
        content = substr($0, length(file) + length(ln) + 3)
        if (content !~ cmp) next
        if (content !~ sys) next
        sub(/^[[:space:]]+/, "", content)
        printf "%s\t%s:%s compares chip time against system time without an epoch: %s\n", \
            (content ~ epoch ? "OK" : "BAD"), file, ln, content
    }
')

# A prohibition pin, so zero sites is the healthy state (same shape as
# watchdog-cap's "no active ShutdownWatchdogSec="): it asserts that no naive
# comparison EXISTS, and it starts asserting the moment someone writes one.
if [ "$fails" -eq "$fails_before_pin6" ]; then
    pass "no naive chip-vs-system time comparison under ${SWEEP_ROOTS[*]} ($cmp_sites comparison site(s) found)"
fi

[ "$fails" = 0 ] || printf 'rtc-utc: %s check(s) failed — convention home: %s\n' "$fails" "$CONVENTION_HOME"
exit "$fails"
