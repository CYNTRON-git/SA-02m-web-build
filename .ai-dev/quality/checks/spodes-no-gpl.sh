#!/bin/bash
# SPODES stays clean-room: no Gurux and no spodes-rs import, and a COM that
# mixes HDLC with Modbus is a named refusal. The flasher does not install
# the package.
#
# The import sweep and the flasher pin are fail-IF-PRESENT: commenting the
# banned line out removes the needle, so that half cannot be a comment-out
# case. The positive pin is "mixed_framing" in bridge_bus.py, matched on
# comment-stripped text. Registered in comment-mutation-proof: commenting
# that string out turns this gate RED. A live `import gurux` was observed
# RED on 2026-10-03 and then removed.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. .ai-dev/quality/checks/lib_check.sh || { echo "FAIL  cannot source lib_check.sh"; exit 1; }
fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

if stripped_has opt/sa02m-modbus-mqtt/bridge_bus.py '"mixed_framing"'; then
    ok "mixed_framing is a bridge_bus reason"
else
    bad "mixed_framing missing from bridge_bus.REASONS"
fi

seen=0
hits=""
while IFS= read -r f; do
    [ -n "$f" ] || continue
    seen=$((seen + 1))
    text=$(stripped_text "$f")
    if text_matches "$text" '(^|[[:space:]])(import|from)[[:space:]]+(gurux|spodes_rs|gurux_dlms)\b'; then
        hits="${hits}
$f"
    fi
done < <(find opt/sa02m-spodes opt/sa02m-modbus-mqtt/bridge_spodes.py -name '*.py' -type f 2>/dev/null)
if [ "$seen" -eq 0 ]; then
    bad "no Python files under opt/sa02m-spodes"
elif [ -n "$hits" ]; then
    bad "GPL client imported:$hits"
else
    ok "no gurux / spodes_rs import"
fi

if stripped_has scripts/04-flasher.sh 'sa02m_install_spodes_pkg'; then
    bad "04-flasher.sh installs the SPODES package"
else
    ok "flasher does not install the SPODES package"
fi

if [ "$fails" -eq 0 ]; then
    echo "spodes-no-gpl: ALL OK"
    exit 0
fi
echo "spodes-no-gpl: $fails FAILURE(S)"
exit 1
