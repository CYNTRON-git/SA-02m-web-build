#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-image-manifest.sh — regression test for the donor-metadata handoff in
# tools/imaging/make-image.sh step 6 (collect_donor_metadata → write_manifest).
#
# Why this exists: the handoff is a text protocol between a remote Bash heredoc
# and a local Python heredoc, and NO other runner in this repo reaches it — the
# static lint row used to glob only www/, etc/, scripts/ and install.sh, and
# there is no build step. That gap let a real defect ship into a release: the
# manifest emitted one newline-separated stream that Python split POSITIONALLY,
# so when /proc/device-tree/model (NUL-terminated, no trailing newline) merged
# with the following `uname -r`, every later field shifted by one index —
# platform.os held the deployed-commit marker, git_commit held the serial
# profile, partitions.root.uuid held the armbian-bsp version. Every syntax and
# static gate stayed green. This test pins the protocol.
#
# Method: extract the REAL remote body and the REAL write_manifest python out of
# the shipped make-image.sh and run them against a sandboxed fake donor, with
# absolute donor paths repointed at the sandbox and PATH shims for the commands
# a dev box lacks (blkid, dpkg-query). Nothing touches the real system, no ssh,
# no device. The emit()/pipefail logic and the whole Python body run verbatim.
#
# Run: bash scripts/dev/test-image-manifest.sh   (bash + python, no deps)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# SRC is overridable so the test can be pointed at an older make-image.sh to
# confirm it actually CATCHES the shift it pins (a test that cannot fail is
# worse than none):
#   git show <rev>:tools/imaging/make-image.sh > /tmp/old.sh
#   MAKE_IMAGE_SRC=/tmp/old.sh bash scripts/dev/test-image-manifest.sh   # must FAIL
SRC=${MAKE_IMAGE_SRC:-tools/imaging/make-image.sh}
[ -r "$SRC" ] || { echo "FAIL  $SRC not readable"; exit 1; }

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys" >/dev/null 2>&1; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "test-image-manifest: no working python interpreter"; exit 1; }

T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── extract the shipped code (non-vacuous: empty extraction is a failure) ──
# Both extractions are ANCHORED to their owning function and stop at the first
# closing tag: make-image.sh contains OTHER heredocs with the same tags (a
# second <<'REMOTE' body masks services and disables the donor's hardware
# watchdog), and an unanchored scan would capture and RUN them here.
awk "/^collect_donor_metadata\(\)/{g=1} g&&/<<'REMOTE'/{f=1;next} /^REMOTE\$/{if(f)exit} f" "$SRC" > "$T/remote.src"
awk "/^write_manifest\(\)/{g=1}         g&&/<<'PY'/{f=1;next}     /^PY\$/{if(f)exit}     f" "$SRC" > "$T/wm.py"
[ -s "$T/remote.src" ] || bad "extraction: REMOTE body empty — make-image.sh restructured?"
[ -s "$T/wm.py" ]      || bad "extraction: PY body empty — make-image.sh restructured?"
grep -q 'DONOR_META' "$T/wm.py" || bad "extraction: PY body does not read DONOR_META"
# Over-capture guard (fail closed the OTHER way too): the collector body must
# never contain a state-mutating command — catching a future unanchored scan.
grep -qE 'systemctl|busctl|dd |mkfs|reboot' "$T/remote.src" \
    && bad "extraction: REMOTE body captured foreign heredoc content (state-mutating commands present)"
[ "$fails" -eq 0 ] || { printf '\nFAIL  cannot reach the code under test\n'; exit 1; }

