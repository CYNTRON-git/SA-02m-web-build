#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-dtb-fdtput.sh — the boot-DTB edit on the REAL tools (quality row
# dtb-fdtput-real): usr/local/sbin/sa02m-dtb-patch.sh's dtb_edit, driven by the
# real fdtget/fdtput on the repo's own blob, compared with what the retired
# text patcher produced on the bench.
#
# Why: dtb_edit replaced a dtc decompile -> python text patcher -> dtc compile
# round trip (1.0.7.26). The change is behaviour-preserving, so the net is
# EQUIVALENCE plus byte idempotency, on real tools — the sandbox harness
# (test-dtb-patch-tool.sh) runs dtb_edit only against fdtget/fdtput shims and
# cannot tell whether the real tools behave like them.
#
# Needs fdtget, fdtput and dtc (apt install device-tree-compiler). Without them
# the whole run SKIPs (exit 77): run.mjs reports SKIP, and under QUALITY_STRICT
# (CI, which installs the package) a SKIP is a FAIL — so CI must really run it.
# On a Windows dev box it always skips; the bench (dtc 1.7.0) runs it too.
#
# Inputs — EXACTLY the recipes of the bench capture (scratch golden.sh,
# 2026-10-31), applied with the real fdtput to copies of
# tools/imaging/boot/sun8i-a40i-sk.dtb (md5 2c46fe62…, the bench 1.135 original
# blob; checked below):
#   asis      the blob as it is (only the EMAC PHY list pending)
#   damaged   GMAC PHY compatible = icplus,ip101a x4 + c22, EMAC PHY = the id
#             only (the bench apply-#3 lists)
#   reverted  all eight edits undone: regulator-always-on deleted from dc1sw;
#             status disabled on i2c@1c2b800, the GMAC and its mdio child (a
#             NEW property there); GMAC syscon 1; GMAC PHY compatible c22 only;
#             both reset-deassert-us 100000
# Goldens — scripts/dev/fixtures/dtb-patch/golden-<input>.sorted.dts: the
# retired text patcher (1.0.7.23 sa02m-dtb-patch.py; the deployed copy's md5
# 3150a3ca == the repo's) run on the bench on the same three inputs, recompiled
# and decompiled SORTED (`dtc -q -s -I dtb -O dts`, DTC 1.7.0). Byte-exact dtc
# output, so they carry no header; this paragraph and the plan record are
# their provenance. golden-asis == golden-damaged; golden-reverted differs by
# the GMAC mdio `status = "okay"` the reverted recipe created.
#
# Cases (each input):
#   E1 dtb_edit exits 0 and prints exactly the change lines the text patcher
#      printed on the bench (its *.changes.txt, embedded below).
#   E2 the edited blob, decompiled sorted, is IDENTICAL to the golden — the
#      same tree as the text patcher's (the byte layout may differ: fdtput
#      appends a new property at the end of its node).
#   E3 the tool's own verify_blob accepts the result.
#   E4 dtb_edit again on the result: no change line, exit 0, and the blob is
#      byte-identical — the «уже исправлен» guarantee of apply.
#   E5 dc1sw carries regulator-always-on as an EMPTY property (fdtget -p lists
#      it, its value is empty).
#   F2 a GMAC compatible string with a literal backslash followed by `0`
#      (`acme,phy\0x`): the real fdtput path keeps it ONE string —
#      `icplus,ip101a acme,phy\0x c22`. The text patcher split it on the dts
#      `\0` escape and emitted an invalid literal (observed on its 1.0.7.23
#      copy, 2026-10-31; dtc then refuses the text, apply exits 5). The seed
#      itself is read back first, so an fdtput that interpreted the escape
#      FAILS the case rather than passing it.
# Non-vacuous: a missing tool file, blob or golden, a blob whose md5 is not the
# bench original, a recipe step that fails, dtb_edit not defined after
# sourcing, or an empty golden FAILS.
#
# DTB_TOOL_SRC=<path> re-points the suite at another tool copy (a mutation
# run). The tool is SOURCED (it defines dtb_edit and its constants; its
# dispatch runs only when executed) — it also sets `-uo pipefail` in this
# shell, so nothing below pipes a producer into an early-exit reader.
#
# Comment-out cases (comment-mutation-proof): the EMAC `patch_phy` call of
# dtb_edit -> E1/E2/E3 RED on every input; the empty `regulator-always-on`
# fdtput -> E2/E5 RED on `reverted`. Where this suite SKIPs, the proof reports
# those cases SKIP, never ok — CI measures them.
#
# Run: bash scripts/dev/test-dtb-fdtput.sh
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

