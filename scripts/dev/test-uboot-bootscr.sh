#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-uboot-bootscr.sh — byte-level regression test for the U-Boot boot.scr
# generator (tools/imaging/uboot-script.py + its two callers).
#
# Why this exists: TWICE (2026-08-03, 2026-08-05) this repo shipped an image
# whose boot.scr was a malformed U-Boot legacy script image — the 8-byte size
# table of the data section was missing — and the board stopped in U-Boot with
# no kernel, no beeper, no network. Every gate stayed green: the only assertion
# was `grep -aq threadirqs`, which a malformed file passes. On 2026-08-03 only
# the ARTIFACT was repaired (boot.scr copied from a donor), the GENERATOR was
# left broken, and the failure recurred. This test pins the FORMAT.
#
# The golden is anchored to the DONOR facts observed on a board that boots
# (docs/codesys-rt/README.md, BUGLOG 2026-08-03 16:24):
#   261 bytes total · 189-byte script · `00 00 00 bd 00 00 00 00` at 0x40 ·
#   script text starting at 0x48
# — NOT to the writer's own logic, so a shared misunderstanding between writer
# and test cannot pass. The `== 261` assertion is the padding discriminator:
# a writer that pads the script to 4 bytes emits 264 and FAILS.
#
# No mkimage, no device, no network: mkimage is absent on the dev boxes and in
# CI, so the gate must not depend on it (the pipeline requires it separately —
# tools/imaging/make-image.sh step 0).
#
# Run: bash scripts/dev/test-uboot-bootscr.sh    (bash + python, no deps)
#
# BOOTSCR_SRC is overridable so the test can be pointed at the PRE-FIX
# generator and must FAIL — a test that cannot fail is worse than none:
#   git show 2ee80d0:tools/imaging/patch-firstboot-image.sh > /tmp/old.sh
#   BOOTSCR_SRC=/tmp/old.sh bash scripts/dev/test-uboot-bootscr.sh   # must FAIL
# It resolves the writer under test from that file: the pre-fix shape is the
# inline `<<'PY'` heredoc inside build_bootscr(); the post-fix shape delegates
# to tools/imaging/uboot-script.py beside it. Neither found ⇒ the run FAILS
# (non-vacuity) rather than silently testing nothing.
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC=${BOOTSCR_SRC:-tools/imaging/patch-firstboot-image.sh}
[ -r "$SRC" ] || { echo "FAIL  $SRC not readable"; exit 1; }

# The VALIDATOR under test is always the shipped one — the negatives below ask
# "does today's validator reject yesterday's artifact?".
VALIDATOR=tools/imaging/uboot-script.py
[ -r "$VALIDATOR" ] || { echo "FAIL  $VALIDATOR not readable"; exit 1; }

PAYLOAD=etc/boot.cmd.sa02m.min
UNVERIFIED=etc/boot.cmd.sa02m

# Donor-observed constants (the anchor — never derived from the writer).
WANT_TOTAL=261
WANT_SCRIPT=189
WANT_IH_SIZE=197
WANT_TABLE=000000bd00000000
WANT_SHA256=20ffa6151e80a73625552f996051e801dc22448a9c9691848cc3a4c854c71abd

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys" >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "test-uboot-bootscr: no working python interpreter"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── resolve the writer under test (non-vacuous: neither shape ⇒ hard stop) ──
# Anchored to build_bootscr() and stopping at the first closing tag: the file
# carries other heredocs an unanchored scan would capture and RUN.
awk "/^build_bootscr\(\)/{g=1} g&&/<<'PY'/{f=1;next} /^PY\$/{if(f)exit} f" "$SRC" > "$T/writer.py"
if [ -s "$T/writer.py" ]; then
    WRITER_MODE=heredoc
    grep -q 'struct' "$T/writer.py" || bad "extraction: inline writer body has no struct — captured the wrong heredoc?"
    grep -qE 'systemctl|mkfs|losetup|reboot' "$T/writer.py" \
        && bad "extraction: inline writer captured foreign heredoc content"
elif [ -r "$(dirname "$SRC")/uboot-script.py" ]; then
    WRITER_MODE=module
    WRITER_MODULE="$(dirname "$SRC")/uboot-script.py"
    grep -q 'uboot-script.py' "$SRC" \
        || bad "non-vacuity: $SRC does not call uboot-script.py — generator restructured?"
