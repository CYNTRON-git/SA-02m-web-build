#!/usr/bin/env bash
# Gate for the image-identity reset of the Alice controller enrollment (1.0.6.20)
# and of the HomeKit bridge identity (Part H, contract §7).
# comment-mutation-proof-exempt: behavioural harness - every guarantee is asserted by RUNNING the shipped code in a sandbox (files written, shim invocations, exit codes), so a commented-out line changes the measured behaviour instead of hiding behind a needle grep; its source-text greps are extraction/retarget sanity guards on its own scratch copy, which abort the run when the shipped block moves.
#
# The defect it pins is a CROSS-TENANT one: the gateway identifies a controller
# by its mTLS DN alone, so an image captured from a LINKED donor makes every
# clone the SAME controller as the donor — a customer's board surfacing in
# someone else's «Дом с Алисой». The donor's device document clones too (its
# uuid4 ids identify bindings). The cloud agent had this exact defect and was
# fixed on 2026-07-31 at four sites; this is the Alice twin, site for site.
#
# Contract (the one home of the clear-list): docs/contracts/image-identity-reset.md
#
# Part A — static pins (the wiring a behavioural run cannot see):
#   A1 all five sites carry the wipe; fewer than five = FAIL (non-vacuity)
#   A1c each site actually CALLS it — a defined-but-uninvoked wipe is inert,
#      and the two receivers are the only defence for a pre-fix stick
#   A2 each site's path set EQUALS the contract set for its form (offline sites
#      minus the /run row — asserted as an absence, so a dead line fails too)
#   A3 over-wipe tripwire: ca.crt.pem in no removal set, no rm -rf on the tree,
#      and the patch site keeps its CA-survived assertion
#   A3c every sidecar glob, EXPANDED FOR REAL, stays clear of ca.crt.pem
#   A4 the patch site's wipe AND assertion are UNCONDITIONAL (nesting depth 0)
#      and the assertion dies rather than warns — a `|| true` here is exactly
#      the fail-open that neutered the pre-2026-08-05 boot.scr guard
#   A5 under-scope tripwire: /var/lib/sa02m-alice is STILL in cleanup-donor.sh's
#      DENY lists (the obvious wrong fix is to loosen them)
#   A6 the factory-reset floor is intact — capture and factory reset are
#      OPPOSITE policies and this change must not unify them
#   A7 every systemctl in the on-device wipes is timeout-bounded
#   A8 cloud parity — the six cloud sites still carry their own wipe
#
# Part B — behavioural: the SHIPPED wipe functions are extracted and run
#   against a sandboxed fake rootfs seeded as a LINKED donor. A grep cannot see
#   that ca.crt.pem survives byte-identical, that the other client.conf keys are
#   untouched, that a second run is a no-op, or that a rootfs with no Alice
#   files at all exits 0 instead of aborting the capture BEFORE dd.
#
# RED battery (non-vacuity — each of these must FAIL this gate):
#   STREAM_SRC=<(git show HEAD~1:tools/imaging/stream-after-cleanup.sh)  ... A1/B
#   PATCH_SRC=<pre-fix patch-firstboot-image.sh>                          ... A1/A4
#   add ca.crt.pem to any wipe's rm list                                  ... A3/B
#   wrap the patch site's wipe in `if [ "$DO_ID_RESET" = 1 ]; then …`     ... A4
#   drop `timeout` from a systemctl call                                  ... A7
#   remove /var/lib/sa02m-alice from cleanup-donor.sh's DENY_TREES        ... A5
#   delete the CALL at any site, leaving the body intact                  ... A1c
#   drop a *.tmp / .alice-* sidecar row from a clear-list                 ... A2/B
#   widen a sidecar glob to *.pem* or *                                   ... A3c/B
# Override any site with <NAME>_SRC=/path to re-run it against another revision.
#
# Part H — the HomeKit twin (contract §7, wipe_homekit_identity() at the same
#   five sites): H0 the gate's on-device set IS the §7 clear-list table (read
#   from the contract); H1/H1c every file carries AND calls the wipe; H2 per-site
#   path-set equality (on-device: + /run, offline: + the wants link, - /run);
#   H3 no recursion, the software never named; H4 the patch site's wipe and its
#   fatal belt are unconditional and bare; H5 the `.hk-*` rows are the
#   daemon's own TMP_PREFIX (read from its constants, and used to seed HB's
#   sidecar); H7 on-device systemctl bounded. HB
#   runs each shipped wipe on a paired donor (the receivers under bash AND dash):
#   the state dir left empty but present, the conf read as disabled by the
#   daemon's OWN parser (sa02m_homekit.config) with interface/port kept, the
#   stop recorded BEFORE the store is removed, idempotent, a no-module rootfs
#   untouched, `ENABLED : on` forced off, a symlinked conf dropped without its
#   target being read or written, a symlinked state dir dropped without being
#   descended. HB2 runs the patch site's belt: passes a wiped image and a
#   no-module image, dies on each dirty dimension alone — including an
#   ABSOLUTE wants link, which `[ -e ]` alone resolves on the host, and a
#   dangling link at the store, whose entry globs expand to nothing.
#   RED observed 2026-09-27 with every site at HEAD 485f385: 29 FAIL, all in H
#   (A/B/B2 still green). Mutations: each call commented out -> H1c/H4; the
#   `.hk-*` glob commented out -> H2 + HB; the stop commented out -> H7 + HB
#   ordering; the belt's `-L` dropped -> HB2 wantslink.
#   A symlink AT the state dir (HB 'symlinked state dir', HB2 'dirlink'): the
#   contents glob followed it and emptied the victim dir, dot-files included —
#   on a mounted image an absolute link resolves on the imaging HOST. RED
#   observed 2026-09-27 on the six copies at 2e806a3: 9 FAIL (8 HB runs, each
#   victim dir emptied; HB2 passed a dangling link); GREEN with the `-L` guard.
#   H5 reads the sidecar prefix from sa02m_homekit.constants.TMP_PREFIX: set
#   to ".homekit-" on a scratch copy -> 10 FAIL (H5 + the sidecar surviving
#   all 8 HB runs + HB2's clean image), with the sites unchanged.
#
# No root, no device, no image, no loop mount.
set -u

HERE="$(cd "$(dirname "$0")/../.." && pwd)"

RESET_SRC="${RESET_SRC:-$HERE/tools/imaging/reset-alice-enrollment.sh}"
STREAM_SRC="${STREAM_SRC:-$HERE/tools/imaging/stream-after-cleanup.sh}"
PATCH_SRC="${PATCH_SRC:-$HERE/tools/imaging/patch-firstboot-image.sh}"
AUTORUN_SRC="${AUTORUN_SRC:-$HERE/tools/imaging/autorun.sh}"
AUTORUN_FEL_SRC="${AUTORUN_FEL_SRC:-$HERE/tools/imaging/autorun-fel.sh}"
SSH_FLASH_SRC="${SSH_FLASH_SRC:-$HERE/tools/imaging/ssh-flash-safe.sh}"
CLEANUP="${CLEANUP_SRC:-$HERE/tools/imaging/cleanup-donor.sh}"
PRESERVE="${PRESERVE_SRC:-$HERE/etc/sa02m-factory-defaults/lists/preserve.list}"
FACTORY="${FACTORY_SRC:-$HERE/etc/sa02m-factory-reset-runner.sh}"
CONTRACT="$HERE/docs/contracts/image-identity-reset.md"
CLOUD_RESET="$HERE/tools/imaging/reset-cloud-enrollment.sh"

