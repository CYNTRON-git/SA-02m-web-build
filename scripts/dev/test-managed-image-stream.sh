#!/bin/bash
# comment-mutation-proof-exempt: exercises the actual donor stream in a sandbox
# with real flock descriptors and staged synthetic fixtures, no SSH or devices.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
[ "$EUID" -eq 0 ] && [ "$(uname -s)" = Linux ] || { echo 'Run in isolated Linux as root' >&2; exit 1; }
fixture=$(mktemp -d /tmp/sa02m-capture-test-XXXXXXXX)
trap 'rm -rf -- "$fixture"' EXIT
export fixture
tool="$fixture/stream.sh"
sed -E "s#/(etc|run|root|sys|var/(lib|www)|usr/local/libexec)(/|[[:space:]\"])#$fixture/\1\3#g; s#/zero[.]fill#$fixture/zero.fill#g" \
    tools/imaging/stream-after-cleanup.sh > "$tool"
state=$fixture/var/lib/sa02m-runtime-transition
mkdir -p $fixture/var/www/network_config $fixture/var/lib/dbus $fixture/etc/systemd/system $fixture/etc/ssh $fixture/usr/local/libexec $fixture/root $fixture/run/guard-fixture
cp www/network_config/VERSION $fixture/var/www/network_config/VERSION
archive="$fixture/root/etc-backup-20261127-160403.tgz"
touch "$fixture/root/etc-backup-user.tgz" "$fixture/root/etc-backup-A0261127-160403.tgz"
for v in 1.0.8.2 1.0.8.3 1.0.8.10; do
  sed -E -i "s/^[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+$/$v/" $fixture/var/www/network_config/VERSION
  expected=managed
  [ "$v" != 1.0.8.2 ] || expected=legacy
  [ "$(bash "$tool" --probe-mode)" = "$expected $v" ]
  touch "$archive"
  if [[ "$expected" = managed ]]; then
    if bash "$tool" --probe-mode >"$fixture/out" 2>"$fixture/log"; then exit 1; fi
    [[ ! -s "$fixture/out" ]]
    grep -Fq "backup remains: $archive" "$fixture/log"
    if bash "$tool" --legacy-only >"$fixture/out" 2>"$fixture/log"; then exit 1; fi
    grep -Fq 'managed runtime requires guarded make-image.sh' "$fixture/log"
  else
    [[ $(bash "$tool" --probe-mode) = "legacy $v" ]]
    bash "$tool" --legacy-only
  fi
  rm "$archive"
  echo "classified $expected $v and checked admission with a private archive"
done
mkdir -p "$state" $fixture/var/lib/sa02m-update
touch $fixture/run/sa02m-install.lock $fixture/var/lib/sa02m-update/update.lock $fixture/var/lib/sa02m-update/control.lock "$state/acquisition.lock"
printf '{"fixture":true}\n' > "$state/capture.json"
sha=$(sha256sum -- "$state/capture.json" | cut -d' ' -f1)
for fake in sync ethtool rmmod; do ln -s /bin/true "$fixture/run/guard-fixture/$fake"; done
cat > $fixture/run/guard-fixture/systemctl <<'SYS'
#!/bin/bash
if [[ "${SCENARIO:-}" = late-archive && "$1" = stop && "$2" = nginx ]]; then
  touch "$fixture/root/etc-backup-20261127-160403.tgz"
fi
if [[ "$1" = show ]]; then echo inactive; fi
SYS
cat > $fixture/run/guard-fixture/busctl <<'BUS'
#!/bin/bash
if [[ "$1" = get-property ]]; then echo 't 0'; fi
BUS
chmod +x $fixture/run/guard-fixture/{busctl,systemctl}
cat > $fixture/run/guard-fixture/dd <<'DD'
#!/bin/bash
set -eu
if [[ "${SCENARIO:-}" = legacy ]]; then
  touch "$fixture/run/dd-started"
  printf legacy-sample
  exit 0
fi
state=$fixture/var/lib/sa02m-runtime-transition
for f in $fixture/run/sa02m-install.lock $fixture/var/lib/sa02m-update/update.lock $fixture/var/lib/sa02m-update/control.lock "$state/acquisition.lock"; do
  if flock -n -x "$f" true; then echo "unheld lock at dd" >&2; exit 30; fi
done
flock -n -s "$state/acquisition.lock" true
sha=$(sha256sum "$state/capture.json" | cut -d' ' -f1)
grep -Fxq "capture:$sha" $fixture/run/sa02m-imaging.lock
touch $fixture/run/dd-started
case "${SCENARIO:-}" in
  competing-reader)
    echo "other reader acquired" >&2
    /bin/bash -c 'exec 5<&- 6<&- 7<&- 8<&-; exec 9<$fixture/var/lib/sa02m-runtime-transition/acquisition.lock; flock -s 9; touch $fixture/run/other-reader; sleep 2' </dev/null >/dev/null 2>&1 &
    until [ -e $fixture/run/other-reader ]; do sleep 0.03; done;;
  altered-marker) printf '\n' >> $fixture/run/sa02m-imaging.lock;;
  replaced-marker) mv $fixture/run/sa02m-imaging.lock $fixture/run/original-held; printf 'foreign' >$fixture/run/sa02m-imaging.lock;;