else
    bad "non-vacuity: no writer found in $SRC (no inline PY heredoc, no uboot-script.py beside it)"
fi
[ "$fails" -eq 0 ] || { printf '\nFAIL  cannot reach the code under test\n'; exit 1; }
printf 'note  writer under test: %s (%s)\n' "$SRC" "$WRITER_MODE"

# build_scr <payload-path> <out-path> — run the SHIPPED writer.
build_scr() {
    if [ "$WRITER_MODE" = heredoc ]; then
        "$PY" "$T/writer.py" "$1" "$2" >/dev/null 2>&1
    else
        "$PY" "$WRITER_MODULE" build "$1" "$2" --name "SA-02m" >/dev/null 2>&1
    fi
}

# validate_scr <path> — the shipped validator; 0 = accepted.
validate_scr() { "$PY" "$VALIDATOR" validate "$1" >/dev/null 2>&1; }

# ── D. payload pins ────────────────────────────────────────────────────────
[ -f "$PAYLOAD" ] || bad "D1: $PAYLOAD missing"
if [ -f "$PAYLOAD" ]; then
    "$PY" - "$PAYLOAD" "$WANT_SCRIPT" "$WANT_SHA256" <<'PYCHK' || bad "D1: default payload pin"
import hashlib, sys
blob = open(sys.argv[1], "rb").read()
want_len, want_sha = int(sys.argv[2]), sys.argv[3]
got = hashlib.sha256(blob).hexdigest()
errs = []
if len(blob) != want_len: errs.append("length %d != %d" % (len(blob), want_len))
if b"\r" in blob:         errs.append("payload contains CR (CRLF checkout — see .gitattributes)")
if got != want_sha:       errs.append("sha256 %s != pinned %s" % (got, want_sha))
if errs:
    print("      " + "; ".join(errs))
    sys.exit(1)
PYCHK
    [ "$fails" -eq 0 ] && ok "D1: default payload is 189 bytes, LF-only, sha256 pinned"
fi

grep -q 'UNVERIFIED-ON-HARDWARE' "$UNVERIFIED" \
    && ok "D2: $UNVERIFIED still carries the UNVERIFIED-ON-HARDWARE marker" \
    || bad "D2: $UNVERIFIED lost its UNVERIFIED-ON-HARDWARE marker — promotion needs the procedure in docs/contracts/uboot-boot-script.md"

def_payload=$(unset SA02M_BOOTCMD; "$PY" "$VALIDATOR" resolve --repo-root .)
[ "$(basename "$def_payload")" = "boot.cmd.sa02m.min" ] \
    && ok "D3: the default payload resolves to boot.cmd.sa02m.min" \
    || bad "D3: default payload resolves to '$def_payload', want etc/boot.cmd.sa02m.min (the unverified variant must never become the default)"

# Relative on purpose: an absolute POSIX path is rewritten by the MSYS layer
# when this runs under Git Bash, which would test the shell, not the selector.
ovr_payload=$(SA02M_BOOTCMD="$UNVERIFIED" "$PY" "$VALIDATOR" resolve --repo-root .)
[ "$ovr_payload" = "$UNVERIFIED" ] \
    && ok "D3: SA02M_BOOTCMD selects the opt-in payload" \
    || bad "D3: SA02M_BOOTCMD ignored — got '$ovr_payload'"

# D3 runtime — RUN each producer's boot.scr function against a hostile mkimage
# shim that writes a MALFORMED artifact (the 2026-08-05 shape). Each must FAIL.
# A static grep for "validate" was worthless: adding `|| true` to the validate
# call left it green while the guard did nothing.
mkdir -p "$T/bin"
cat > "$T/bin/mkimage" <<'MKI'
#!/bin/bash
# hostile mkimage: emits header+payload with NO size table (the exact regression)
out=""; src=""
while [ $# -gt 0 ]; do
    case "$1" in
        -d) src="$2"; shift 2 ;;
        -C|-A|-T|-n) shift 2 ;;
        *) out="$1"; shift ;;
    esac