fails=0
ok(){ printf '  ok    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }

for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" \
         "$AUTORUN_FEL_SRC" "$SSH_FLASH_SRC" "$CLEANUP" "$PRESERVE" \
         "$FACTORY" "$CONTRACT" "$CLOUD_RESET"; do
    [ -r "$f" ] || { echo "alice-image-identity: cannot read $f"; exit 1; }
done

# Extract a shell function by name: `name() {` at column 0 through `}` at
# column 0 — the same anchor the uboot and reload-handshake gates use.
extract_fn() { # <file> <name>
    awk -v n="$2" '$0 ~ "^"n"\\(\\) \\{" {f=1} f{print} f&&/^}$/{exit}' "$1"
}

# Every Alice path a wipe body names, comments stripped first (the prose around
# these functions legitimately names ca.crt.pem as the thing NOT to remove).
# Quotes are stripped before matching so the twin's glob form — a quoted root
# followed by a bare pattern, "$root/var/lib/sa02m-alice"/*.tmp — is extracted
# as one token, exactly like the on-device form.
paths_of() { # <file>
    extract_fn "$1" wipe_alice_enrollment \
        | sed 's/#.*$//' \
        | tr -d '"' \
        | grep -oE '/var/lib/sa02m-alice/[A-Za-z0-9._*-]+|/run/sa02m-alice/[A-Za-z0-9._*-]+|/etc/sa02m-alice[A-Za-z0-9._/*-]*|/etc/systemd/system/multi-user\.target\.wants/sa02m-alice-client\.service' \
        | sort -u
}

# The sidecar rows are GLOBS on purpose (F1): api.py writes "<path>.tmp" and
# only then os.replace()s it, config_store leaves a mkstemp ".alice-XXXXXX" —
# so a crash mid-link strands the private key or the donor's bindings under a
# name no literal list would catch.
ONDEVICE_SET="$(printf '%s\n' \
    '/etc/sa02m-alice-client.conf' \
    '/etc/sa02m-alice-devices.conf' \
    '/etc/sa02m-alice/.alice-*' \
    '/etc/sa02m-alice/sa02m-alice-client.conf' \
    '/etc/sa02m-alice/sa02m-alice-devices.conf' \
    '/run/sa02m-alice/*.tmp' \
    '/run/sa02m-alice/status.json' \
    '/var/lib/sa02m-alice/*.tmp' \
    '/var/lib/sa02m-alice/device.crt.pem' \
    '/var/lib/sa02m-alice/device.key.pem' \
    '/var/lib/sa02m-alice/pending_claim.json' | sort -u)"

OFFLINE_SET="$(printf '%s\n' \
    '/etc/sa02m-alice-client.conf' \
    '/etc/sa02m-alice-devices.conf' \
    '/etc/sa02m-alice/.alice-*' \
    '/etc/sa02m-alice/sa02m-alice-client.conf' \
    '/etc/sa02m-alice/sa02m-alice-devices.conf' \
    '/etc/systemd/system/multi-user.target.wants/sa02m-alice-client.service' \
    '/var/lib/sa02m-alice/*.tmp' \
    '/var/lib/sa02m-alice/device.crt.pem' \
    '/var/lib/sa02m-alice/device.key.pem' \
    '/var/lib/sa02m-alice/pending_claim.json' | sort -u)"

echo "A. static pins"

# ── A1 — all five sites carry the wipe ────────────────────────────────────
site_count=0
for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" \
         "$AUTORUN_FEL_SRC" "$SSH_FLASH_SRC"; do
    if [ -n "$(extract_fn "$f" wipe_alice_enrollment)" ]; then
        site_count=$((site_count + 1))
    else
        bad "(A1) no wipe_alice_enrollment() in ${f#"$HERE"/} — that site ships the donor's identity"
    fi
done
if [ "$site_count" -eq 6 ]; then
    ok "(A1) all six files of the five sites carry wipe_alice_enrollment()"
else
    bad "(A1) only $site_count/6 files carry the wipe — the enumeration is incomplete (contract §4)"
fi

# The clear-list must have a documented home, or the four copies have no anchor.
if grep -q 'device.key.pem' "$CONTRACT" && grep -q 'ca.crt.pem' "$CONTRACT"; then
    ok "(A1b) the contract states the clear-list and the keep-list"
else
    bad "(A1b) $CONTRACT does not carry the clear-list — the four copies lose their one home"
fi

# ── A2 — per-site path-set EQUALITY ───────────────────────────────────────
check_set() { # <label> <file> <expected>
    local got
    got="$(paths_of "$2")"
    if [ -z "$got" ]; then
        bad "(A2) $1: no Alice path extracted — vacuous"
        return
    fi
    if [ "$got" = "$3" ]; then
        ok "(A2) $1: path set matches the contract exactly"
    else
        bad "(A2) $1: path set differs from the contract
--- got ---
$got
--- expected ---
$3"
    fi
}
check_set "reset-alice-enrollment.sh (on-device)" "$RESET_SRC"   "$ONDEVICE_SET"
check_set "stream-after-cleanup.sh (on-device)"   "$STREAM_SRC"  "$ONDEVICE_SET"
check_set "patch-firstboot-image.sh (offline)"    "$PATCH_SRC"   "$OFFLINE_SET"
check_set "autorun.sh (offline)"                  "$AUTORUN_SRC" "$OFFLINE_SET"
check_set "autorun-fel.sh (offline)"              "$AUTORUN_FEL_SRC" "$OFFLINE_SET"
check_set "ssh-flash-safe.sh (offline)"           "$SSH_FLASH_SRC" "$OFFLINE_SET"

# ── A3 — the over-wipe tripwire ───────────────────────────────────────────
overwipe=0
for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" \
         "$AUTORUN_FEL_SRC" "$SSH_FLASH_SRC"; do
    body="$(extract_fn "$f" wipe_alice_enrollment | sed 's/#.*$//')"
    if printf '%s\n' "$body" | grep -q 'ca\.crt\.pem'; then
        bad "(A3) ${f#"$HERE"/} names ca.crt.pem inside the wipe — that is the SHARED gateway CA; removing it silently breaks every clone's client"
        overwipe=1
    fi
    if printf '%s\n' "$body" | grep -Eq 'rm[[:space:]]+-[a-z]*r[a-z]*f?[[:space:]]'; then
        bad "(A3) ${f#"$HERE"/} does a recursive rm inside the wipe — the tree must survive (tmpfiles.d owns the dir)"
        overwipe=1
    fi
done
[ "$overwipe" -eq 0 ] && ok "(A3) no site removes ca.crt.pem and none rm -rf's the state tree"

if grep -q 'alice_had_ca' "$PATCH_SRC" \
   && grep -A2 'alice_had_ca.*=.*1.*\[ ! -f' "$PATCH_SRC" | grep -q 'die '; then
    ok "(A3b) the patch site dies if ca.crt.pem was removed (real over-wipe guard)"
else
    bad "(A3b) the patch site lost its ca.crt.pem-survived assertion — an over-wipe would ship silently"
fi

# ── A3c — glob tightness, proven by real expansion, not by reading ────────
# The clear-list covers the atomic-write sidecars by SHAPE (F1). A glob is only
# safe while it cannot reach ca.crt.pem — so every glob any wipe names is
# expanded for real against a directory seeded with the whole steady-state set.
globdir="$(mktemp -d)"
mkdir -p "$globdir/var/lib/sa02m-alice" "$globdir/etc/sa02m-alice"
for s in ca.crt.pem device.crt.pem device.key.pem pending_claim.json \
         device.key.pem.tmp ca.crt.pem.tmp; do
    : > "$globdir/var/lib/sa02m-alice/$s"
done
: > "$globdir/etc/sa02m-alice/sa02m-alice-server.conf"
: > "$globdir/etc/sa02m-alice/.alice-Ab3xQ1"
glob_seen=0; glob_bad=0
for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" \
         "$AUTORUN_FEL_SRC" "$SSH_FLASH_SRC"; do
    for g in $(paths_of "$f" | grep -F '*'); do
        glob_seen=$((glob_seen + 1))
        # Unquoted on purpose: this is the pathname expansion under test. No
        # eval — the pattern comes from repo source and never reaches a shell
        # word beyond the glob itself.
        # shellcheck disable=SC2086
        for hit in $globdir$g; do
            [ -e "$hit" ] || continue
            if [ "${hit##*/}" = ca.crt.pem ]; then
                bad "(A3c) ${f#"$HERE"/}: the glob $g matches ca.crt.pem — the SHARED gateway CA would be deleted on every clone"
                glob_bad=1
            fi
        done
    done
done
rm -rf "$globdir"
if [ "$glob_seen" -lt 6 ]; then
    bad "(A3c) only $glob_seen glob(s) seen across the six files — the sidecar rows are missing (F1: device.key.pem.tmp would clone)"
elif [ "$glob_bad" -eq 0 ]; then
    ok "(A3c) all $glob_seen sidecar globs expand clear of ca.crt.pem (real expansion)"
fi

# ── A1c — the CALL, not just the definition (F2) ──────────────────────────
# A site whose function is defined but never invoked is inert, and the two
# receivers are the ONLY defence for a stick captured before this fix. A1 and
# Part B both work on the function BODY; neither asks whether the shipped
# script runs it.
call_pin() { # <label> <file> <enclosing fn|-> <call regex>
    local lbl=$1 file=$2 fn=$3 re=$4 scope
    if [ "$fn" = "-" ]; then
        scope="$(sed 's/#.*$//' "$file")"
    else
        scope="$(extract_fn "$file" "$fn" | sed 's/#.*$//')"
        if [ -z "$scope" ]; then
            bad "(A1c) $lbl: $fn() could not be extracted — vacuous, cannot judge the call"
            return
        fi
    fi
    if printf '%s\n' "$scope" | grep -Eq "$re"; then
        ok "(A1c) $lbl: the wipe is actually CALLED"
    else
        bad "(A1c) $lbl: wipe_alice_enrollment is DEFINED BUT NEVER CALLED — that site is inert and nothing else would say so"
    fi
}
call_pin "reset-alice-enrollment.sh" "$RESET_SRC"  "-" \
    '^wipe_alice_enrollment[[:space:]]*$'
# The stream call must live INSIDE prepare_clone_ids: that is what makes the
# donor-side wipe follow --no-id-reset exactly like the cloud twin (plan §7.4).
call_pin "stream-after-cleanup.sh (in prepare_clone_ids)" "$STREAM_SRC" "prepare_clone_ids" \
    '^[[:space:]]*wipe_alice_enrollment[[:space:]]*$'
call_pin "autorun.sh (in apply_firstboot_wiring)" "$AUTORUN_SRC" "apply_firstboot_wiring" \
    '^[[:space:]]*wipe_alice_enrollment[[:space:]]+"\$root"[[:space:]]*$'
call_pin "autorun-fel.sh (in apply_firstboot_wiring)" "$AUTORUN_FEL_SRC" "apply_firstboot_wiring" \
    '^[[:space:]]*wipe_alice_enrollment[[:space:]]+"\$root"[[:space:]]*$'
call_pin "ssh-flash-safe.sh (on written rootfs)" "$SSH_FLASH_SRC" "-" \
    '^[[:space:]]*wipe_alice_enrollment[[:space:]]+"\$MNT"[[:space:]]*$'
# …and one level up: the receivers' enclosing function must itself be invoked
# on the freshly written rootfs, or the whole block is dead code.
for f in "$AUTORUN_SRC" "$AUTORUN_FEL_SRC"; do
    if sed 's/#.*$//' "$f" | grep -Eq '^[[:space:]]*apply_firstboot_wiring[[:space:]]+"\$MNT"'; then
        ok "(A1c) ${f#"$HERE"/}: apply_firstboot_wiring runs on the written rootfs"
    else
        bad "(A1c) ${f#"$HERE"/}: apply_firstboot_wiring is never invoked — the receiver's whole wiring, cloud wipe included, is dead"
    fi
done

# ── A4 — the patch site is UNCONDITIONAL and fatal ────────────────────────
# Nesting depth at the call lines: anything but 0 means a flag or a branch can
# skip the security clear (§7.4 — the flag governs the DONOR, not the artifact).
# Block keywords are counted in command position only, after comments and
# quoted strings are masked — so a one-line `if …; then …; fi`, a `case` arm or
# the word "if" inside a message cannot skew the count.
depths="$(awk '
  {
    l=$0; sub(/#.*$/,"",l)
    iscall = (l ~ /^[[:space:]]*(wipe_alice_enrollment|assert_alice_enrollment_clean)[[:space:]]+"\$mnt"/)
    gsub(/"[^"]*"/, "S", l)
    gsub(/\047[^\047]*\047/, "S", l)
    o = gsub(/(^|[;&|(=[:space:]])(if|for|while|until|case)[[:space:]]/, "X", l)
    c = gsub(/(^|[;&|[:space:]])(fi|done|esac)([;&|[:space:]]|$)/, "X", l)
    if (iscall) print d+0
    d += o - c
  }' "$PATCH_SRC")"
n_calls="$(printf '%s\n' "$depths" | grep -c '[0-9]' || true)"
n_nested="$(printf '%s\n' "$depths" | grep -cv '^0$' || true)"
if [ "$n_calls" -ne 2 ]; then
    bad "(A4) expected exactly 2 top-level calls (wipe + assert) in patch-firstboot-image.sh, found $n_calls"
elif [ "$n_nested" -ne 0 ]; then
    bad "(A4) the patch site's wipe/assert sits inside a conditional (depth $depths) — a security clear must not be skippable"
else
    ok "(A4) the patch site's wipe AND assertion are unconditional (depth 0)"
fi

# …and the CALL must be bare: `assert … || true` leaves the depth at 0 and the
# function's own `die` intact while making the whole belt decorative.
swallowed="$(grep -E '^[[:space:]]*(wipe_alice_enrollment|assert_alice_enrollment_clean)[[:space:]]+"\$mnt"[[:space:]]*([|&]|;|$)' "$PATCH_SRC" \
              | grep -E '\|\||&&|; *(true|:)' || true)"
if [ -z "$swallowed" ]; then
    ok "(A4c) the wipe/assert calls are bare — no || true swallowing the failure"
else
    bad "(A4c) the patch site's call swallows its own failure: $swallowed"
fi

assert_body="$(extract_fn "$PATCH_SRC" assert_alice_enrollment_clean)"
if [ -z "$assert_body" ]; then
    bad "(A4b) assert_alice_enrollment_clean() not found — the belt has no buckle"
elif printf '%s\n' "$assert_body" | grep -q 'die ' \
     && ! printf '%s\n' "$assert_body" | sed 's/#.*$//' | grep -q '|| true'; then
    ok "(A4b) the assertion dies and swallows nothing"
else
    bad "(A4b) the assertion warns instead of dying, or carries a || true — the fail-open that neutered the boot.scr guard"
fi

# ── A5 — the under-scope tripwire (the obvious WRONG fix) ─────────────────
deny_trees="$(sed -n '/^DENY_TREES=(/,/^)/p' "$CLEANUP")"
deny_lits="$(sed -n '/^DENY_LITERALS=(/,/^)/p' "$CLEANUP")"
if [ -z "$deny_trees" ] || [ -z "$deny_lits" ]; then
    bad "(A5) cleanup-donor.sh DENY arrays could not be extracted — vacuous"
elif printf '%s\n' "$deny_trees" | grep -q '^[[:space:]]*/var/lib/sa02m-alice[[:space:]]*$' \
     && printf '%s\n' "$deny_lits" | grep -q '^[[:space:]]*/var/lib/sa02m-alice[[:space:]]*$'; then
    ok "(A5) /var/lib/sa02m-alice is still DENY'd in the glob-driven junk collector"
else
    bad "(A5) /var/lib/sa02m-alice left cleanup-donor.sh's DENY lists — loosening a safety list to delete one file re-opens the whole tree to every future glob (contract §4)"
fi

# ── A6 — the factory-reset floor (the OPPOSITE policy) ────────────────────
pres_ok=1
for p in /var/lib/sa02m-alice/device.crt.pem /var/lib/sa02m-alice/device.key.pem \
         /var/lib/sa02m-alice/ca.crt.pem; do
    grep -qx "$p" "$PRESERVE" || { pres_ok=0; bad "(A6) $p is no longer preserved on factory reset"; }
done
if [ "$pres_ok" -eq 1 ]; then
    ok "(A6) factory reset still preserves the board's OWN cert triplet"
fi
if grep -q 'is_preserved /var/lib/sa02m-alice/device.crt.pem' "$FACTORY" \
   && grep -q 'alice cert not preserved' "$FACTORY"; then
    ok "(A6b) the factory-reset runner still ASSERTS the preserve policy"
else
    bad "(A6b) the factory-reset runner lost its preserve assertion — capture and factory reset must stay opposite policies"
fi

# ── A7 — every systemctl the on-device wipes introduce is bounded ─────────
for f in "$RESET_SRC" "$STREAM_SRC"; do
    body="$(extract_fn "$f" wipe_alice_enrollment | sed 's/#.*$//')"
    n="$(printf '%s\n' "$body" | grep -c 'systemctl' || true)"
    if [ "${n:-0}" -lt 2 ]; then
        bad "(A7) ${f#"$HERE"/}: only ${n:-0} systemctl call(s) in the wipe — the unit is not stopped+disabled, or the extraction is vacuous"
    else
        unbounded="$(printf '%s\n' "$body" | grep 'systemctl' | grep -v 'timeout[[:space:]]\+[0-9]\+[[:space:]]\+systemctl' || true)"
        if [ -z "$unbounded" ]; then
            ok "(A7) ${f#"$HERE"/}: all $n systemctl calls are timeout-bounded"
        else
            bad "(A7) ${f#"$HERE"/}: unbounded systemctl — a wedged unit would hang the capture BEFORE dd: $unbounded"
        fi
    fi
done

# ── A8 — cloud parity (protects the shipped 2026-07-31 fix at zero cost) ──
cloud_ok=1
for f in "$CLOUD_RESET" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" \
         "$AUTORUN_FEL_SRC" "$SSH_FLASH_SRC"; do
    grep -q 'device_secret' "$f" || { cloud_ok=0; bad "(A8) ${f#"$HERE"/} lost its cloud enrollment wipe (the twin defect, fixed 2026-07-31)"; }
done
[ "$cloud_ok" -eq 1 ] && ok "(A8) all six cloud-wipe sites intact"

echo
echo "B. behavioural — the shipped wipes against a sandboxed linked donor"

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys' >/dev/null 2>&1; then PY="$p"; break; fi
done

CA_BYTES='SHARED-GATEWAY-CA-DO-NOT-DELETE'
SERVER_CONF='[gateway]
wss_url = wss://alice.cyntron.ru/controller/socket.io
http_url = https://alice.cyntron.ru
sio_path = /socket.io'

seed_donor() { # <root> — a board LINKED to the gateway, with bench bindings
    local r=$1
    mkdir -p "$r/var/lib/sa02m-alice" "$r/etc/sa02m-alice" "$r/run/sa02m-alice" \
             "$r/etc/systemd/system/multi-user.target.wants"
    printf 'DONOR-PRIVATE-KEY\n'          > "$r/var/lib/sa02m-alice/device.key.pem"
    printf 'DONOR-CERT-CN=sa02m-1135\n'   > "$r/var/lib/sa02m-alice/device.crt.pem"
    printf '%s\n' "$CA_BYTES"             > "$r/var/lib/sa02m-alice/ca.crt.pem"
    printf '{"claim_token":"donor-secret"}\n' > "$r/var/lib/sa02m-alice/pending_claim.json"
    printf '{"state":"connected"}\n'      > "$r/run/sa02m-alice/status.json"
    # Atomic-write sidecars, exactly as a crash / ENOSPC mid-link leaves them
    # (F1). device.key.pem.tmp is the private key itself under another name.
    printf 'DONOR-PRIVATE-KEY\n'          > "$r/var/lib/sa02m-alice/device.key.pem.tmp"
    printf 'DONOR-CERT-CN=sa02m-1135\n'   > "$r/var/lib/sa02m-alice/device.crt.pem.tmp"
    printf '{"claim_token":"donor-secret"}\n' > "$r/var/lib/sa02m-alice/pending_claim.json.tmp"
    # A partial CA write: the sidecar goes, the real ca.crt.pem must NOT.
    printf 'PARTIAL-CA\n'                 > "$r/var/lib/sa02m-alice/ca.crt.pem.tmp"
    printf '{"devices": [{"id": "d1"}]}\n' > "$r/etc/sa02m-alice/.alice-Ab3xQ1"
    printf '{"state":"connected"}\n'      > "$r/run/sa02m-alice/status.json.tmp"
    printf '%s\n' "$SERVER_CONF"          > "$r/etc/sa02m-alice/sa02m-alice-server.conf"
    # A populated device document with two uuid4 ids — and the exact leftover
    # the 2026-08-27 audit found inside the in-tree golden image.
    cat > "$r/etc/sa02m-alice/sa02m-alice-devices.conf" <<'EOF'
{
  "rooms": [{"id": "3f1c2b90-1f2e-4a7d-9f11-8f6a2c0b1d33", "name": "Цех"}],
  "devices": [
    {"id": "d1", "name": "Lab Switch", "topic": "sa02m/dtv/1"},
    {"id": "9c2e5a41-7b0d-4c88-a1e2-5d3f7b9c0e12", "name": "СЭ фаза A", "topic": "sa02m/ce/1"}
  ]
}
EOF
    # The stand-down marker of a donor that was unlinked in the cloud before
    # the image was taken. IDENTITY, not configuration (contract §2, the twin
    # of the cloud half's §6): carried into a clone it makes every board's card
    # read «отвязано в облаке» instead of «нет сертификата».
    cat > "$r/etc/sa02m-alice/sa02m-alice-client.conf" <<'EOF'
[client]
client_enabled = true
log_level = INFO
mqtt_host = 127.0.0.1
mqtt_port = 1883
unlinked_at = 2026-09-02T10:00:00Z
unlinked_reason = unlinked
unlinked_reason_text = controller_unlink
EOF
    # The legacy flat layout, which the factory-reset and update runners still know.
    cp "$r/etc/sa02m-alice/sa02m-alice-devices.conf" "$r/etc/sa02m-alice-devices.conf"
    cp "$r/etc/sa02m-alice/sa02m-alice-client.conf"  "$r/etc/sa02m-alice-client.conf"
    # A symlink where the platform makes one; a plain file is equivalent for the
    # -e / rm -f semantics under test (Windows dev hosts have no symlink right).
    ln -s /etc/systemd/system/sa02m-alice-client.service \
        "$r/etc/systemd/system/multi-user.target.wants/sa02m-alice-client.service" 2>/dev/null \
        || printf 'unit-wants-link\n' > "$r/etc/systemd/system/multi-user.target.wants/sa02m-alice-client.service"
}

snapshot() { # <root> — content-only tree fingerprint (idempotency proof)
    ( cd "$1" 2>/dev/null || return 0
      find . \( -type f -o -type l \) | LC_ALL=C sort | while read -r p; do
          printf '%s %s\n' "$p" "$(cksum < "$p" 2>/dev/null || echo link)"
      done )
}

# Build a runnable probe for an ON-DEVICE site: the shipped body names absolute
# paths, so it is sed-retargeted into the sandbox. The retarget is verified —
# an un-retargeted line would edit the REAL /etc of the host running this gate.
make_ondevice_probe() { # <src> <out>
    local body
    body="$(extract_fn "$1" wipe_alice_enrollment)"
    [ -n "$body" ] || return 90
    body="$(printf '%s\n' "$body" \
        | sed -e 's#\(^\|[[:space:]"]\)/var/lib/sa02m-alice#\1"$SBROOT"/var/lib/sa02m-alice#g' \
              -e 's#\(^\|[[:space:]"]\)/etc/sa02m-alice#\1"$SBROOT"/etc/sa02m-alice#g' \
              -e 's#\(^\|[[:space:]"]\)/run/sa02m-alice#\1"$SBROOT"/run/sa02m-alice#g')"
    # Verify the retarget: mask the sandbox prefix to a token first, then any
    # SURVIVING absolute Alice path means the body would edit the host's /etc.
    if printf '%s\n' "$body" | sed 's/#.*$//' | sed 's#"\$SBROOT"/#@SB@/#g' \
         | grep -Eq '(^|[[:space:]"])/(var/lib|etc|run)/sa02m-alice'; then
        return 91   # un-retargeted absolute path: refuse to run
    fi
    {
        echo '#!/usr/bin/env bash'
        echo 'set -euo pipefail'          # the real scripts run under these flags
        echo 'SBROOT="$1"; REC="$2"'
        echo 'systemctl(){ printf "%s\n" "systemctl $*" >> "$REC"; return 0; }'
        echo 'timeout(){ shift; "$@"; }'
        echo 'log(){ :; }'
        printf '%s\n' "$body"
        echo 'wipe_alice_enrollment'
    } > "$2"
}