esac
printf sample
exit "${TEST_DD_RC:-0}"
DD
cat >$fixture/usr/local/libexec/sa02m-runtime-activate <<'REL'
#!/bin/bash
set -eu
[ "$1" = release-capture ] || exit 89
test "${SCENARIO:-}" != failed-release || exit 85
test "$2" = "$(sha256sum $fixture/var/lib/sa02m-runtime-transition/capture.json | cut -d' ' -f1)"
test ! -e $fixture/run/sa02m-imaging.lock
flock -n -x $fixture/var/lib/sa02m-runtime-transition/acquisition.lock true
touch $fixture/run/release-complete
printf '{"schema":1,"phase":"complete","reason":"capture_released"}\n'
REL
chmod +x $fixture/usr/local/libexec/sa02m-runtime-activate $fixture/run/guard-fixture/dd
export PATH="$fixture/run/guard-fixture:$PATH"
for scenario in archive-at-entry late-archive; do
  export SCENARIO=$scenario
  if [[ "$scenario" = archive-at-entry ]]; then ln -s "$fixture/root/absent" "$archive"; fi
  rc=0
  timeout 15 bash "$tool" --no-zerofill --capture-receipt-sha256 "$sha" >"$fixture/out" 2>"$fixture/log" || rc=$?
  [[ $rc = 1 && ! -s "$fixture/out" && ! -e "$fixture/run/dd-started" && ! -e "$fixture/run/release-complete" ]]
  grep -Fq "backup remains: $archive" "$fixture/log"
  if [[ "$scenario" = archive-at-entry ]]; then
    [[ ! -e "$fixture/run/sa02m-imaging.lock" && -L "$archive" ]]
  else
    grep -Fxq "capture:$sha" "$fixture/run/sa02m-imaging.lock"
    rm "$fixture/run/sa02m-imaging.lock"
  fi
  rm "$archive"
  echo "refused $scenario before raw read"
done
unset SCENARIO
for scenario in wrong-hash foreign-marker foreign-symlink foreign-fifo busy-acquisition; do
  case "$scenario" in
    wrong-hash) digest=$(printf '0%.0s' {1..64});;
    foreign-marker) printf 'not-owned' >$fixture/run/sa02m-imaging.lock; digest=$sha;;
    foreign-symlink) ln -s $fixture/run/absent $fixture/run/sa02m-imaging.lock;;
    foreign-fifo) mkfifo $fixture/run/sa02m-imaging.lock;;
    busy-acquisition) exec 9<"$state/acquisition.lock"; flock -x 9;;
  esac
  rc=0; timeout 3 bash "$tool" --no-zerofill --capture-receipt-sha256 "$digest" >$fixture/run/stream-output 2>$fixture/run/stream-log || rc=$?
  test "$rc" -gt 0 && test "$rc" -lt 124 && test ! -e $fixture/run/dd-started && test ! -s $fixture/run/stream-output || exit 50
  if [[ "$scenario" = foreign-marker ]]; then grep -Fq not-owned $fixture/run/sa02m-imaging.lock; fi
  if [[ "$scenario" = foreign-symlink ]]; then test -L $fixture/run/sa02m-imaging.lock; fi
  if [[ "$scenario" = foreign-fifo ]]; then test -p $fixture/run/sa02m-imaging.lock; fi
  if [[ "$scenario" = busy-acquisition ]]; then flock -u 9; exec 9<&-; fi
  rm -f $fixture/run/sa02m-imaging.lock
  echo "refused $scenario"
done
for scenario in good dd-failure failed-release replaced-marker altered-marker competing-reader; do
  export SCENARIO=$scenario
  status=0
  case "$scenario" in dd-failure) status=44;; failed-release) status=85;; competing-reader|altered-marker|replaced-marker) status=1;; esac
  export TEST_DD_RC=0; if [[ "$scenario" = dd-failure ]]; then export TEST_DD_RC=$status; fi
  rc=0; timeout 15 bash "$tool" --no-zerofill --capture-receipt-sha256 "$sha" >$fixture/run/stream-output 2>$fixture/run/stream-log || rc=$?
  if [ "$rc" != "$status" ]; then echo "WRONG rc for $scenario: rc=$rc expected=$status" >&2; cat $fixture/run/stream-log >&2; exit 1; fi
  test "$(cat $fixture/run/stream-output)" = sample
  test -f $fixture/run/dd-started
  echo "dd fixture: all locks held, stdout exact, rc=$rc"
  printf '%s marker=%s release=%s\n' "$scenario" "$(test -e $fixture/run/sa02m-imaging.lock && echo present || echo absent)" "$(test -e $fixture/run/release-complete && echo confirmed || echo none)"
  case "$scenario" in
    good) test -e $fixture/run/release-complete && test ! -e $fixture/run/sa02m-imaging.lock || exit 1;;
    failed-release) test ! -e $fixture/run/release-complete || exit 1;;
    *) test ! -e $fixture/run/release-complete && test -e $fixture/run/sa02m-imaging.lock || exit 1;;
  esac
  rm -f $fixture/run/sa02m-imaging.lock $fixture/run/dd-started $fixture/run/release-complete
done

# Native Windows admits legacy donors before optional cleanup, then runs this same stream.
mv "$state" "$state.saved"
mv "$fixture/usr/local/libexec/sa02m-runtime-activate" "$fixture/activate.saved"
sed -E -i 's/^[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+$/1.0.8.2/' "$fixture/var/www/network_config/VERSION"
touch "$archive"
bash "$tool" --legacy-only
if SCENARIO=legacy bash "$tool" --no-zerofill --no-id-reset >"$fixture/out" 2>"$fixture/log"; then exit 1; fi
grep -Fq "backup remains: $archive" "$fixture/log"
[[ ! -s "$fixture/out" && ! -e "$fixture/run/dd-started" && ! -e "$fixture/run/sa02m-imaging.lock" ]]
rm "$archive"
SCENARIO=legacy bash "$tool" --no-zerofill --no-id-reset >"$fixture/out" 2>"$fixture/log"
[[ $(cat "$fixture/out") = legacy-sample && -f "$fixture/run/dd-started" ]]
echo 'legacy stream refuses outstanding backup and reads successfully after cleanup'