done
python3 - "$src" "$out" <<'PY'
import binascii, struct, sys, time
data = open(sys.argv[1], "rb").read()
name = b"SA-02m" + b"\0" * 26
t = int(time.time())
dcrc = binascii.crc32(data) & 0xFFFFFFFF
h = struct.pack(">7I4B32s", 0x27051956, 0, t, len(data), 0, 0, dcrc, 5, 2, 6, 0, name)
c = binascii.crc32(h) & 0xFFFFFFFF
h = struct.pack(">7I4B32s", 0x27051956, c, t, len(data), 0, 0, dcrc, 5, 2, 6, 0, name)
open(sys.argv[2], "wb").write(h + data)
PY
MKI
chmod +x "$T/bin/mkimage"

# run_producer_fn <file> <fn-name> <driver-body-file> — extract fn, run driver.
extract_fn() {
    awk -v fn="$2" 'index($0, fn "()")==1{f=1} f{print} f&&/^}$/{exit}' "$1" > "$3"
    [ -s "$3" ]
}

# patch-firstboot-image.sh :: build_bootscr — the validator must be fatal
if extract_fn tools/imaging/patch-firstboot-image.sh build_bootscr "$T/bb.sh"; then
    cat > "$T/bb-drive.sh" <<'DRV'
set -uo pipefail
REPO_ROOT="$1"; UBOOT_PY="$REPO_ROOT/tools/imaging/uboot-script.py"
BOOTCMD_ARG=""
log() { :; }
die() { echo "DIE: $*" >&2; exit 1; }
# shellcheck disable=SC1090
. "$2"
build_bootscr "$3"
DRV
    if PATH="$T/bin:$PATH" bash "$T/bb-drive.sh" "$PWD" "$T/bb.sh" "$T/hostile.scr" >/dev/null 2>&1; then
        bad "D3: build_bootscr ACCEPTED a malformed mkimage artifact — the validate call is not fatal"
    else
        ok "D3: build_bootscr dies on a malformed mkimage artifact (validator is load-bearing)"
    fi
else
    bad "D3: build_bootscr() not found in patch-firstboot-image.sh (non-vacuity)"
fi

# pack-sa02m-image.sh :: build_boot_scr — same, the second producer
if extract_fn tools/debian-rootfs/pack-sa02m-image.sh build_boot_scr "$T/bs.sh"; then
    cat > "$T/bs-drive.sh" <<'DRV'
set -uo pipefail
REPO_ROOT="$1"; UBOOT_PY="$REPO_ROOT/tools/imaging/uboot-script.py"
BOOTCMD_ARG=""
log() { :; }
die() { echo "DIE: $*" >&2; exit 1; }
# shellcheck disable=SC1090
. "$2"
build_boot_scr "$3" "$4"
DRV
    mkdir -p "$T/packwork"
    if PATH="$T/bin:$PATH" bash "$T/bs-drive.sh" "$PWD" "$T/bs.sh" "$T/hostile2.scr" "$T/packwork" >/dev/null 2>&1; then
        bad "D3: build_boot_scr ACCEPTED a malformed mkimage artifact — the validate call is not fatal"
    else
        ok "D3: build_boot_scr dies on a malformed mkimage artifact (validator is load-bearing)"
    fi
    # and the selector reaches it as an ARGUMENT (no env dependency)
    sel=$(BOOTCMD_ARG="$UNVERIFIED" "$PY" "$VALIDATOR" resolve --repo-root . --payload "$UNVERIFIED")
    [ "$sel" = "$UNVERIFIED" ] \
        && ok "D3: --bootcmd/--payload argument selects the opt-in payload" \
        || bad "D3: the --payload argument was ignored — got '$sel'"
else
    bad "D3: build_boot_scr() not found in pack-sa02m-image.sh (non-vacuity)"
fi

# Neither producer may depend on an environment variable across sudo.
for producer in tools/imaging/patch-firstboot-image.sh tools/debian-rootfs/pack-sa02m-image.sh; do
    grep -q 'resolve --repo-root .* --payload' "$producer" \
        && ok "D3: $(basename "$producer") selects its payload by argument" \
        || bad "D3: $(basename "$producer") does not pass --payload — an env-only selector is stripped by sudo"
done

# ── A. golden bytes for the proven payload ─────────────────────────────────
if build_scr "$PAYLOAD" "$T/boot.scr"; then
    "$PY" - "$T/boot.scr" "$PAYLOAD" "$WANT_TOTAL" "$WANT_IH_SIZE" "$WANT_TABLE" <<'PYCHK' || bad "A: golden bytes"
import binascii, struct, sys
blob    = open(sys.argv[1], "rb").read()
payload = open(sys.argv[2], "rb").read()
want_total, want_size, want_table = int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
errs = []