make_offline_probe() { # <src> <out>
    local body
    body="$(extract_fn "$1" wipe_alice_enrollment)"
    [ -n "$body" ] || return 90
    {
        echo '#!/usr/bin/env bash'
        echo 'set -euo pipefail'
        printf '%s\n' "$body"
        echo 'wipe_alice_enrollment "$1"'
    } > "$2"
}

# ── the per-site assertions ───────────────────────────────────────────────
assert_wiped() { # <label> <root> <form: ondevice|offline>
    local lbl=$1 r=$2 form=$3 f

    for f in device.crt.pem device.key.pem pending_claim.json; do
        if [ -e "$r/var/lib/sa02m-alice/$f" ]; then
            bad "(B/$lbl) $f survived — the exposure is still in the artifact"
        else
            ok "(B/$lbl) $f removed"
        fi
    done

    # F1 — the sidecar class. device.key.pem.tmp IS the private key; a
    # literal-name clear-list clones it and the fatal belt waves it through.
    for f in device.key.pem.tmp device.crt.pem.tmp pending_claim.json.tmp ca.crt.pem.tmp; do
        if [ -e "$r/var/lib/sa02m-alice/$f" ]; then
            bad "(B/$lbl) sidecar $f survived — the donor's identity clones under a name one character off the literal list"
        else
            ok "(B/$lbl) sidecar $f removed"
        fi
    done
    if [ -e "$r/etc/sa02m-alice/.alice-Ab3xQ1" ]; then
        bad "(B/$lbl) the config_store mkstemp sidecar survived — it holds the donor's bindings"
    else
        ok "(B/$lbl) the .alice-* conf sidecar removed"
    fi

    # The mirrored risk: an over-wipe is its own defect and no test of a
    # DELETION feature would otherwise catch it.
    if [ "$(cat "$r/var/lib/sa02m-alice/ca.crt.pem" 2>/dev/null || echo MISSING)" = "$CA_BYTES" ]; then
        ok "(B/$lbl) ca.crt.pem present and byte-identical (shared gateway CA kept)"
    else
        bad "(B/$lbl) ca.crt.pem was removed or altered — every clone's client loses its trust anchor"
    fi
    if [ -d "$r/var/lib/sa02m-alice" ]; then
        ok "(B/$lbl) the state dir itself survives"
    else
        bad "(B/$lbl) the state dir was removed — tmpfiles.d owns it, the wipe must not"
    fi

    for f in "$r/etc/sa02m-alice/sa02m-alice-devices.conf" "$r/etc/sa02m-alice-devices.conf"; do
        if [ ! -f "$f" ]; then
            bad "(B/$lbl) ${f#"$r"} disappeared — reset, never delete (the CGI and the client expect it)"
            continue
        fi
        if grep -q '"id"' "$f"; then
            bad "(B/$lbl) a donor binding survived in ${f#"$r"} — every clone would carry the donor's devices"
        elif grep -q '"devices"' "$f"; then
            ok "(B/$lbl) ${f#"$r"} reset to the empty document"
        else
            bad "(B/$lbl) ${f#"$r"} is not a device document any more"
        fi
        if [ -n "$PY" ]; then
            "$PY" -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$f" 2>/dev/null \
                && ok "(B/$lbl) ${f#"$r"} still parses as JSON" \
                || bad "(B/$lbl) ${f#"$r"} is not valid JSON — the client would fall back and log an error"
        fi
    done

    for f in "$r/etc/sa02m-alice/sa02m-alice-client.conf" "$r/etc/sa02m-alice-client.conf"; do
        if [ ! -f "$f" ]; then
            bad "(B/$lbl) ${f#"$r"} disappeared"
            continue
        fi
        if grep -Eqi '^[[:space:]]*client_enabled[[:space:]]*=[[:space:]]*(true|1|yes|on)' "$f"; then
            bad "(B/$lbl) client_enabled is still on in ${f#"$r"} — a clone would dial the gateway unattended"
        else
            ok "(B/$lbl) client_enabled forced false in ${f#"$r"}"
        fi
        if grep -q 'mqtt_port = 1883' "$f" && grep -q 'log_level = INFO' "$f"; then
            ok "(B/$lbl) the other client.conf keys are untouched (configuration, not identity)"
        else
            bad "(B/$lbl) the wipe damaged configuration keys in ${f#"$r"}"
        fi
        # The stand-down marker: identity, so it must be GONE. Non-vacuous —
        # seed_donor writes all three keys and B3 below proves the seed carries
        # them, so an assertion that passed on an unseeded file would fail there.
        left="$(grep -cE '^[[:space:]]*unlinked_(at|reason|reason_text)[[:space:]]*=' "$f")"
        if [ "${left:-0}" -eq 0 ]; then
            ok "(B/$lbl) the stand-down marker is gone from ${f#"$r"} (identity, contract §2)"
        else
            bad "(B/$lbl) $left stand-down marker key(s) survived in ${f#"$r"} — every clone
  would boot with the donor's «отвязано в облаке» instead of «нет сертификата»"
        fi
    done

    if [ "$(cat "$r/etc/sa02m-alice/sa02m-alice-server.conf" 2>/dev/null)" = "$SERVER_CONF" ]; then
        ok "(B/$lbl) server.conf byte-identical (gateway URLs are configuration)"
    else
        bad "(B/$lbl) server.conf was modified — it holds gateway URLs, not identity"
    fi

    case "$form" in
      ondevice)
        if [ -e "$r/run/sa02m-alice/status.json" ] || [ -e "$r/run/sa02m-alice/status.json.tmp" ]; then
            bad "(B/$lbl) /run/sa02m-alice/status.json (or its .tmp sidecar) survived — the stale link truth would be served"
        else
            ok "(B/$lbl) the tmpfs status file and its sidecar are cleared"
        fi ;;
      offline)
        # The /run row is deliberately ABSENT offline (tmpfs never reaches the
        # image); a dead line added for "completeness" is caught here.
        if [ -e "$r/run/sa02m-alice/status.json" ]; then
            ok "(B/$lbl) the offline wipe does not touch /run (never in the image)"
        else
            bad "(B/$lbl) the offline wipe cleared /run — a dead line: tmpfs is not on /dev/mmcblk2"
        fi
        if [ -e "$r/etc/systemd/system/multi-user.target.wants/sa02m-alice-client.service" ]; then
            bad "(B/$lbl) the unit is still enabled in the image — a clone would dial the gateway on first boot"
        else
            ok "(B/$lbl) the client unit is disabled in the image"
        fi ;;
    esac
}

