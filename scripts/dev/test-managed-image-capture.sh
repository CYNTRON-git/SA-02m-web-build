#!/bin/bash
# comment-mutation-proof-exempt: Runs the shipped orchestration against device-free
# fixtures. Source rewrites retarget fixed host paths and fixture size only.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
[ "$EUID" -eq 0 ] || { echo 'Run in an isolated Linux environment as root' >&2; exit 1; }
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
imaging="$fixture/repo/tools/imaging"
mkdir -p "$imaging" "$fixture/bin"
sed "s#^EMMC_BYTES=.*#EMMC_BYTES=6#; s#/var/lib/sa02m-image-captures#$fixture/captures#g" \
    tools/imaging/make-image.sh > "$imaging/make-image.sh"
cp tools/imaging/{stream-after-cleanup,cleanup-donor,fix-donor-after-abort}.sh "$imaging/"
touch "$fixture/key" "$imaging/patch-firstboot-image.sh"
for tool in pishrink.sh e2fsck resize2fs parted mkimage losetup mount umount; do
    printf '#!/bin/bash\nexit 99\n' > "$fixture/bin/$tool"
done
cat > "$fixture/bin/ssh" <<'EOS'
#!/bin/bash
set -euo pipefail
command=${!#}
printf '%s\n' "$command" >> "$TRACE"
case "$command" in
    'bash -s -- --probe-mode') echo 'managed 1.0.8.3'; exit "${PROBE_RC:-0}" ;;
    'test -b /dev/mmcblk2 && blockdev --getsize64 /dev/mmcblk2') echo 6 ;;
    '/usr/local/libexec/sa02m-runtime-activate capture-receipt') printf '{"fixture":true}\n' ;;
    'bash -s') printf 'hostname=fixture\n' ;;
    'bash -s -- --no-zerofill --capture-receipt-sha256 '*|'bash -s -- --capture-receipt-sha256 '*) printf rawimg; exit "$STREAM_RC" ;;
    *) exit 96 ;;
esac
EOS
cat > "$fixture/bin/sudo" <<'EOS'
#!/bin/bash
set -euo pipefail
printf '%s\n' "$1" >> "$TRACE"
case "$1" in
    pishrink.sh) [ "$#" -eq 4 ]; printf shrink >> "$4" ;;
    bash) [ "$#" -eq 3 ]; printf patch >> "$3" ;;
    *) exit 95 ;;
esac
EOS
cat > "$imaging/seal-captured-runtime.sh" <<'EOS'
#!/bin/bash
set -euo pipefail
printf 'seal\n' >> "$TRACE"
[ "$(cat "$1")" = rawimg ]
[ "$(cat "$2")" = rawimgshrinkpatch ]
[ "$(stat -c '%u:%g:%a' "$1")" = '0:0:400' ]
[ "$(stat -c '%u:%g:%a' "$3")" = '0:0:400' ]
python3 - "$1" "$3" "$4" <<'PY'
import hashlib, json, pathlib, sys
raw, receipt, passport = map(pathlib.Path, sys.argv[1:])
assert json.loads(passport.read_bytes()) == {
    'schema': 1,
    'raw_sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
    'receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(),
}
PY
exit_code=$SEAL_RC
hash=$(printf '%064d' 0)
printf '{"schema":1,"source":"%s","release_id":"%s","manifest_sha256":"%s","selector_sha256":"%s"}\n' \
    "$hash" "$hash" "$hash" "$hash"
exit "$exit_code"
EOS
cat > "$imaging/zero-free-ext4.py" <<'PY_ZERO'
import hashlib, json, os, sys
from pathlib import Path
source, final = map(Path, sys.argv[1:])
assert source.read_bytes() == b'rawimgshrinkpatch'
assert (source.parent/'raw.img').read_bytes() == b'rawimg'
assert Path(os.environ['TRACE']).read_text().splitlines()[-1] == 'seal'
with open(os.environ['TRACE'], 'a') as stream:
    stream.write('final-zero\n')