TOOL=${DTB_TOOL_SRC:-usr/local/sbin/sa02m-dtb-patch.sh}
BLOB=tools/imaging/boot/sun8i-a40i-sk.dtb
BLOB_MD5=2c46fe62052c7eea22d0319ec175614d
GOLD=scripts/dev/fixtures/dtb-patch

missing=""
for t in fdtget fdtput dtc; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
if [ -n "$missing" ]; then
    reason="test-dtb-fdtput.sh: not installed:$missing (apt install device-tree-compiler) — the real-tool equivalence runs in CI and on the bench"
    echo "SKIP  $reason"
    exit 77   # run.mjs SKIP_EXIT
fi

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$TOOL" "$BLOB" "$GOLD/golden-asis.sorted.dts" "$GOLD/golden-damaged.sorted.dts" "$GOLD/golden-reverted.sorted.dts"; do
    [ -s "$f" ] || { echo "FAIL  missing or empty: $f"; exit 1; }
done
md5=$(md5sum "$BLOB") || { echo "FAIL  md5sum $BLOB"; exit 1; }
[ "${md5%% *}" = "$BLOB_MD5" ] || { echo "FAIL  $BLOB is not the bench original (md5 ${md5%% *}, expected $BLOB_MD5) — the goldens describe that blob only"; exit 1; }
echo "dtc: $(dtc --version 2>&1)"

# shellcheck source=usr/local/sbin/sa02m-dtb-patch.sh
. "$TOOL" || { echo "FAIL  sourcing $TOOL failed"; exit 1; }
declare -F dtb_edit >/dev/null || { echo "FAIL  $TOOL defines no dtb_edit"; exit 1; }
declare -F verify_blob >/dev/null || { echo "FAIL  $TOOL defines no verify_blob"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

P_DC=/soc/i2c@1c2ac00/pmic@34/regulators/dc1sw
P_I2C=/soc/i2c@1c2b800
P_G=/soc/ethernet@1c50000
P_GMDIO=/soc/ethernet@1c50000/mdio
P_GPHY=/soc/ethernet@1c50000/mdio/ethernet-phy@0
P_EPHY=/soc/mdio@1c0b080/ethernet-phy@0
C22=ethernet-phy-ieee802.3-c22

recipe() {   # fdtput "$@" on an input, or stop: a recipe step that fails makes the inputs wrong
    fdtput "$@" || { echo "FAIL  recipe step failed: fdtput $*"; exit 1; }
}

# ── the three inputs (golden.sh, verbatim recipes) ─────────────────────────
cp "$BLOB" "$T/input-asis.dtb"
cp "$BLOB" "$T/input-damaged.dtb"
recipe -t s "$T/input-damaged.dtb" "$P_GPHY" compatible icplus,ip101a icplus,ip101a icplus,ip101a icplus,ip101a "$C22"
recipe -t s "$T/input-damaged.dtb" "$P_EPHY" compatible ethernet-phy-id0243.0c54
cp "$BLOB" "$T/input-reverted.dtb"
recipe -d "$T/input-reverted.dtb" "$P_DC" regulator-always-on
recipe -t s "$T/input-reverted.dtb" "$P_I2C" status disabled
recipe -t s "$T/input-reverted.dtb" "$P_G" status disabled
recipe -t s "$T/input-reverted.dtb" "$P_GMDIO" status disabled
recipe -t u "$T/input-reverted.dtb" "$P_G" syscon 1
recipe -t s "$T/input-reverted.dtb" "$P_GPHY" compatible "$C22"
recipe -t u "$T/input-reverted.dtb" "$P_GPHY" reset-deassert-us 100000
recipe -t u "$T/input-reverted.dtb" "$P_EPHY" reset-deassert-us 100000

# The text patcher's change lines on the bench (golden/*.changes.txt).
expect_asis='mdio@1c0b080/ethernet-phy@0: compatible ethernet-phy-id0243.0c54 first'
expect_damaged='ethernet@1c50000/mdio/ethernet-phy@0: compatible icplus,ip101a first
mdio@1c0b080/ethernet-phy@0: compatible ethernet-phy-ieee802.3-c22 appended'
expect_reverted='dc1sw: regulator-always-on
i2c@1c2b800: status okay
ethernet@1c50000: status okay
ethernet@1c50000: syscon 0x02
ethernet@1c50000/mdio/ethernet-phy@0: compatible icplus,ip101a first
ethernet@1c50000/mdio/ethernet-phy@0: reset-deassert-us 500000
mdio@1c0b080/ethernet-phy@0: compatible ethernet-phy-id0243.0c54 first
mdio@1c0b080/ethernet-phy@0: reset-deassert-us 500000'

for n in asis damaged reverted; do
    in="$T/input-$n.dtb"; p1="$T/$n.pass1.dtb"; p2="$T/$n.pass2.dtb"
    case "$n" in
        asis) want=$expect_asis ;;
        damaged) want=$expect_damaged ;;
        reverted) want=$expect_reverted ;;
    esac
    cp "$in" "$p1"
    dtb_edit "$p1" > "$T/$n.out1" 2> "$T/$n.err1"; rc=$?
    got=$(cat "$T/$n.out1")
    if [ "$rc" = 0 ] && [ "$got" = "$want" ] && ! cmp -s "$in" "$p1"; then
        ok "E1 $n: dtb_edit exit 0, the text patcher's change lines exactly"
    else
        bad "E1 $n: rc=$rc lines=[$(tr '\n' '|' < "$T/$n.out1")] err=[$(tr '\n' '|' < "$T/$n.err1")]"
    fi
    if dtc -q -s -I dtb -O dts -o "$T/$n.sorted.dts" "$p1" 2> "$T/$n.dtc.err" \
       && diff "$GOLD/golden-$n.sorted.dts" "$T/$n.sorted.dts" > "$T/$n.diff"; then
        ok "E2 $n: the edited tree == the text patcher's (sorted dts identical to golden-$n)"
    else
        bad "E2 $n: sorted tree differs from golden-$n — $(head -c 400 "$T/$n.diff" "$T/$n.dtc.err" | tr '\n' '|')"
    fi
    if verify_blob "$p1" "$in" "$n" > "$T/$n.verify" 2>&1; then
        ok "E3 $n: the tool's verify_blob accepts the result"
    else
        bad "E3 $n: verify_blob refused — $(tr '\n' '|' < "$T/$n.verify")"
    fi
    cp "$p1" "$p2"
    dtb_edit "$p2" > "$T/$n.out2" 2> "$T/$n.err2"; rc=$?
    if [ "$rc" = 0 ] && [ ! -s "$T/$n.out2" ] && cmp -s "$p1" "$p2"; then
        ok "E4 $n: a second dtb_edit changes nothing — byte-identical, no change line"
    else
        bad "E4 $n: pass 2 rc=$rc lines=[$(tr '\n' '|' < "$T/$n.out2")] identical=$(cmp -s "$p1" "$p2" && echo y || echo n)"
    fi
    props=$(fdtget -p "$p1" "$P_DC" 2>&1); v=$(fdtget "$p1" "$P_DC" regulator-always-on 2>&1); vrc=$?
    if grep -qxF regulator-always-on <<<"$props" && [ "$vrc" = 0 ] && [ -z "$v" ]; then
        ok "E5 $n: dc1sw regulator-always-on present and EMPTY"
    else
        bad "E5 $n: dc1sw props=[$(tr '\n' ' ' <<<"$props")] value rc=$vrc '$v'"
    fi