run_site() { # <label> <src> <form>
    local lbl=$1 src=$2 form=$3 root="$SANDBOX/$1" probe="$SANDBOX/$1.probe.sh" rc=0
    local rec="$SANDBOX/$1.systemctl.log"
    rm -rf "$root"; mkdir -p "$root"; : > "$rec"
    seed_donor "$root"

    if [ "$form" = ondevice ]; then
        make_ondevice_probe "$src" "$probe" || rc=$?
    else
        make_offline_probe "$src" "$probe" || rc=$?
    fi
    case "$rc" in
      90) bad "(B/$lbl) wipe_alice_enrollment() could not be extracted — nothing behavioural was run"; return ;;
      91) bad "(B/$lbl) the sandbox retarget failed — refusing to run (it would have edited the host's real /etc)"; return ;;
    esac

    if [ "$form" = ondevice ]; then
        bash "$probe" "$root" "$rec" || { bad "(B/$lbl) the wipe exited non-zero on a linked donor"; return; }
    else
        bash "$probe" "$root" || { bad "(B/$lbl) the wipe exited non-zero on a linked donor"; return; }
    fi
    ok "(B/$lbl) the shipped wipe ran clean on a linked donor"
    assert_wiped "$lbl" "$root" "$form"

    if [ "$form" = ondevice ]; then
        if grep -q 'systemctl stop sa02m-alice-client' "$rec" \
           && grep -q 'systemctl disable sa02m-alice-client' "$rec"; then
            ok "(B/$lbl) the client unit was stopped AND disabled (recorded by the shim)"
        else
            bad "(B/$lbl) stop/disable not recorded — the donor's client keeps running against a wiped identity, and a clone boots enabled"
        fi
    fi

    # Idempotency: re-running the reset (an operator re-run, or a receiver on an
    # already-clean image) must change nothing and exit 0.
    local before after
    before="$(snapshot "$root")"
    if [ "$form" = ondevice ]; then bash "$probe" "$root" "$rec" >/dev/null 2>&1; else bash "$probe" "$root" >/dev/null 2>&1; fi
    rc=$?
    after="$(snapshot "$root")"
    if [ "$rc" -eq 0 ] && [ "$before" = "$after" ]; then
        ok "(B/$lbl) idempotent — a second run is a byte-identical no-op, exit 0"
    else
        bad "(B/$lbl) not idempotent (rc=$rc) — a re-run changed the tree"
    fi

    # Degraded input: a donor that never linked, legacy layout absent. THE
    # common case, and the one an unguarded `sed -i` aborts on — under
    # `set -euo pipefail` that kills the capture mid-stream, BEFORE dd.
    local bare="$SANDBOX/$1.bare"
    rm -rf "$bare"; mkdir -p "$bare/etc" "$bare/var/lib"
    if [ "$form" = ondevice ]; then bash "$probe" "$bare" "$rec" >/dev/null 2>&1; else bash "$probe" "$bare" >/dev/null 2>&1; fi
    rc=$?
    created="$(find "$bare" -name '*alice*' 2>/dev/null | head -n 5)"
    if [ "$rc" -eq 0 ] && [ -z "$created" ]; then
        ok "(B/$lbl) a rootfs with no Alice files at all: exit 0, creates nothing"
    else
        bad "(B/$lbl) degraded rootfs: rc=$rc created='$created' — an abort here kills the capture before dd"
    fi
}

