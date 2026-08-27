#!/usr/bin/env bash
# Gate for the image-identity reset of the Alice controller enrollment (1.0.6.20).
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
#   A1 all four sites carry the wipe; fewer than four = FAIL (non-vacuity)
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
#   A8 cloud parity — the four cloud sites still carry their own wipe
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
# No root, no device, no image, no loop mount.
set -u

HERE="$(cd "$(dirname "$0")/../.." && pwd)"

RESET_SRC="${RESET_SRC:-$HERE/tools/imaging/reset-alice-enrollment.sh}"
STREAM_SRC="${STREAM_SRC:-$HERE/tools/imaging/stream-after-cleanup.sh}"
PATCH_SRC="${PATCH_SRC:-$HERE/tools/imaging/patch-firstboot-image.sh}"
AUTORUN_SRC="${AUTORUN_SRC:-$HERE/tools/imaging/autorun.sh}"
AUTORUN_FEL_SRC="${AUTORUN_FEL_SRC:-$HERE/tools/imaging/autorun-fel.sh}"
CLEANUP="${CLEANUP_SRC:-$HERE/tools/imaging/cleanup-donor.sh}"
PRESERVE="${PRESERVE_SRC:-$HERE/etc/sa02m-factory-defaults/lists/preserve.list}"
FACTORY="${FACTORY_SRC:-$HERE/etc/sa02m-factory-reset-runner.sh}"
CONTRACT="$HERE/docs/contracts/image-identity-reset.md"
CLOUD_RESET="$HERE/tools/imaging/reset-cloud-enrollment.sh"

fails=0
ok(){ printf '  ok    %s\n' "$1"; }
bad(){ printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }

for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" \
         "$AUTORUN_FEL_SRC" "$CLEANUP" "$PRESERVE" "$FACTORY" "$CONTRACT" \
         "$CLOUD_RESET"; do
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

# ── A1 — all four sites carry the wipe ────────────────────────────────────
site_count=0
for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" "$AUTORUN_FEL_SRC"; do
    if [ -n "$(extract_fn "$f" wipe_alice_enrollment)" ]; then
        site_count=$((site_count + 1))
    else
        bad "(A1) no wipe_alice_enrollment() in ${f#"$HERE"/} — that site ships the donor's identity"
    fi
done
if [ "$site_count" -eq 5 ]; then
    ok "(A1) all five files of the four sites carry wipe_alice_enrollment()"
else
    bad "(A1) only $site_count/5 files carry the wipe — the enumeration is incomplete (contract §4)"
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

# ── A3 — the over-wipe tripwire ───────────────────────────────────────────
overwipe=0
for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" "$AUTORUN_FEL_SRC"; do
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
for f in "$RESET_SRC" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" "$AUTORUN_FEL_SRC"; do
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
if [ "$glob_seen" -lt 5 ]; then
    bad "(A3c) only $glob_seen glob(s) seen across the five sites — the sidecar rows are missing (F1: device.key.pem.tmp would clone)"
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
for f in "$CLOUD_RESET" "$STREAM_SRC" "$PATCH_SRC" "$AUTORUN_SRC" "$AUTORUN_FEL_SRC"; do
    grep -q 'device_secret' "$f" || { cloud_ok=0; bad "(A8) ${f#"$HERE"/} lost its cloud enrollment wipe (the twin defect, fixed 2026-07-31)"; }
done
[ "$cloud_ok" -eq 1 ] && ok "(A8) all five cloud-wipe sites intact"

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
    cat > "$r/etc/sa02m-alice/sa02m-alice-client.conf" <<'EOF'
[client]
client_enabled = true
log_level = INFO
mqtt_host = 127.0.0.1
mqtt_port = 1883
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

run_site reset  "$RESET_SRC"       ondevice
run_site stream "$STREAM_SRC"      ondevice
run_site patch  "$PATCH_SRC"       offline
run_site autorun "$AUTORUN_SRC"    offline
run_site autorunfel "$AUTORUN_FEL_SRC" offline

# ── B2 — the shipped ASSERTION, run for real ──────────────────────────────
# The belt itself: it must PASS a wiped rootfs and DIE on one that still holds
# the donor's key (a capture that ran with --no-id-reset, or a pre-fix stream).
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

echo
[ "$fails" -eq 0 ] && { echo "alice-image-identity: ALL OK"; exit 0; }
echo "alice-image-identity: $fails FAILURE(S)"; exit 1
