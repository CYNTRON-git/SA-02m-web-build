#!/usr/bin/python3
"""Run one product entry point in the interpreter selected by the activation owner."""
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    if not sys.flags.isolated or len(sys.argv) < 2:
        raise ValueError("isolated interpreter and consumer ID required")
    entries = json.loads(Path(__file__).with_name("consumers.json").read_bytes())["entries"]
    entry_id = sys.argv.pop(1)
    entry = entries[entry_id]
    if sys.prefix != sys.base_prefix:
        prefix = Path(sys.prefix)
        if prefix.name != entry["group"]:
            raise ValueError("incorrect runtime group")
        sys.path.insert(0,str(Path(__file__).absolute().parent))
        from native_guard import check
        check(prefix)
        for name in ("SA02M_CAREL_DIR","SA02M_LED_DIR","SA02M_SPODES_DIR","SA02M_RULES_DIR"):
            os.environ.pop(name,None)
    for name in tuple(os.environ):
        if name.startswith("PYTHON"):
            os.environ.pop(name)
    os.environ.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    # -I ignores PYTHONUNBUFFERED. Preserve the services' and SSE stream's flush behavior.
    if entry_id == "mqtt-monitor" or entry_id.startswith("sa02m-"):
        sys.stdout.reconfigure(write_through=True, line_buffering=True)
        sys.stderr.reconfigure(write_through=True, line_buffering=True)
    sys.path.extend(entry["roots"])
    kind, target = entry["kind"], entry["target"]
    if kind == "module":
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    elif kind == "script":
        sys.argv[0] = target
        runpy.run_path(target, run_name="__main__")
    elif kind == "stdin":
        sys.argv[0] = "-"
        # Only fixed product heredocs use this form. Privileged sudo grants are
        # restricted to a separate typed scan entry point.
        exec(compile(sys.stdin.buffer.read(), "<stdin>", "exec"),
             {"__name__":"__main__", "__file__":"<stdin>", "__package__":None})
    else:
        raise ValueError("unsupported consumer kind")


if __name__ == "__main__":
    main()