# ── B3 — the seed itself carries what B asserts is removed ────────────────
# Without this the marker assertion above could pass on a file that never had a
# marker: a check that passes because it tested nothing is the defect.
seedprobe="$SANDBOX/seedcheck"
rm -rf "$seedprobe"; mkdir -p "$seedprobe"; seed_donor "$seedprobe"
seeded=0
for f in "$seedprobe/etc/sa02m-alice/sa02m-alice-client.conf"          "$seedprobe/etc/sa02m-alice-client.conf"; do
    n="$(grep -cE '^[[:space:]]*unlinked_(at|reason|reason_text)[[:space:]]*=' "$f" 2>/dev/null || echo 0)"
    [ "${n:-0}" -eq 3 ] && seeded=$((seeded + 1))
done
if [ "$seeded" -eq 2 ]; then
    ok "(B3) the donor fixture really carries the three stand-down marker keys in both layouts"
else
    bad "(B3) the donor fixture seeds the marker in only $seeded/2 client.conf layouts — the removal assertion would pass vacuously"
fi

run_site reset  "$RESET_SRC"       ondevice
run_site stream "$STREAM_SRC"      ondevice
run_site patch  "$PATCH_SRC"       offline
run_site autorun "$AUTORUN_SRC"    offline
run_site autorunfel "$AUTORUN_FEL_SRC" offline
run_site sshflash "$SSH_FLASH_SRC" offline

# ── B2 — the shipped ASSERTION, run for real ──────────────────────────────
# The belt itself: it must PASS a wiped rootfs and DIE on one that still holds
# the donor's key (a capture that ran with --no-id-reset, or a pre-fix stream).
# ── A9 — every site drops the stand-down marker (static, all six files) ──
marker_sites=0
for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" \
         "$AUTORUN_FEL_SRC" "$SSH_FLASH_SRC"; do
    body="$(extract_fn "$f" wipe_alice_enrollment | sed 's/#.*$//')"
    if printf '%s
' "$body" | grep -q 'unlinked_at'        && printf '%s
' "$body" | grep -q 'unlinked_reason'        && printf '%s
' "$body" | grep -q 'unlinked_reason_text'; then
        marker_sites=$((marker_sites + 1))
    else
        bad "(A9) ${f#"$HERE"/} does not drop all three stand-down marker keys — a clone from an unlinked donor inherits its «отвязано в облаке»"
    fi
done
[ "$marker_sites" -eq 6 ] && ok "(A9) all six sites drop the stand-down marker keys"

echo
echo "B2. the shipped fail-closed assertion (patch site)"
abody="$(extract_fn "$PATCH_SRC" assert_alice_enrollment_clean)"
if [ -z "$abody" ]; then
    bad "(B2) assert_alice_enrollment_clean() could not be extracted"
else
    aprobe="$SANDBOX/assert.probe.sh"
    {
        echo '#!/usr/bin/env bash'
        echo 'set -euo pipefail'
        echo 'die(){ echo "FATAL: $*" >&2; exit 1; }'
        printf '%s\n' "$abody"
        echo 'assert_alice_enrollment_clean "$1"'
    } > "$aprobe"

    if bash "$aprobe" "$SANDBOX/patch" >/dev/null 2>&1; then
        ok "(B2) the assertion PASSES a correctly wiped image"
    else
        bad "(B2) the assertion fails a correctly wiped image — it would abort every capture"
    fi

    dirty="$SANDBOX/dirty"
    rm -rf "$dirty"; mkdir -p "$dirty"; seed_donor "$dirty"
    if bash "$aprobe" "$dirty" >/dev/null 2>&1; then
        bad "(B2) the assertion PASSED an image still holding the donor's private key — the belt is dead"
    else
        ok "(B2) the assertion DIES on an unwiped image (the --no-id-reset / pre-fix-stream belt)"
    fi

    # One dimension at a time, so a single over-broad check cannot fake a pass.
    for one in key doc enabled unit sidecar confsidecar; do
        d="$SANDBOX/dirty-$one"; rm -rf "$d"; mkdir -p "$d"; seed_donor "$d"
        # start from a clean state, then re-dirty exactly one dimension
        rm -f "$d/var/lib/sa02m-alice/device.crt.pem" \
              "$d/var/lib/sa02m-alice/device.key.pem" \
              "$d/var/lib/sa02m-alice/pending_claim.json" \
              "$d/var/lib/sa02m-alice"/*.tmp \
              "$d/etc/sa02m-alice"/.alice-* \
              "$d/etc/systemd/system/multi-user.target.wants/sa02m-alice-client.service"
        for c in "$d/etc/sa02m-alice/sa02m-alice-devices.conf" "$d/etc/sa02m-alice-devices.conf"; do
            printf '%s\n' '{' '  "rooms": [],' '  "devices": []' '}' > "$c"
        done
        for c in "$d/etc/sa02m-alice/sa02m-alice-client.conf" "$d/etc/sa02m-alice-client.conf"; do
            sed -i 's/^[[:space:]]*client_enabled[[:space:]]*=.*/client_enabled = false/' "$c"
        done
        case "$one" in
          key)     printf 'DONOR-PRIVATE-KEY\n' > "$d/var/lib/sa02m-alice/device.key.pem" ;;
          doc)     printf '%s\n' '{"rooms": [], "devices": [{"id": "d1", "name": "Lab Switch"}]}' \
                       > "$d/etc/sa02m-alice/sa02m-alice-devices.conf" ;;
          enabled) sed -i 's/^client_enabled = false/client_enabled = true/' \
                       "$d/etc/sa02m-alice-client.conf" ;;
          unit)    printf 'unit-wants-link\n' \
                       > "$d/etc/systemd/system/multi-user.target.wants/sa02m-alice-client.service" ;;
          # F1: the belt must die on the private key under its sidecar name —
          # a blacklist of the three literals passes this image.
          sidecar) printf 'DONOR-PRIVATE-KEY\n' \
                       > "$d/var/lib/sa02m-alice/device.key.pem.tmp" ;;
          confsidecar) printf '{"devices": [{"id": "d1"}]}\n' \
                       > "$d/etc/sa02m-alice/.alice-Ab3xQ1" ;;
        esac
        if bash "$aprobe" "$d" >/dev/null 2>&1; then
            bad "(B2) the assertion missed a dirty '$one' — that dimension is unguarded"
        else
            ok "(B2) the assertion catches a dirty '$one'"
        fi
    done
fi

# ══════════════════════════════════════════════════════════════════════════
# H — the HomeKit half (contract §7): wipe_homekit_identity() at the same five
# sites. Same shape as A/B/B2 above, its own clear-list (the pairing store is
# wiped WHOLE — contents of /var/lib/sa02m-homekit, dot-sidecars included),
# its own keep-list (the dirs, interface/port, the unit, the software).
# ══════════════════════════════════════════════════════════════════════════
echo
echo "H. HomeKit bridge identity (contract §7)"

HK_SITES="$RESET_SRC $STREAM_SRC $PATCH_SRC $AUTORUN_SRC $AUTORUN_FEL_SRC $SSH_FLASH_SRC"

hk_paths_of() { # <file>
    extract_fn "$1" wipe_homekit_identity \
        | sed 's/#.*$//' \
        | tr -d '"' \
        | grep -oE '/var/lib/sa02m-homekit/[A-Za-z0-9._*-]+|/run/sa02m-homekit/[A-Za-z0-9._*-]+|/etc/sa02m-homekit/[A-Za-z0-9._*-]+|/etc/systemd/system/multi-user\.target\.wants/sa02m-homekit\.service' \
        | sort -u
}

# ── H0 — the harness's sets are the CONTRACT's, not a second copy ─────────
# The clear-list table of §7 is read from the contract itself: every
# backticked HomeKit path in the rows between "Clear-list HomeKit" and
# "Keep-list HomeKit". The unit row names no path (on the board it is a
# `systemctl disable`, offline the wants link), so it is asserted by name.
hk_table="$(awk '/^\*\*Clear-list HomeKit/{f=1;next} /^\*\*Keep-list HomeKit/{f=0} f' "$CONTRACT")"
HK_CONTRACT_SET="$(printf '%s\n' "$hk_table" | grep '^|' \
    | grep -oE '`/(var/lib|run|etc)/sa02m-homekit/[^`]*`' | tr -d '`' | sort -u)"
HK_ONDEVICE_SET="$(printf '%s\n' \
    '/etc/sa02m-homekit/sa02m-homekit.conf' \
    '/run/sa02m-homekit/*' \
    '/var/lib/sa02m-homekit/*' \
    '/var/lib/sa02m-homekit/.hk-*' | sort -u)"