# ── sandboxed fake donor ──────────────────────────────────────────────────
# build_donor <dir> — the healthy donor; model file is NUL-terminated with NO
# trailing newline, exactly as the kernel exposes it (the regression trigger).
build_donor() {
    local d=$1
    mkdir -p "$d/proc/device-tree" "$d/etc" "$d/var/lib/sa02m-web-build" "$d/bin"
    printf 'Cyntron A40i-2Eth\0'                        > "$d/proc/device-tree/model"
    printf 'PRETTY_NAME="Armbian 26.5.1 noble"\nID=debian\n' > "$d/etc/os-release"
    printf 'app-1.0.5.54\n'                             > "$d/var/lib/sa02m-web-build/deployed_commit"
    printf 'SA02M_SERIAL_PROFILE=sa02m-1eth\n'          > "$d/etc/sa02m_serial_profile.conf"
    printf '#!/bin/bash\necho SA-02\n'                  > "$d/bin/hostname"
    printf '#!/bin/bash\necho 6.1.0-rc6\n'              > "$d/bin/uname"
    cat > "$d/bin/blkid" <<'EOS'
#!/bin/bash
case "$*" in
  *mmcblk2p1*) echo D8CE-50BA ;;
  *mmcblk2p2*) echo 3599389b-2279-4cc9-8fd1-43135f13a73b ;;
  *) exit 2 ;;
esac
EOS
    printf '#!/bin/bash\necho 26.5.1\n'                 > "$d/bin/dpkg-query"
    chmod +x "$d/bin/"*
}

# run_remote <dir> — the shipped remote body against that donor, stdout captured
run_remote() {
    local d=$1
    sed "s#/proc/device-tree/model#$d/proc/device-tree/model#g;
         s#/etc/os-release#$d/etc/os-release#g;
         s#/var/lib/sa02m-web-build/deployed_commit#$d/var/lib/sa02m-web-build/deployed_commit#g;
         s#/etc/sa02m_serial_profile.conf#$d/etc/sa02m_serial_profile.conf#g" \
        "$T/remote.src" > "$d/remote.sh"
    PATH="$d/bin:$PATH" bash "$d/remote.sh" 2>/dev/null
}

# run_manifest <donor-meta> <out.json> [release-profile] — the shipped python.
# The 3rd arg defaults to the --profile override; pass "" to exercise the
# donor-supplied serial_profile path (the override otherwise masks it).
run_manifest() {
    printf 'deadbeef  x.img.xz\n' > "$T/x.sha256"
    DONOR_META="$1" DEVICE_IP=192.168.1.136 \
    RELEASE_PROFILE="${3-sa02m-1eth}" RELEASE_VERSION=1.0.5.54 \
    PIPE_CLEANUP=1 PIPE_ZEROFILL=1 PIPE_ID_RESET=1 PIPE_XZ_LEVEL=9e \
        "$PY" "$T/wm.py" "$T/x.img.xz" "$T/x.sha256" "$2" >/dev/null 2>&1
}

# field <out.json> <dotted.path>
field() {
    "$PY" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
for k in sys.argv[2].split('.'): d=d[k]
print(d)
" "$1" "$2" 2>/dev/null
}

# expect <label> <out.json> <dotted.path> <want>
expect() {
    local got; got=$(field "$2" "$3")
    if [ "$got" = "$4" ]; then ok "$1"; else bad "$1 — got '$got', want '$4'"; fi
}

# ── case 1: healthy donor — every field lands in its own slot ─────────────
build_donor "$T/d1"
META1=$(run_remote "$T/d1")

# The protocol pin: one line per field, and the board line must NOT have
# swallowed the kernel line (the exact regression).
lines=$(printf '%s\n' "$META1" | grep -c .)
[ "$lines" -eq 9 ] && ok "donor metadata is exactly 9 lines" \
                   || bad "donor metadata is $lines lines, want 9 (a merged/absent line shifts every later field)"

