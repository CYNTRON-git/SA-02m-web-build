#!/usr/bin/python3
"""Controlled-manager delivery fixture; real PID1 behavior is tested separately."""

import json
import os
from pathlib import Path
import sys

root = Path(os.environ["JOURNAL_QUEUE_FIXTURE"])
sys.path.insert(0, str(root / "opt/sa02m-devices"))
from sa02m_devices import event_journal_queue as queue
from sa02m_devices import event_journal_request as request

arguments = sys.argv[1:]
mode = Path(sys.argv[0]).name
records = root / "queue-records"
records.mkdir(exist_ok=True)

if mode == "systemd-run":
    separator = arguments.index("--")
    options, command = arguments[:separator], arguments[separator + 1:]
    unit = next(option.split("=", 1)[1] for option in options if option.startswith("--unit="))
    encoded = command[-1]
    assert unit == request.unit_name(encoded)
    assert command == queue.argv(encoded)
    assert "--no-block" in options and "--expand-environment=no" in options
    assert "--property=Type=oneshot" in options
    assert "--property=After=" + " ".join(queue.AFTER) in options
    path = queue.TRANSIENT / unit
    assert not path.exists()
    path.write_bytes(queue.fragment(command))
    path.chmod(0o644)
    (records / (unit + ".json")).write_text(json.dumps(command))
elif arguments == ["daemon-reload"]:
    pass
else:
    assert arguments[0] == "show" and arguments[-2] == "--", arguments
    unit = arguments[-1]
    assert unit.startswith(request.PREFIX) and unit.endswith(".service")
    values = dict(Id=unit, LoadState="not-found", ActiveState="inactive", SubState="dead", Job="")
    if (records / (unit + ".json")).exists():
        values.update(LoadState="loaded", ActiveState="activating", SubState="start", Job="42", Transient="yes",
            FragmentPath=str(queue.TRANSIENT / unit), DropInPaths=str(queue._timeout_path(unit)), NeedDaemonReload="no",
            Type="oneshot", User="root", Group="root", DynamicUser="no", Restart="no", KillMode="control-group",
            Environment="", TimeoutStartUSec="16min", JobTimeoutUSec="16min",
            After=" ".join((*queue.AFTER, *sorted(queue.DEFAULT_AFTER))))
    selected = next(option.split("=", 1)[1].split(",") for option in arguments if option.startswith("--property="))
    for key in selected:
        if key in values:
            print(key + "=" + values[key])