HK_OFFLINE_SET="$(printf '%s\n' \
    '/etc/sa02m-homekit/sa02m-homekit.conf' \
    '/etc/systemd/system/multi-user.target.wants/sa02m-homekit.service' \
    '/var/lib/sa02m-homekit/*' \
    '/var/lib/sa02m-homekit/.hk-*' | sort -u)"
if [ -z "$HK_CONTRACT_SET" ]; then
    bad "(H0) no HomeKit clear-list table found in $CONTRACT §7 — the sites have no anchor (vacuous)"
elif [ "$HK_CONTRACT_SET" = "$HK_ONDEVICE_SET" ]; then
    ok "(H0) the on-device set here IS the contract's §7 clear-list (read from the table)"
else
    bad "(H0) the contract's §7 clear-list and this gate's on-device set differ
--- contract ---
$HK_CONTRACT_SET
--- gate ---
$HK_ONDEVICE_SET"
fi
if printf '%s\n' "$hk_table" | grep '^|' | grep -q 'sa02m-homekit\.service' \
   && printf '%s\n' "$hk_table" | grep '^|' | grep -q 'multi-user.target.wants'; then
    ok "(H0) the contract's unit row names sa02m-homekit.service and the offline wants link"
else
    bad "(H0) the contract's §7 table lost its unit row — the offline wants-link removal has no anchor"
fi

# ── H1 — every file of the five sites carries the wipe, and CALLS it ──────
hk_count=0
for f in $HK_SITES; do
    if [ -n "$(extract_fn "$f" wipe_homekit_identity)" ]; then
        hk_count=$((hk_count + 1))
    else
        bad "(H1) no wipe_homekit_identity() in ${f#"$HERE"/} — that site ships the donor's HomeKit accessory"
    fi
done
[ "$hk_count" -eq 6 ] && ok "(H1) all six files of the five sites carry wipe_homekit_identity()"

hk_call_pin() { # <label> <file> <enclosing fn|-> <call regex>
    local lbl=$1 file=$2 fn=$3 re=$4 scope
    if [ "$fn" = "-" ]; then
        scope="$(sed 's/#.*$//' "$file")"
    else
        scope="$(extract_fn "$file" "$fn" | sed 's/#.*$//')"
        [ -n "$scope" ] || { bad "(H1c) $lbl: $fn() could not be extracted — vacuous"; return; }
    fi
    if printf '%s\n' "$scope" | grep -Eq "$re"; then
        ok "(H1c) $lbl: the HomeKit wipe is actually CALLED"
    else
        bad "(H1c) $lbl: wipe_homekit_identity is DEFINED BUT NEVER CALLED — that site is inert"
    fi
}
hk_call_pin "reset-alice-enrollment.sh" "$RESET_SRC" "-" '^wipe_homekit_identity[[:space:]]*$'
# Inside prepare_clone_ids, like the Alice wipe: the donor-side wipe follows
# --no-id-reset (contract §5); the patch site below is the unconditional belt.
hk_call_pin "stream-after-cleanup.sh (in prepare_clone_ids)" "$STREAM_SRC" "prepare_clone_ids" \
    '^[[:space:]]*wipe_homekit_identity[[:space:]]*$'
hk_call_pin "autorun.sh (in apply_firstboot_wiring)" "$AUTORUN_SRC" "apply_firstboot_wiring" \
    '^[[:space:]]*wipe_homekit_identity[[:space:]]+"\$root"[[:space:]]*$'
hk_call_pin "autorun-fel.sh (in apply_firstboot_wiring)" "$AUTORUN_FEL_SRC" "apply_firstboot_wiring" \
    '^[[:space:]]*wipe_homekit_identity[[:space:]]+"\$root"[[:space:]]*$'
hk_call_pin "ssh-flash-safe.sh (on written rootfs)" "$SSH_FLASH_SRC" "-" \
    '^[[:space:]]*wipe_homekit_identity[[:space:]]+"\$MNT"[[:space:]]*$'

# ── H2 — per-site path-set EQUALITY with the contract set for its form ────
hk_check_set() { # <label> <file> <expected>
    local got
    got="$(hk_paths_of "$2")"
    if [ -z "$got" ]; then
        bad "(H2) $1: no HomeKit path extracted — vacuous"
    elif [ "$got" = "$3" ]; then
        ok "(H2) $1: HomeKit path set matches the contract exactly"
    else
        bad "(H2) $1: HomeKit path set differs from the contract
--- got ---
$got
--- expected ---
$3"
    fi
}
hk_check_set "reset-alice-enrollment.sh (on-device)" "$RESET_SRC"       "$HK_ONDEVICE_SET"
hk_check_set "stream-after-cleanup.sh (on-device)"   "$STREAM_SRC"      "$HK_ONDEVICE_SET"
hk_check_set "patch-firstboot-image.sh (offline)"    "$PATCH_SRC"       "$HK_OFFLINE_SET"
hk_check_set "autorun.sh (offline)"                  "$AUTORUN_SRC"     "$HK_OFFLINE_SET"
hk_check_set "autorun-fel.sh (offline)"              "$AUTORUN_FEL_SRC" "$HK_OFFLINE_SET"
hk_check_set "ssh-flash-safe.sh (offline)"           "$SSH_FLASH_SRC"   "$HK_OFFLINE_SET"

# ── H3 — keep-list tripwire: no recursive rm, the software is never named ─
hk_over=0
for f in $HK_SITES; do
    body="$(extract_fn "$f" wipe_homekit_identity | sed 's/#.*$//')"
    [ -n "$body" ] || continue
    if printf '%s\n' "$body" | grep -Eq 'rm[[:space:]]+-[a-z]*r'; then
        bad "(H3) ${f#"$HERE"/}: recursive rm inside the HomeKit wipe — the dirs belong to tmpfiles.d"
        hk_over=1
    fi
    if printf '%s\n' "$body" | grep -Eq '/opt/sa02m-homekit|system/sa02m-homekit\.service'; then
        bad "(H3) ${f#"$HERE"/}: the HomeKit wipe names the installed software (package, venv or unit file) — identity goes, software stays"
        hk_over=1
    fi
done
[ "$hk_over" -eq 0 ] && ok "(H3) no HomeKit wipe recurses or touches the installed software"

# ── H4 — the patch site: wipe + fatal belt, unconditional and bare ───────
hk_depths="$(awk '
  {
    l=$0; sub(/#.*$/,"",l)
    iscall = (l ~ /^[[:space:]]*(wipe_homekit_identity|assert_homekit_identity_clean)[[:space:]]+"\$mnt"/)
    gsub(/"[^"]*"/, "S", l)
    gsub(/\047[^\047]*\047/, "S", l)
    o = gsub(/(^|[;&|(=[:space:]])(if|for|while|until|case)[[:space:]]/, "X", l)
    c = gsub(/(^|[;&|[:space:]])(fi|done|esac)([;&|[:space:]]|$)/, "X", l)
    if (iscall) print d+0
    d += o - c
  }' "$PATCH_SRC")"
hk_calls="$(printf '%s\n' "$hk_depths" | grep -c '[0-9]' || true)"
hk_nested="$(printf '%s\n' "$hk_depths" | grep -cv '^0$' || true)"
if [ "$hk_calls" -ne 2 ]; then
    bad "(H4) expected exactly 2 top-level HomeKit calls (wipe + assert) in patch-firstboot-image.sh, found $hk_calls"
elif [ "$hk_nested" -ne 0 ]; then
    bad "(H4) the patch site's HomeKit wipe/assert sits inside a conditional (depth $hk_depths) — a security clear must not be skippable"
else
    ok "(H4) the patch site's HomeKit wipe AND assertion are unconditional (depth 0)"
fi
hk_swallowed="$(grep -E '^[[:space:]]*(wipe_homekit_identity|assert_homekit_identity_clean)[[:space:]]+"\$mnt"' "$PATCH_SRC" \
              | grep -E '\|\||&&|; *(true|:)' || true)"
[ -z "$hk_swallowed" ] && ok "(H4c) the HomeKit wipe/assert calls are bare" \
                        || bad "(H4c) the patch site's HomeKit call swallows its own failure: $hk_swallowed"

# ── H7 — every systemctl the on-device HomeKit wipes run is bounded ──────
for f in "$RESET_SRC" "$STREAM_SRC"; do
    body="$(extract_fn "$f" wipe_homekit_identity | sed 's/#.*$//')"
    n="$(printf '%s\n' "$body" | grep -c 'systemctl' || true)"
    if [ "${n:-0}" -lt 2 ]; then
        bad "(H7) ${f#"$HERE"/}: only ${n:-0} systemctl call(s) in the HomeKit wipe — the unit is not stopped+disabled, or the extraction is vacuous"
    else
        unbounded="$(printf '%s\n' "$body" | grep 'systemctl' | grep -v 'timeout[[:space:]]\+[0-9]\+[[:space:]]\+systemctl' || true)"
        [ -z "$unbounded" ] && ok "(H7) ${f#"$HERE"/}: all $n HomeKit systemctl calls are timeout-bounded" \
                            || bad "(H7) ${f#"$HERE"/}: unbounded systemctl in the HomeKit wipe: $unbounded"
    fi
done

# ── HB — behavioural: the shipped HomeKit wipes against a paired donor ───
HK_PKG="$HERE/opt/sa02m-homekit"

# ── H5 — the sidecar glob is anchored at the daemon's OWN tmp prefix ─────
# Every atomic write of the package names its temp file with
# sa02m_homekit.constants.TMP_PREFIX (fsutil.atomic_write). The prefix is read
# from that code home, never assumed: the gate's `.hk-*` rows (and so the
# contract's and every site's, by H0/H2) must be exactly that prefix + `*`, and
# HB seeds its torn-write sidecar under the READ prefix — so a renamed prefix
# leaves a sidecar the shipped globs no longer reach and HB goes RED.
HK_TMP_PREFIX=""
if [ -n "$PY" ]; then
    HK_TMP_PREFIX="$(PYTHONPATH="$HK_PKG" "$PY" -c 'from sa02m_homekit import constants as C; print(C.TMP_PREFIX)' 2>/dev/null)"
fi
case "$HK_TMP_PREFIX" in
  ""|*[!A-Za-z0-9._-]*)
    bad "(H5) TMP_PREFIX could not be read from sa02m_homekit.constants ('$HK_TMP_PREFIX') — the sidecar rows have no anchor"
    HK_TMP_PREFIX=".hk-" ;;
  *)
    if printf '%s\n' "$HK_ONDEVICE_SET" | grep -qxF "/var/lib/sa02m-homekit/${HK_TMP_PREFIX}*"; then
        ok "(H5) the sidecar glob is the daemon's own atomic-write prefix (${HK_TMP_PREFIX}*, sa02m_homekit.constants.TMP_PREFIX)"
    else
        bad "(H5) sa02m_homekit.constants.TMP_PREFIX is '$HK_TMP_PREFIX' but the clear-list glob is not '${HK_TMP_PREFIX}*' — a torn write of the pairing store survives every site"
    fi ;;