# Anchored to the donor artifact, not to the writer's logic. 261 (not 264) is
# what proves the script is NOT padded to a 4-byte boundary.
if len(blob) != want_total:
    errs.append("total %d != %d (64 header + 8 table + %d script; a missing size "
                "table gives %d, a padding writer gives 264)"
                % (len(blob), want_total, len(payload), 64 + len(payload)))
else:
    if blob[64:72].hex() != want_table:
        errs.append("size table @0x40 is %s, want %s" % (blob[64:72].hex(), want_table))
    if blob[72:] != payload:
        errs.append("script text @0x48 is not the payload verbatim")

    (magic, hcrc, stamp, size, load, entry, dcrc,
     os_id, arch, itype, comp) = struct.unpack(">7I4B", blob[:32])
    name = blob[32:64]
    if magic != 0x27051956: errs.append("magic 0x%08x" % magic)
    if size != want_size:   errs.append("ih_size %d != %d (8 + script)" % (size, want_size))
    if load != 0:           errs.append("ih_load %d != 0" % load)
    if entry != 0:          errs.append("ih_ep %d != 0" % entry)
    if (os_id, arch, itype, comp) != (5, 2, 6, 0):
        errs.append("os/arch/type/comp %s != (5, 2, 6, 0)" % ((os_id, arch, itype, comp),))
    if len(name) != 32 or name.rstrip(b"\0").count(b"\0"):
        errs.append("ih_name is not 32 bytes NUL-padded: %r" % name)

    want_hcrc = binascii.crc32(blob[:4] + b"\0" * 4 + blob[8:64]) & 0xFFFFFFFF
    if hcrc != want_hcrc:
        errs.append("ih_hcrc 0x%08x != recomputed 0x%08x" % (hcrc, want_hcrc))
    want_dcrc = binascii.crc32(blob[64:]) & 0xFFFFFFFF
    if dcrc != want_dcrc:
        errs.append("ih_dcrc 0x%08x != crc32(table+script) 0x%08x" % (dcrc, want_dcrc))
    if stamp <= 1600000000:
        errs.append("ih_time %d implausible" % stamp)

if errs:
    for e in errs: print("      " + e)
    sys.exit(1)
print("      %d bytes = 64 header + 8 table + %d payload" % (len(blob), len(payload)))
PYCHK
    validate_scr "$T/boot.scr" \
        && ok "A: the shipped validator accepts the shipped writer's output" \
        || bad "A: the shipped validator REJECTS the shipped writer's own output"
else
    bad "A: the writer under test failed to produce a boot.scr from $PAYLOAD"
fi

# ── B. the malformed shapes must be REJECTED ───────────────────────────────
# Each is hand-built here (never through the writer), so the negatives stay
# valid even if the writer is replaced.
"$PY" - "$PAYLOAD" "$T" <<'PYGEN' || bad "B: could not build the malformed fixtures"
import binascii, struct, sys
payload = open(sys.argv[1], "rb").read()
out = sys.argv[2]

def hdr(size, dcrc, stamp=1770000000):
    name = b"SA-02m" + b"\0" * 26
    h = struct.pack(">7I4B32s", 0x27051956, 0, stamp, size, 0, 0, dcrc, 5, 2, 6, 0, name)
    c = binascii.crc32(h) & 0xFFFFFFFF
    return struct.pack(">7I4B32s", 0x27051956, c, stamp, size, 0, 0, dcrc, 5, 2, 6, 0, name)

def w(name, blob):
    open("%s/%s" % (out, name), "wb").write(blob)

# 1 — no size table at all (the 2026-08-03 / 2026-08-05 artifact shape)
w("neg-no-table.scr", hdr(len(payload), binascii.crc32(payload) & 0xFFFFFFFF) + payload)

# 2 — table present, but ih_size/ih_dcrc computed over the script alone
table = struct.pack(">II", len(payload), 0)
w("neg-size-over-data.scr",
  hdr(len(payload), binascii.crc32(payload) & 0xFFFFFFFF) + table + payload)

# 3 — built from a CRLF payload (a CR reaches the kernel command line)
crlf = payload.replace(b"\n", b"\r\n")
data = struct.pack(">II", len(crlf), 0) + crlf
w("neg-crlf.scr", hdr(len(data), binascii.crc32(data) & 0xFFFFFFFF) + data)