run_manifest "$META1" "$T/m1.json" || bad "write_manifest failed on a healthy donor"
expect "platform.board is the model alone"     "$T/m1.json" platform.board          "Cyntron A40i-2Eth"
expect "platform.os is the OS PRETTY_NAME"     "$T/m1.json" platform.os             "Armbian 26.5.1 noble"
expect "platform.kernel is uname -r"           "$T/m1.json" platform.kernel         "6.1.0-rc6"
expect "platform.armbian_bsp is the bsp ver"   "$T/m1.json" platform.armbian_bsp    "armbian-bsp-cli-bananapim2ultra-current 26.5.1"
expect "git_commit is the deployed marker"     "$T/m1.json" sa02m_web_build.git_commit "app-1.0.5.54"
expect "boot.uuid is the BOOT uuid"            "$T/m1.json" partitions.boot.uuid    "D8CE-50BA"
expect "root.uuid is a real ext4 uuid"         "$T/m1.json" partitions.root.uuid    "3599389b-2279-4cc9-8fd1-43135f13a73b"
expect "source_device.hostname"                "$T/m1.json" source_device.hostname  "SA-02"
expect "serial_profile honours RELEASE_PROFILE" "$T/m1.json" serial_profile         "sa02m-1eth"

# Without the --profile override the DONOR-read profile must land — a real
# assertion on the serial_profile slot. The donor value is deliberately
# DIFFERENT from the override above, so a leak either way is caught.
build_donor "$T/d1b"
printf 'SA02M_SERIAL_PROFILE=sa02m-2eth\n' > "$T/d1b/etc/sa02m_serial_profile.conf"
META1B=$(run_remote "$T/d1b")
run_manifest "$META1B" "$T/m1b.json" "" || bad "write_manifest failed without RELEASE_PROFILE"
expect "serial_profile from the donor conf"    "$T/m1b.json" serial_profile         "sa02m-2eth"

# ── case 2: degraded donor — absent sources must not shift the survivors ──
# No model file; conf present but WITHOUT the profile key (awk exits 0 printing
# nothing); dpkg-query fails (its `|| echo` never fired through the old pipe).
build_donor "$T/d2"
rm -f "$T/d2/proc/device-tree/model"
printf 'SOMETHING_ELSE=1\n' > "$T/d2/etc/sa02m_serial_profile.conf"
printf '#!/bin/bash\nexit 1\n' > "$T/d2/bin/dpkg-query"
chmod +x "$T/d2/bin/dpkg-query"
META2=$(run_remote "$T/d2")

lines=$(printf '%s\n' "$META2" | grep -c .)
[ "$lines" -eq 9 ] && ok "degraded donor still emits 9 lines" \
                   || bad "degraded donor emits $lines lines, want 9"

run_manifest "$META2" "$T/m2.json" || bad "write_manifest failed on a degraded donor"
expect "degraded: board falls back"             "$T/m2.json" platform.board          "unknown"
expect "degraded: os still correct (no shift)"  "$T/m2.json" platform.os             "Armbian 26.5.1 noble"
expect "degraded: kernel still correct"         "$T/m2.json" platform.kernel         "6.1.0-rc6"
expect "degraded: bsp falls back"               "$T/m2.json" platform.armbian_bsp    "unknown"
expect "degraded: git_commit still correct"     "$T/m2.json" sa02m_web_build.git_commit "app-1.0.5.54"
expect "degraded: root.uuid still the uuid"     "$T/m2.json" partitions.root.uuid    "3599389b-2279-4cc9-8fd1-43135f13a73b"

# ── case 3: a value containing '=' must survive the key/value split ───────
META3=$(printf 'hostname=SA-02\nboard=A40i rev=2\nkernel=6.1.0\nos=Armbian\ngit_commit=abc\nserial_profile=p\nboot_uuid=B\nroot_uuid=R\nbsp=1\n')
run_manifest "$META3" "$T/m3.json" || bad "write_manifest failed on an '=' value"
expect "a value containing '=' is not truncated" "$T/m3.json" platform.board "A40i rev=2"

# ── case 4: malformed DONOR_META must not crash, only fall back ───────────
run_manifest "junk-with-no-separator" "$T/m4.json" \
    && ok "malformed DONOR_META does not crash write_manifest" \
    || bad "malformed DONOR_META crashed write_manifest"
expect "malformed: fields fall back to unknown" "$T/m4.json" platform.kernel "unknown"

printf '\n'
if [ "$fails" -eq 0 ]; then
    printf 'PASS  image manifest donor-metadata protocol green\n'
else
    printf 'FAIL  %d check(s) failed\n' "$fails"
fi
exit $(( fails > 0 ))
