#!/usr/bin/python3
"""Isolated administrative entry point; imports only its installed package."""

from pathlib import Path
import os
import stat
import sys
import tempfile

if __name__ == "__main__":
    # A private empty prefix prevents reads of timestamp-valid stale package
    # bytecode as well as writes; -B alone only prevents writes.
    runtime = os.lstat("/run")
    if not stat.S_ISDIR(runtime.st_mode) or runtime.st_uid != 0 or runtime.st_mode & 0o022:
        raise RuntimeError("unsafe journal import runtime directory")
    with tempfile.TemporaryDirectory(prefix="sa02m-journal-import-", dir="/run") as cache:
        info = os.lstat(cache)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700 \
                or os.listdir(cache):
            raise RuntimeError("unsafe journal import cache directory")
        sys.pycache_prefix = cache
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from sa02m_devices.event_journal_startup import main
        raise SystemExit(main())
