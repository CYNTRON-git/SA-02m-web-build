#!/usr/bin/env bash
# Exercise the shipped Python modules on top of the package's original base.
set -euo pipefail
package=$(cd "${1:?usage: test-home-pilot-payload.sh PACKAGE_DIRECTORY}" && pwd)
repo=$(cd "$(dirname "$0")/../.." && pwd)
base=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["base"])' "$package/manifest.json")
[[ "$base" =~ ^[0-9a-f]{40,64}$ ]]
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT
git -C "$repo" archive "$base" -- opt/sa02m-alice | tar -x -C "$fixture"
python3 -I -B - "$package" "$fixture" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch

package, fixture = map(Path, sys.argv[1:])
manifest = json.loads((package / "manifest.json").read_bytes())
for entry in manifest["runtime"]:
    name = entry["source"]
    if not entry["destination"].startswith("/opt/sa02m-alice/"):
        continue
    destination = Path(entry["destination"]).relative_to("/opt/sa02m-alice")
    if ".." in destination.parts or name != "payload/opt/sa02m-alice/" + destination.as_posix():
        raise ValueError("invalid pilot runtime path")
    data = (package / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["files"][name]["sha256"]:
        raise ValueError("pilot hash mismatch: " + name)
    target = fixture / "opt/sa02m-alice" / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

sys.path.insert(0, str(fixture / "opt/sa02m-alice"))
from sa02m_alice.client.sio_connection import AliceSocketIO
from sa02m_alice.client.sio_handlers import SioHandlers
from sa02m_alice.common import constants as C
from sa02m_alice.home import bridge

sio = AliceSocketIO(profile=C.PROFILE_CLOUD)
sio._connected, sio._sio = True, Mock()
publish = Mock()
handler = SioHandlers(None, publish_mqtt=publish, emit_response=sio.emit_response, profile=C.PROFILE_CLOUD)
request = {"request_id": "pilot-probe", "method": "GET", "path": "/meta"}
answer = {"status": 200, "body": {"ok": True, "controller_id": "SA-test"}}
with patch.object(bridge, "request", return_value=answer) as board:
    handler.handle("home_api_v1", request)
board.assert_called_once_with(request)
sio._sio.emit.assert_called_once_with("alice_devices_response", {"request_id": "pilot-probe", "payload": answer})
publish.assert_not_called()
print("Pilot overlay emitted the expected Home response on base", manifest["base"])
PY