esac
hk_seed() { # <root> <enabled-line>
    local r=$1 en=${2:-enabled = true}
    mkdir -p "$r/var/lib/sa02m-homekit" "$r/run/sa02m-homekit" "$r/etc/sa02m-homekit" \
             "$r/etc/systemd/system/multi-user.target.wants" "$r/opt/sa02m-homekit/sa02m_homekit"
    printf '{"accessory_ltsk":"DONOR-LTSK","paired_clients":{"iphone":"DONOR"}}\n' > "$r/var/lib/sa02m-homekit/state.json"
    printf '{"dev-1":2}\n'                    > "$r/var/lib/sa02m-homekit/aids.json"
    printf '{"machine_id_sha256":"donor"}\n' > "$r/var/lib/sa02m-homekit/identity.json"
    # A torn atomic write of the pairing store: the same key under a dot-name.
    printf '{"accessory_ltsk":"DONOR-LTSK"}\n' > "$r/var/lib/sa02m-homekit/${HK_TMP_PREFIX}Zq81xk.tmp"
    printf '{"state":"running"}\n'           > "$r/run/sa02m-homekit/status.json"
    printf '{"code":"111-22-333"}\n'          > "$r/run/sa02m-homekit/setup.json"
    printf '{"accessories":[]}\n'             > "$r/run/sa02m-homekit/projection.json"
    printf '%s\n' '# SA-02m Apple HomeKit bridge' '[bridge]' "$en" 'interface = eth1' 'port = 21070' \
        > "$r/etc/sa02m-homekit/sa02m-homekit.conf"
    printf '[Unit]\n' > "$r/etc/systemd/system/sa02m-homekit.service"
    printf 'code\n'   > "$r/opt/sa02m-homekit/sa02m_homekit/main.py"
    # Relative, so it resolves inside the sandbox (the absolute form a board
    # carries is HB2's 'wantslink' case).
    ln -s ../sa02m-homekit.service \
        "$r/etc/systemd/system/multi-user.target.wants/sa02m-homekit.service" 2>/dev/null \
        || printf 'unit-wants-link\n' > "$r/etc/systemd/system/multi-user.target.wants/sa02m-homekit.service"
}

# The conf as the DAEMON reads it (sa02m_homekit.config.load) — the parser
# whose verdict decides whether a clone opens a listener. Prints
# "<enabled> <interface> <port>" or nothing.
hk_conf_view() { # <conf>
    [ -n "$PY" ] || return 0
    PYTHONPATH="$HK_PKG" "$PY" -c 'import sys
from sa02m_homekit import config
c = config.load(sys.argv[1])
print("%s %s %s" % (c.enabled, c.interface, c.port))' "$1" 2>/dev/null
}

make_hk_ondevice_probe() { # <src> <out>
    local body
    body="$(extract_fn "$1" wipe_homekit_identity)"
    [ -n "$body" ] || return 90
    body="$(printf '%s\n' "$body" \
        | sed -e 's#\(^\|[[:space:]"]\)/var/lib/sa02m-homekit#\1"$SBROOT"/var/lib/sa02m-homekit#g' \
              -e 's#\(^\|[[:space:]"]\)/etc/sa02m-homekit#\1"$SBROOT"/etc/sa02m-homekit#g' \
              -e 's#\(^\|[[:space:]"]\)/run/sa02m-homekit#\1"$SBROOT"/run/sa02m-homekit#g')"
    if printf '%s\n' "$body" | sed 's/#.*$//' | sed 's#"\$SBROOT"/#@SB@/#g' \
         | grep -Eq '(^|[[:space:]"])/(var/lib|etc|run)/sa02m-homekit'; then
        return 91
    fi
    {
        echo '#!/usr/bin/env bash'
        echo 'set -euo pipefail'
        echo 'SBROOT="$1"; REC="$2"'
        # Records every call, and whether the pairing store was still on disk
        # when the stop ran: the daemon holds the keys in memory and would
        # write them back, so the stop must come FIRST.
        echo 'systemctl(){ printf "%s\n" "systemctl $*" >> "$REC"; if [ "$1" = stop ] && [ -e "$SBROOT/var/lib/sa02m-homekit/state.json" ]; then echo "stop-before-wipe" >> "$REC"; fi; return 0; }'
        echo 'timeout(){ shift; "$@"; }'
        echo 'log(){ :; }'
        printf '%s\n' "$body"
        echo 'wipe_homekit_identity'
    } > "$2"
}

make_hk_offline_probe() { # <src> <out> <shell>
    local body
    body="$(extract_fn "$1" wipe_homekit_identity)"
    [ -n "$body" ] || return 90
    {
        echo "#!/usr/bin/env $3"
        echo 'set -eu'
        printf '%s\n' "$body"
        echo 'wipe_homekit_identity "$1"'
    } > "$2"
}

hk_run() { # <form> <probe> <root> <rec> <shell>
    if [ "$1" = ondevice ]; then bash "$2" "$3" "$4"; else "$5" "$2" "$3"; fi
}

