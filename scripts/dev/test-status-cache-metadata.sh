#!/usr/bin/env bash
# comment-mutation-proof-exempt: executes extracted shipped cache functions against live temporary files and measures emitted JSON metadata.
set -euo pipefail
base=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
sed -n '/^cache_is_fresh() {/,/^status_timeout_run() {/p' "$base/www/network_config/cgi-bin/status.cgi" | sed '$d' > "$work/functions.sh"
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
