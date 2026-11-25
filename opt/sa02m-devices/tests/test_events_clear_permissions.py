"""Owner smoke plus an explicit privileged fixture for real non-owner probes.

Run the probe as root with a private test directory argument. The child drops
to nobody and proves the target files, not an inaccessible outer fixture,
enforce the boundary. No root-only pytest skip can stand in for this probe.
"""

import json
import os
from pathlib import Path
import pwd
import subprocess
import sys

from sa02m_devices import event_journal_state as journal


def test_effective_owner_uses_private_standalone_state(tmp_path):
    domain = journal.Domain(tmp_path / "authority", True, tmp_path / "archive.db")
    state = journal.bootstrap(domain, ready=True)
    assert journal.read_atomic(domain) == state
    assert domain.directory.stat().st_uid == os.geteuid()
    assert journal.clear(domain, lambda: [], timeout=0.2) == 0


def permission_probe(root: Path) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("the non-owner fixture must be prepared by root")
    root = root.resolve()
    scratch = Path(os.environ["TMPDIR"]).resolve()
    if not root.is_relative_to(scratch):
        raise RuntimeError("permission fixture must stay inside assigned scratch")
    root.mkdir(parents=True, mode=0o755, exist_ok=True)
    path = root
    while path != scratch.parent:
        path.chmod(0o755)
        path = path.parent
    domain = journal.Domain(root / "authority")
    before = journal.bootstrap(domain, ready=True)
    probe_file = root / "private-file"
    probe_file.write_text("permission fixture\n")
    probe_file.chmod(0o600)
    nobody = pwd.getpwnam("nobody")
    child = subprocess.run([
        sys.executable, "-B", "-c", '''
import json, os, stat, sys
from pathlib import Path
from sa02m_devices import event_journal_state as journal
root = Path(sys.argv[1])
assert os.geteuid() != 0
info = (root / "private-file").stat()
assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
assert stat.S_IMODE((root / "authority").stat().st_mode) == 0o700
for path in (root / "private-file", root / "authority" / "state.json"):
    try:
        fd = os.open(path, os.O_WRONLY)
    except PermissionError:
        pass
    else:
        os.close(fd)
        raise RuntimeError("non-owner could modify private authority data")
try:
    journal.read_atomic(journal.Domain(root / "authority"))
except journal.JournalStateError:
    pass
else:
    raise RuntimeError("non-owner opened production authority")
print(json.dumps({"non_owner_uid": os.geteuid(), "outer_fixture_traversable": True,
                  "private_file_write": "denied", "authority_write": "denied",
                  "authority_read": "denied"}))
''', str(root)], user=nobody.pw_uid, group=nobody.pw_gid, extra_groups=[],
        capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert journal.read_atomic(domain) == before
    print(child.stdout.strip())
    print("owner validation: passed; authority unchanged")


if __name__ == "__main__":
    permission_probe(Path(sys.argv[1]))