hk_run_site() { # <label> <src> <form> [shell for offline: bash|sh]
    local lbl=$1 src=$2 form=$3 sh=${4:-bash} root="$SANDBOX/hk-$1" probe="$SANDBOX/hk-$1.probe.sh"
    local rec="$SANDBOX/hk-$1.systemctl.log" rc=0 view left
    rm -rf "$root"; mkdir -p "$root"; : > "$rec"
    hk_seed "$root"
    if [ "$form" = ondevice ]; then make_hk_ondevice_probe "$src" "$probe" || rc=$?
    else make_hk_offline_probe "$src" "$probe" "$sh" || rc=$?; fi
    case "$rc" in
      90) bad "(HB/$lbl) wipe_homekit_identity() could not be extracted — nothing behavioural was run"; return ;;
      91) bad "(HB/$lbl) the sandbox retarget failed — refusing to run (it would edit the host's real /etc)"; return ;;
    esac
    hk_run "$form" "$probe" "$root" "$rec" "$sh" || { bad "(HB/$lbl) the wipe exited non-zero on a paired donor"; return; }
    ok "(HB/$lbl) the shipped HomeKit wipe ran clean on a paired donor ($form${4:+, $sh})"

    if [ -d "$root/var/lib/sa02m-homekit" ] && [ -z "$(ls -A "$root/var/lib/sa02m-homekit")" ]; then
        ok "(HB/$lbl) the pairing store is empty (state, aids, identity and the .hk- sidecar gone); the dir stays"
    else
        bad "(HB/$lbl) the state dir is gone or not empty: $(ls -A "$root/var/lib/sa02m-homekit" 2>&1 | tr '\n' ' ')"
    fi
    view="$(hk_conf_view "$root/etc/sa02m-homekit/sa02m-homekit.conf")"
    if [ -z "$PY" ]; then
        bad "(HB/$lbl) no python — the conf cannot be read through the daemon's own parser"
    elif [ "$view" = "False eth1 21070" ]; then
        ok "(HB/$lbl) the daemon's parser reads the conf as disabled; interface/port kept (configuration)"
    else
        bad "(HB/$lbl) the daemon's parser reads the wiped conf as '$view' — want 'False eth1 21070'"
    fi
    if [ -f "$root/etc/systemd/system/sa02m-homekit.service" ] && [ -f "$root/opt/sa02m-homekit/sa02m_homekit/main.py" ]; then
        ok "(HB/$lbl) the unit file and the package survive (software, not identity)"
    else
        bad "(HB/$lbl) the wipe removed installed software (unit file or package)"
    fi
    case "$form" in
      ondevice)
        if [ -d "$root/run/sa02m-homekit" ] && [ -z "$(ls -A "$root/run/sa02m-homekit")" ]; then
            ok "(HB/$lbl) /run/sa02m-homekit emptied (the live setup code and status gone), dir kept"
        else
            bad "(HB/$lbl) /run/sa02m-homekit still holds: $(ls -A "$root/run/sa02m-homekit" 2>&1 | tr '\n' ' ')"
        fi
        if grep -q 'systemctl stop sa02m-homekit' "$rec" && grep -q 'systemctl disable sa02m-homekit' "$rec"; then
            ok "(HB/$lbl) the bridge unit was stopped AND disabled (recorded by the shim)"
        else
            bad "(HB/$lbl) stop/disable not recorded — the donor's daemon keeps its keys in memory, and a clone boots enabled"
        fi
        if grep -qx 'stop-before-wipe' "$rec"; then
            ok "(HB/$lbl) the daemon was stopped BEFORE its pairing store was removed"
        else
            bad "(HB/$lbl) the stop ran after the wipe (or never) — a running daemon writes its in-memory keys back"
        fi ;;
      offline)
        if [ -e "$root/run/sa02m-homekit/status.json" ]; then
            ok "(HB/$lbl) the offline wipe does not touch /run (never in the image)"
        else
            bad "(HB/$lbl) the offline wipe cleared /run — a dead line: tmpfs is not in the image"
        fi
        left="$root/etc/systemd/system/multi-user.target.wants/sa02m-homekit.service"
        if [ -e "$left" ] || [ -L "$left" ]; then
            bad "(HB/$lbl) the bridge unit is still enabled in the image"
        else
            ok "(HB/$lbl) the bridge unit is disabled in the image (wants link removed)"
        fi ;;
    esac

    local before after
    before="$(snapshot "$root")"
    hk_run "$form" "$probe" "$root" "$rec" "$sh" >/dev/null 2>&1; rc=$?
    after="$(snapshot "$root")"
    [ "$rc" -eq 0 ] && [ "$before" = "$after" ] \
        && ok "(HB/$lbl) idempotent — a second run is a byte-identical no-op, exit 0" \
        || bad "(HB/$lbl) not idempotent (rc=$rc)"

    # THE common case: the module is optional and not in the factory image.
    local bare="$SANDBOX/hk-$1.bare" created
    rm -rf "$bare"; mkdir -p "$bare/etc" "$bare/var/lib"
    hk_run "$form" "$probe" "$bare" "$rec" "$sh" >/dev/null 2>&1; rc=$?
    created="$(find "$bare" -name '*homekit*' 2>/dev/null | head -n 5)"
    [ "$rc" -eq 0 ] && [ -z "$created" ] \
        && ok "(HB/$lbl) a rootfs without the module: exit 0, creates nothing" \
        || bad "(HB/$lbl) rootfs without the module: rc=$rc created='$created' — an abort here kills the capture"

    # The key as configparser also accepts it: any case, `:` delimiter.
    local var="$SANDBOX/hk-$1.var"
    rm -rf "$var"; mkdir -p "$var"; hk_seed "$var" 'ENABLED : on'
    hk_run "$form" "$probe" "$var" "$rec" "$sh" >/dev/null 2>&1
    view="$(hk_conf_view "$var/etc/sa02m-homekit/sa02m-homekit.conf")"
    [ "$view" = "False eth1 21070" ] \
        && ok "(HB/$lbl) 'ENABLED : on' (valid for configparser) is forced off too" \
        || bad "(HB/$lbl) 'ENABLED : on' survived as '$view' — the daemon would still open the listener"

    # A symlink planted at the conf (www-data can, the dir is 0770): never
    # read or written through — the victim stays byte-identical and no copy
    # of it appears at the conf name.
    local lk="$SANDBOX/hk-$1.link" victim="$SANDBOX/hk-$1.victim"
    rm -rf "$lk"; mkdir -p "$lk"; hk_seed "$lk"
    printf 'root:$6$hk-planted-victim:19000:0:99999:7:::\nenabled = true\n' > "$victim"
    rm -f "$lk/etc/sa02m-homekit/sa02m-homekit.conf"
    ln -s "$victim" "$lk/etc/sa02m-homekit/sa02m-homekit.conf"
    local vb; vb="$(cksum < "$victim")"
    hk_run "$form" "$probe" "$lk" "$rec" "$sh" >/dev/null 2>&1; rc=$?
    if [ "$rc" -eq 0 ] && [ "$(cksum < "$victim")" = "$vb" ] \
       && [ ! -e "$lk/etc/sa02m-homekit/sa02m-homekit.conf" ] && [ ! -L "$lk/etc/sa02m-homekit/sa02m-homekit.conf" ]; then
        ok "(HB/$lbl) a symlinked conf is dropped, never followed: victim identical, no copy at the conf name"
    else
        bad "(HB/$lbl) symlinked conf: rc=$rc, victim identical=$([ "$(cksum < "$victim")" = "$vb" ] && echo yes || echo NO), conf name now: $(ls -l "$lk/etc/sa02m-homekit/" 2>&1 | tail -n +2 | tr '\n' ' ')"
    fi

    # A symlink AT the state dir: the contents glob would descend it, and on a
    # mounted image an absolute link resolves on the HOST running the capture
    # (`rm -f <link>/*` empties a host directory). Dropped, never descended —
    # the victim dir keeps every file, dot-files included; tmpfiles.d
    # re-creates the real dir at boot, so the wipe creates nothing there.
    local dl="$SANDBOX/hk-$1.dirlink" vdir="$SANDBOX/hk-$1.victimdir"
    rm -rf "$dl" "$vdir"; mkdir -p "$dl" "$vdir"; hk_seed "$dl"
    printf 'host-file\n' > "$vdir/precious"
    printf 'host-dot\n'  > "$vdir/.hk-host.tmp"
    rm -rf "$dl/var/lib/sa02m-homekit"
    ln -s "$vdir" "$dl/var/lib/sa02m-homekit"
    hk_run "$form" "$probe" "$dl" "$rec" "$sh" >/dev/null 2>&1; rc=$?
    if [ "$rc" -eq 0 ] && [ -f "$vdir/precious" ] && [ -f "$vdir/.hk-host.tmp" ] \
       && [ ! -e "$dl/var/lib/sa02m-homekit" ] && [ ! -L "$dl/var/lib/sa02m-homekit" ]; then
        ok "(HB/$lbl) a symlinked state dir is dropped, never descended: the victim dir keeps its files"
    else
        bad "(HB/$lbl) symlinked state dir: rc=$rc, victim now: $(ls -A "$vdir" 2>&1 | tr '\n' ' '), link still there: $([ -L "$dl/var/lib/sa02m-homekit" ] && echo yes || echo no)"
    fi
}

hk_run_site reset      "$RESET_SRC"       ondevice
hk_run_site stream     "$STREAM_SRC"      ondevice
hk_run_site patch      "$PATCH_SRC"       offline bash
hk_run_site autorun    "$AUTORUN_SRC"     offline bash
hk_run_site autorunfel "$AUTORUN_FEL_SRC" offline bash
hk_run_site sshflash   "$SSH_FLASH_SRC"   offline bash
# The receivers run under BusyBox sh on the USB stick, not bash: run them
# under a POSIX sh too (dash on Linux), where one is present.
if command -v dash >/dev/null 2>&1; then
    hk_run_site autorun-sh    "$AUTORUN_SRC"     offline dash
    hk_run_site autorunfel-sh "$AUTORUN_FEL_SRC" offline dash
else
    echo "  SKIP  (HB) no dash on this host — the receivers' HomeKit wipe was run under bash only (a skip is not a pass)"
fi

# ── HB2 — the shipped HomeKit belt (patch site), run for real ────────────
hk_abody="$(extract_fn "$PATCH_SRC" assert_homekit_identity_clean)"
if [ -z "$hk_abody" ]; then
    bad "(HB2) assert_homekit_identity_clean() could not be extracted — the HomeKit belt has no buckle"
else
    hk_aprobe="$SANDBOX/hk-assert.probe.sh"
    {
        echo '#!/usr/bin/env bash'
        echo 'set -euo pipefail'
        echo 'die(){ echo "FATAL: $*" >&2; exit 1; }'
        printf '%s\n' "$hk_abody"
        echo 'assert_homekit_identity_clean "$1"'
    } > "$hk_aprobe"
    if printf '%s\n' "$hk_abody" | sed 's/#.*$//' | grep -q '|| true'; then
        bad "(HB2) the HomeKit belt carries a || true — the fail-open that neutered the boot.scr guard"
    fi
    if bash "$hk_aprobe" "$SANDBOX/hk-patch" >/dev/null 2>&1; then
        ok "(HB2) the HomeKit belt PASSES a correctly wiped image"
    else
        bad "(HB2) the HomeKit belt fails a correctly wiped image — it would abort every capture"
    fi
    hk_bare="$SANDBOX/hk-bare-image"; rm -rf "$hk_bare"; mkdir -p "$hk_bare/etc"
    bash "$hk_aprobe" "$hk_bare" >/dev/null 2>&1 \
        && ok "(HB2) the HomeKit belt PASSES an image without the module (the factory image)" \
        || bad "(HB2) the HomeKit belt fails an image without the module — every factory capture would abort"
    for one in state sidecar dotfile enabled wantslink wantsfile conflink dirlink; do
        d="$SANDBOX/hk-dirty-$one"; rm -rf "$d"; mkdir -p "$d"; hk_seed "$d"
        # clean first, then re-dirty exactly one dimension
        rm -f "$d/var/lib/sa02m-homekit"/* "$d/var/lib/sa02m-homekit"/.hk-* \
              "$d/etc/systemd/system/multi-user.target.wants/sa02m-homekit.service"
        sed -i 's/^enabled = true$/enabled = false/' "$d/etc/sa02m-homekit/sa02m-homekit.conf"
        case "$one" in
          state)     printf 'DONOR-LTSK\n' > "$d/var/lib/sa02m-homekit/state.json" ;;
          sidecar)   printf 'DONOR-LTSK\n' > "$d/var/lib/sa02m-homekit/${HK_TMP_PREFIX}Zq81xk.tmp" ;;
          dotfile)   printf 'x\n' > "$d/var/lib/sa02m-homekit/.unknown-shape" ;;
          enabled)   sed -i 's/^enabled = false$/Enabled: Yes/' "$d/etc/sa02m-homekit/sa02m-homekit.conf" ;;
          # The real wants entry is an ABSOLUTE link: `-e` alone resolves it
          # on the host running the patch and misses it.
          wantslink) ln -s /nonexistent-on-this-host/sa02m-homekit.service \
                         "$d/etc/systemd/system/multi-user.target.wants/sa02m-homekit.service" ;;
          wantsfile) printf 'unit-wants-link\n' > "$d/etc/systemd/system/multi-user.target.wants/sa02m-homekit.service" ;;
          conflink)  rm -f "$d/etc/sa02m-homekit/sa02m-homekit.conf"
                     ln -s /nonexistent-on-this-host/conf "$d/etc/sa02m-homekit/sa02m-homekit.conf" ;;
          # A dangling ABSOLUTE link at the store: its globs expand to
          # nothing, so an entry loop alone passes it.
          dirlink)   rmdir "$d/var/lib/sa02m-homekit"
                     ln -s /nonexistent-on-this-host/hk "$d/var/lib/sa02m-homekit" ;;
        esac
        if bash "$hk_aprobe" "$d" >/dev/null 2>&1; then
            bad "(HB2) the HomeKit belt missed a dirty '$one' — that dimension is unguarded"
        else
            ok "(HB2) the HomeKit belt catches a dirty '$one'"
        fi
    done
fi

echo
[ "$fails" -eq 0 ] && { echo "alice-image-identity: ALL OK"; exit 0; }
echo "alice-image-identity: $fails FAILURE(S)"; exit 1
