#!/usr/bin/env bash
# comment-mutation-proof-exempt: executes extracted shipped cache functions against live temporary files and measures emitted JSON metadata.
set -euo pipefail
base=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
sed -n '/^cache_is_fresh() {/,/^status_timeout_run() {/p' "${STATUS_CGI_SOURCE:-$base/www/network_config/cgi-bin/status.cgi}" | sed '$d' > "$work/functions.sh"
# shellcheck source=/dev/null
. "$work/functions.sh"
printf '{"value":42}\n' > "$work/cache.json"
touch -d '90 seconds ago' "$work/cache.json"
status_emit_cache "$work/cache.json" 10 > "$work/first.json"
status_emit_cache "$work/cache.json" 10 > "$work/second.json"
build_failure() { return 1; }
STATUS_CACHE_LOCK_WAIT_SEC=0.01 cache_print_or_build "$work/cache.json" 10 build_failure > "$work/fallback.json"
status_emit_snapshot '{"hw_poll_disabled":1}' "$(date +%s)" 10 > "$work/disabled.json"
python3 - "$work" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
rows=[json.loads((p/f'{name}.json').read_text()) for name in ['first','second','fallback']]
assert all(r['value']==42 and r['_status_meta']['age_s']>=89 for r in rows),rows
assert len({r['_status_meta']['measured_at'] for r in rows})==1,rows
assert json.loads((p/'disabled.json').read_text())['_status_meta']['age_s'] is None
print('status cache metadata: reused and failed-build cache retains source age; disabled hardware age unknown')
PY

# These execute the same functions with real failing paths; stderr remains
# separate so a diagnostic cannot be mistaken for a valid response body.
set +e
build_seven() { return 7; }
STATUS_CACHE_LOCK_WAIT_SEC=0.01 cache_print_or_build "$work/missing.json" 10 build_seven > "$work/error.json" 2> "$work/error.log"
rc_missing=$?
STATUS_CACHE_LOCK_WAIT_SEC=0.01 cache_print_or_build "$work/no-directory/cache.json" 10 build_seven > "$work/directory.json" 2> "$work/directory.log"
rc_directory=$?
( cache_is_fresh() { return 0; }; cache_print_or_build "$work/disappeared.json" 10 build_seven ) > "$work/disappeared-output.json" 2> "$work/disappeared.log"
rc_disappeared=$?
mkdir "$work/not-a-file.json"
( cache_is_fresh() { return 0; }; cache_print_or_build "$work/not-a-file.json" 10 build_seven ) > "$work/read-error.json" 2> "$work/read-error.log"
rc_read=$?
set -e
python3 - "$work" "$rc_missing" "$rc_directory" "$rc_disappeared" "$rc_read" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
for name,rc in zip(['error','directory','disappeared-output','read-error'],map(int,sys.argv[2:])):
    assert rc != 0,(name,rc)
    body=(p/(name+'.json')).read_text()
    assert json.loads(body)=={'error':'status_build_failed'},(name,body)
assert int(sys.argv[2])==7,sys.argv
assert 'status_build_failed' in (p/'error.log').read_text()
print('status cache failures: missing/unwritable path, disappearing/unreadable cache return one error JSON and nonzero status')
PY
# A successful lock open must not redirect the rest of the CGI's stderr.
( STATUS_CACHE_LOCK_WAIT_SEC=0.01 cache_print_or_build "$work/cache.json" 10 build_failure >/dev/null; printf 'stderr-owned-by-caller\n' >&2 ) 2> "$work/stderr.log"
test "$(cat "$work/stderr.log")" = stderr-owned-by-caller
# Verify the actual permission boundary even when the suite runs as root.
python3 - "$work" <<'PY'
import json,os,subprocess,sys
from pathlib import Path
p=Path(sys.argv[1]); p.chmod(0o755)
cache=p/'permission-denied.json'; cache.write_text('{"value":42}'); cache.chmod(0)
directory=p/'unwritable'; directory.mkdir(); directory.chmod(0o555)
identity={'user':65534,'group':65534,'extra_groups':[]} if os.geteuid()==0 else {}
try:
    for command in ['status_emit_cache "$2" 10','cache_print_or_build "$2" 10 false']:
        target=cache if command.startswith('status_emit_cache') else directory/'cache.json'
        run=subprocess.run(['bash','-c','. "$1"; STATUS_PART=services; STATUS_CACHE_LOCK_WAIT_SEC=0.01; '+command,'cache-test',str(p/'functions.sh'),str(target)],capture_output=True,text=True,**identity)
        assert run.returncode!=0,run
        assert json.loads(run.stdout)=={'error':'status_build_failed'},run
        assert 'status_build_failed part=services' in run.stderr,run
        assert '.lock' not in run.stderr,'expected lock-open failure must stay quiet'
finally:
    cache.chmod(0o600); directory.chmod(0o700)
print('status cache permissions: unreadable file and unwritable directory fail with one error JSON')
PY
# Failure releases the lock and closes the caller-owned descriptor.
flock -n "$work/missing.json.lock" true
if { : >&8; } 2>/dev/null; then
    echo 'cache failure leaked lock descriptor' >&2
    exit 1
fi
# The empty readable file takes the read-error branch before emitting metadata.
: > "$work/empty.json"
set +e
status_emit_cache "$work/empty.json" 10 > "$work/empty-output.json" 2> "$work/empty.log"
rc_empty=$?
( cache_is_fresh() { return 0; }; cache_print_or_build "$work/cache.json" 10 build_failure ) > /dev/full 2> "$work/output-error.log"
rc_output=$?
set -e
python3 - "$work/empty-output.json" "$rc_empty" "$rc_output" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))=={'error':'status_build_failed'}
assert all(int(rc)!=0 for rc in sys.argv[2:])
print('status cache output: empty reads emit one error JSON; failed writes retain nonzero status')
PY
