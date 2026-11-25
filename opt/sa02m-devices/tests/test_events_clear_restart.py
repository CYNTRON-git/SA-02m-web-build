"""Fresh interpreters and process death test durable journal publication."""

import json
import os
import subprocess
import sys
import time

import pytest

from sa02m_devices import device_events, event_journal_state as journal
from tests.test_events_clear_continuity import _seed_alarm, _samples
from tests.journal_support import private_authority


def _run(code, *args):
    code = "from tests.journal_support import install_private_authority\ninstall_private_authority()\n" + code
    return subprocess.run([sys.executable, "-B", "-c", code, *map(str, args)],
                          capture_output=True, text=True, timeout=15)


@pytest.fixture
def restart_paths(tmp_path, monkeypatch, private_authority):
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "authority"))
    journal.bootstrap(journal.domain_for(), ready=True)
    source, active = tmp_path / "sd.db", tmp_path / "usb.db"
    _seed_alarm(source, time.time() - 100)
    _seed_alarm(active, time.time() - 50)
    return source, active


@pytest.mark.parametrize("point,exitcode", [("before_replace", 81), ("after_replace", 82), ("after_dir_fsync", 83)])
def test_process_death_never_reexposes_old_events_after_publication(restart_paths, tmp_path, point, exitcode):
    source, active = restart_paths
    before = _samples(source)
    detached = tmp_path / "absent-sd.db"
    source.replace(detached)
    result = _run('''
import os, stat, sys
from pathlib import Path
from sa02m_devices import device_events, event_journal_state as journal
point = sys.argv[2]
original_replace, original_fsync = os.replace, os.fsync
def replace(*args, **kwargs):
    if point == "before_replace":
        os._exit(81)
    result = original_replace(*args, **kwargs)
    if point == "after_replace":
        os._exit(82)
    return result
def fsync(fd):
    original_fsync(fd)
    if point == "after_dir_fsync" and stat.S_ISDIR(os.fstat(fd).st_mode):
        os._exit(83)
os.replace, os.fsync = replace, fsync
device_events.clear_events(Path(sys.argv[1]))
raise RuntimeError("crash point not reached")
''', active, point)
    assert result.returncode == exitcode, result.stderr
    detached.replace(source)
    fresh = _run('''
import json, sys
from pathlib import Path
from sa02m_devices.device_events import list_events
print(json.dumps(list_events(path=Path(sys.argv[1]))))
''', source)
    assert fresh.returncode == 0, fresh.stderr
    events = json.loads(fresh.stdout)["events"]
    assert [e["kind"] for e in events] == (["carel_alarm_on"] if point == "before_replace" else [])
    assert _samples(source) == before


def test_fresh_writer_records_first_real_off_after_clear(restart_paths):
    source, active = restart_paths
    device_events.clear_events(active)
    result = _run('''
import json, sys, time
from pathlib import Path
from sa02m_devices.history_writer import ArchiveWriter
from sa02m_devices.device_events import list_events
from tests.test_events_clear_continuity import _snapshot
path = Path(sys.argv[1])
writer = ArchiveWriter(path, flush_every_s=0)
try:
    writer.add(_snapshot(time.time(), 0), mr_due=True)
    created = writer.flush()
    print(json.dumps({"created": created, "listed": list_events(path=path)["events"]}))
finally:
    writer.close()
''', source)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert [row["kind"] for row in output["created"]] == ["carel_alarm_off"]
    assert [row["kind"] for row in output["listed"]] == ["carel_alarm_off"]


def test_missing_shared_state_fails_in_fresh_reader_but_logger_keeps_samples(restart_paths):
    source, _ = restart_paths
    before = _samples(source)
    (journal.domain_for().directory / "state.json").unlink()
    result = _run('''
import json, sys, time
from pathlib import Path
from sa02m_devices import device_events, event_journal_state as journal
from sa02m_devices.history_writer import ArchiveWriter
from tests.test_events_clear_continuity import _snapshot
path = Path(sys.argv[1])
try:
    device_events.list_events(path=path)
except journal.JournalStateError:
    pass
else:
    raise RuntimeError("missing state was accepted")
writer = ArchiveWriter(path, flush_every_s=0)
try:
    writer.add(_snapshot(time.time(), 0), mr_due=True)
    assert writer.flush() == []
finally:
    writer.close()
''', source)
    assert result.returncode == 0, result.stderr
    after = _samples(source)
    for table in before:
        assert after[table][:len(before[table])] == before[table]
        assert len(after[table]) == len(before[table]) + 1