# 4 — the script padded to a 4-byte boundary (the 264-byte writer)
pad = payload + b"\0" * ((-len(payload)) % 4)
data = struct.pack(">II", len(payload), 0) + pad
w("neg-padded.scr", hdr(len(data), binascii.crc32(data) & 0xFFFFFFFF) + data)

# 5 — the timestamp race: header mutated after hcrc was computed
good = hdr(len(payload) + 8, 0)
data = struct.pack(">II", len(payload), 0) + payload
good = hdr(len(data), binascii.crc32(data) & 0xFFFFFFFF, stamp=1770000000) + data
w("neg-hcrc-race.scr", good[:8] + struct.pack(">I", 1770000001) + good[12:])
PYGEN

check_rejected() {
    if [ ! -f "$T/$1" ]; then bad "B: fixture $1 missing"; return; fi
    if validate_scr "$T/$1"; then bad "B: the validator ACCEPTED $1 — $2"; else ok "B: rejected $1 ($2)"; fi
}
check_rejected neg-no-table.scr      "no 8-byte size table — the exact regression"
check_rejected neg-size-over-data.scr "ih_size/ih_dcrc computed over the script alone"
check_rejected neg-crlf.scr          "CRLF payload — a CR in the kernel command line"
check_rejected neg-padded.scr        "script padded to 4 bytes (264-byte shape)"
check_rejected neg-hcrc-race.scr     "ih_hcrc stale — the twice-sampled timestamp"

# ── E. the caller must actually REACH the fixed path, and stay fail-closed ─
# Everything above is defeated at the caller if the patch step is skipped. It
# WAS: `if [ -x "$SCRIPT_DIR/patch-firstboot-image.sh" ]` — but that file is
# tracked mode 100644 (110 of 115 *.sh in this repo are), so on a fresh
# Linux/WSL clone the test was FALSE, control fell to a stub branch that never
# wrote boot.scr and swallowed its own errors with `return 0`, and the image
# shipped with the donor's unvalidated boot.scr. A static grep for the `die`
# string would have certified a guard that never runs — so E RUNS the shipped
# invocation.

# E1 — no guard on the patch script may depend on the executable bit.
guards=$(grep -nE '\[ -[a-z] "\$(PATCH_SCRIPT|SCRIPT_DIR/patch-firstboot-image\.sh)"' \
         tools/imaging/make-image.sh || true)
if [ -z "$guards" ]; then
    bad "E1: no guard on the patch script found in make-image.sh — restructured? (non-vacuity)"
else
    if printf '%s\n' "$guards" | grep -q '\[ -x "\$'; then
        bad "E1: a guard on the patch script tests -x, but the file is tracked mode 644 — the patch step is skipped on a fresh clone"
    else
        ok "E1: no executable-bit guard on the patch script ($(printf '%s\n' "$guards" | grep -c .) guard(s) checked)"
    fi
fi

# E2 — RUN the shipped invocation against a sudo shim and a mode-644 stub.
# The shim is the assertion: it refuses anything not invoked through an
# interpreter, so `sudo "$PATCH_SCRIPT"` FAILS here on every platform — this
# does not rely on the filesystem honouring mode bits (Windows/Git Bash does
# not, which would make a mode-only probe silently vacuous). The shim also
# mimics sudo's env_reset, so it catches a stripped SA02M_BOOTCMD too.
awk '/^run_firstboot_patch\(\)/{f=1} f{print} f&&/^}$/{exit}' \
    tools/imaging/make-image.sh > "$T/rfp.sh"
if [ ! -s "$T/rfp.sh" ]; then
    bad "E2: run_firstboot_patch() not found in make-image.sh (non-vacuity — restructured?)"
else
    mkdir -p "$T/bin"
    cat > "$T/bin/sudo" <<'SHIM'
#!/bin/bash
# sudo shim, modelling the DEFAULT sudoers policy, not a permissive one:
#  - `setenv` is off by default, so a VAR=value assignment is REFUSED outright;
#  - env_reset wipes the caller's environment (simulated with `env -i`).
# It also records that it ran, so removing `sudo` from the invocation is caught.
touch "$(dirname "$0")/../sudo-ran"
case "${1:-}" in
    *=*) echo "shim: sudo refused env assignment '$1' (sudoers setenv is off by default)" >&2
         exit 96 ;;
