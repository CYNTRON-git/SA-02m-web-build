"""Explicit one-use, short-lived suppression of boot triggers during an upgrade."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

MARKER = Path("/run/sa02m-rules/home-upgrade.json")
SCENARIOS = Path("/etc/sa02m-rules/scenarios.json")


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def consume_skip_boot(path=MARKER, scenarios=SCENARIOS, *, monotonic=time.monotonic, boot=boot_id, owner=0):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != owner or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1:
            raise RuntimeError("unsafe upgrade marker")
        raw = os.read(fd, 2049)
        data = json.loads(raw)
        remaining = float(data["expires_monotonic"]) - monotonic()
        if (len(raw) > 2048 or set(data) != {"nonce", "boot_id", "expires_monotonic", "scenario_sha256"}
                or str(uuid.UUID(data["nonce"])) != data["nonce"] or data["boot_id"] != boot()
                or not 0 < remaining <= 120
                or data["scenario_sha256"] != hashlib.sha256(Path(scenarios).read_bytes()).hexdigest()):
            raise RuntimeError("expired or mismatched upgrade marker")
        current = os.stat(path, follow_symlinks=False)
        if (meta.st_dev, meta.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("upgrade marker changed")
        # Rename is the single-use consume point; retain only an audit receipt.
        os.rename(path, Path(path).with_name("consumed-" + data["nonce"] + ".json"))
        return True
    finally:
        os.close(fd)


def prepare():
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    raw = SCENARIOS.read_bytes()
    rows = json.loads(raw).get("scenarios", [])
    for row in rows:
        if row.get("enabled", True) and row.get("type") == "logic" and row.get("template") != "switch_light":
            raise RuntimeError("logic startup requires a separate preflight")
    MARKER.parent.mkdir(mode=0o700, exist_ok=True)
    meta = MARKER.parent.stat()
    if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o700:
        raise RuntimeError("unsafe upgrade directory")
    data = {"nonce": str(uuid.uuid4()), "boot_id": boot_id(), "expires_monotonic": time.monotonic() + 60,
            "scenario_sha256": hashlib.sha256(raw).hexdigest()}
    fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"nonce": data["nonce"], "valid_seconds": 60, "scope": "explicit_boot_trigger_once"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare"])
    parser.parse_args()
    prepare()