done

# ── F2: a literal backslash + 0 inside one compatible string ───────────────
f2="$T/f2.dtb"
cp "$BLOB" "$f2"
recipe -t s "$f2" "$P_GPHY" compatible 'acme,phy\0x' "$C22"
seed=$(fdtget -ts "$f2" "$P_GPHY" compatible 2>&1)
if [ "$seed" != 'acme,phy\0x ethernet-phy-ieee802.3-c22' ]; then
    bad "F2: the seed does not read back as two strings with a literal backslash ('$seed') — the case would be hollow"
else
    dtb_edit "$f2" > "$T/f2.out" 2> "$T/f2.err"; rc=$?
    g=$(fdtget -ts "$f2" "$P_GPHY" compatible 2>&1)
    if [ "$rc" = 0 ] && [ "$g" = 'icplus,ip101a acme,phy\0x ethernet-phy-ieee802.3-c22' ] \
       && grep -qxF 'ethernet@1c50000/mdio/ethernet-phy@0: compatible icplus,ip101a first' "$T/f2.out" \
       && verify_blob "$f2" "$BLOB" f2 > "$T/f2.verify" 2>&1; then
        ok "F2: a string with a literal backslash + 0 stays ONE string on the real tools, verify accepts"
    else
        bad "F2: rc=$rc gmac='$g' — $(tr '\n' '|' < "$T/f2.out") $(tr '\n' '|' < "$T/f2.err") $(tr '\n' '|' < "$T/f2.verify" 2>/dev/null)"
    fi
fi

printf '\n'
if [ "$fails" = 0 ]; then
    echo "PASS  test-dtb-fdtput.sh"
    exit 0
fi
echo "FAIL  test-dtb-fdtput.sh: $fails failing case(s)"
exit 1