esac
case "${1:-}" in
    bash|sh|*/bash|*/sh) ;;
    *) echo "shim: refused '$1' — not invoked through an interpreter, so it depends on the executable bit" >&2
       exit 97 ;;
esac
exec env -i PATH="$PATH" HOME="${HOME:-/tmp}" "$@"
SHIM
    chmod +x "$T/bin/sudo"

    # The stub stands in for patch-firstboot-image.sh, at the SAME tracked mode.
    # It records its argv and whether the env var reached it.
    cat > "$T/stub-patch.sh" <<'STUB'
#!/bin/bash
d="$(dirname "$0")"
printf 'ARGS=%s\nENV=%s\n' "$*" "${SA02M_BOOTCMD:-<unset>}" > "$d/probe.out"
STUB
    chmod 644 "$T/stub-patch.sh"
    if [ -x "$T/stub-patch.sh" ]; then
        printf 'note  E2: this filesystem does not honour mode bits; the interpreter assertion below still gates\n'
    fi

    cat > "$T/drive.sh" <<'DRV'
set -euo pipefail
PATCH_SCRIPT="$1"
log() { :; }
# shellcheck disable=SC1090
. "$2"
run_firstboot_patch "$3"
DRV

    # with the selector set — it must reach the child as an ARGUMENT, because
    # the shim (like real sudo) refuses env assignments and wipes the env
    rm -f "$T/probe.out" "$T/sudo-ran"
    if PATH="$T/bin:$PATH" SA02M_BOOTCMD=etc/boot.cmd.sa02m \
         bash "$T/drive.sh" "$T/stub-patch.sh" "$T/rfp.sh" "$T/img.img" 2>"$T/e2.err"; then
        got=$(grep '^ARGS=' "$T/probe.out" 2>/dev/null || echo "ARGS=<no output>")
        case "$got" in
            *"--bootcmd etc/boot.cmd.sa02m"*)
                ok "E2: the selector crosses sudo as an argument (no sudoers dependency)" ;;
            *)  bad "E2: the selector did not reach the patch script — got '$got'" ;;
        esac
        [ -f "$T/sudo-ran" ] \
            && ok "E2: the patch step actually goes through sudo" \
            || bad "E2: sudo was not invoked — the patch would run unprivileged and fail on mount"
    else
        bad "E2: the shipped patch invocation FAILED — $(tr -d '\n' < "$T/e2.err" | tail -c 200)"
    fi

    # without it — the fallback must stay fail-safe (the proven payload)
    rm -f "$T/probe.out"
    if PATH="$T/bin:$PATH" bash "$T/drive.sh" "$T/stub-patch.sh" "$T/rfp.sh" "$T/img.img" 2>/dev/null; then
        got=$(grep '^ARGS=' "$T/probe.out" 2>/dev/null || echo "ARGS=<no output>")
        case "$got" in
            *--bootcmd*) bad "E2: --bootcmd passed with no selector set — got '$got'" ;;
            *)           ok "E2: no selector ⇒ no override (falls back to the proven payload)" ;;
        esac
    else
        bad "E2: the patch invocation failed with no selector set"
    fi
fi

# E3 — the patch step is unconditional and fatal; the fail-open stub is gone.
grep -q '^run_firstboot_patch "\$RAW_IMG" || \\' tools/imaging/make-image.sh \
    && grep -A1 '^run_firstboot_patch "\$RAW_IMG" || \\' tools/imaging/make-image.sh | grep -q 'die ' \
    && ok "E3: a failed first-boot patch is fatal" \
    || bad "E3: make-image.sh does not die on a failed patch step — a bad boot.scr still ships"

grep -q 'patch_image' tools/imaging/make-image.sh \
    && bad "E3: the fail-open patch_image() stub is back — it never writes boot.scr and returns 0 on failure" \
    || ok "E3: the fail-open patch_image() stub is gone"

# E4 — the assertion the whole incident turned on.
grep -q "grep -aq 'threadirqs'" tools/imaging/patch-firstboot-image.sh \
    && bad "E4: the string-grep assertion is back — a malformed boot.scr passes it" \
    || ok "E4: the fail-open string-grep assertion is gone"

printf '\n'
if [ "$fails" -eq 0 ]; then
    printf 'PASS  boot.scr format pinned (261 = 64 header + 8 table + 189 payload)\n'
else
    printf 'FAIL  %d check(s) failed\n' "$fails"
fi
exit $(( fails > 0 ))