if os.environ.get('ZERO_RC'):
    raise SystemExit(int(os.environ['ZERO_RC']))
data = source.read_bytes() + b'zero'
final.write_bytes(data)
print(json.dumps(dict(copy_sha256=hashlib.sha256(data).hexdigest(), image_bytes=len(data), cleared_blocks=1)))
PY_ZERO
chmod +x "$fixture/bin/"*
export PATH="$fixture/bin:$PATH" SA02M_DEVICE_ENV="$fixture/no-env"
export TRACE="$fixture/trace" STREAM_RC=0 SEAL_RC=0

capture() {
    local out=$1
    shift
    : > "$TRACE"
    bash "$imaging/make-image.sh" --ip 192.0.2.1 --key "$fixture/key" \
        --name fixture --out-dir "$fixture/$out" --version 1.0.8.3 \
        --no-zerofill --final-xz-level 1 "$@" > "$fixture/$out.log" 2>&1
}
assert_refused() {
    local out=$1
    shift
    if capture "$out" "$@"; then
        cat "$fixture/$out.log"
        echo "Unexpected accepted capture: $out" >&2
        exit 1
    fi
    [ -z "$(find "$fixture/$out" -mindepth 1 -print -quit)" ]
    ! grep -q 'READY:' "$fixture/$out.log"
}

PROBE_RC=1 assert_refused donor-preflight --no-cleanup
[ "$(cat "$TRACE")" = 'bash -s -- --probe-mode' ]
assert_refused cleanup-required
grep -q -- "управляемый захват" "$fixture/cleanup-required.log" || { cat "$fixture/cleanup-required.log"; exit 1; }
[ "$(cat "$TRACE")" = 'bash -s -- --probe-mode' ]
for nonzero in 44 85; do
    STREAM_RC=$nonzero assert_refused "failed-stream-$nonzero" --no-cleanup
    ! grep -xq pishrink.sh "$TRACE"
    grep -q "ssh_rc=$nonzero" "$fixture/failed-stream-$nonzero.log"
done
SEAL_RC=42 assert_refused failed-seal --no-cleanup
grep -qx seal "$TRACE"
if ! capture ready --no-cleanup; then cat "$fixture/ready.log"; exit 1; fi
grep -q 'READY:' "$fixture/ready.log"
[ "$(xz -dc "$fixture/ready/fixture.img.xz")" = rawimgshrinkpatch ]
grep -q '"runtime_image_seal"' "$fixture/ready/fixture.manifest.json"
( cd "$fixture/ready"; sha256sum -c fixture.img.xz.sha256 )
! grep -qx final-zero "$TRACE"
ZERO_RC=3 assert_refused failed-final-zero --no-cleanup --zerofill
grep -qx final-zero "$TRACE"
SEAL_RC=42 assert_refused zero-before-seal --no-cleanup --zerofill
! grep -qx final-zero "$TRACE"
capture clean-copy --no-cleanup --zerofill || { cat "$fixture/clean-copy.log"; exit 1; }
[ "$(xz -dc "$fixture/clean-copy/fixture.img.xz")" = rawimgshrinkpatchzero ]
cmp <(xz -dc "$fixture/clean-copy/fixture.img.xz") "$fixture/clean-copy/fixture.img"
( cd "$fixture/clean-copy"; sha256sum -c fixture.img.xz.sha256 )
python3 - "$fixture/clean-copy" <<'PY_COMPARE'
import hashlib, json, sys
from pathlib import Path
out = Path(sys.argv[1])
doc = json.loads((out/'fixture.manifest.json').read_text())
assert doc['image_sha256'] == hashlib.sha256((out/'fixture.img.xz').read_bytes()).hexdigest()
assert doc['uncompressed_sha256'] == hashlib.sha256((out/'fixture.img').read_bytes()).hexdigest()
PY_COMPARE
echo 'PASS managed imaging admission, full-size failure, seal gate and publication'
