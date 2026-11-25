"""Whole-journal validation stays indexed without hiding corrupt old events."""

import sqlite3

import pytest

from sa02m_devices import device_events as events, event_journal_state as journal


INVALID_INDEX = "idx_device_events_invalid_generation"
INSERT = ("INSERT INTO device_events(ts,device_id,kind,message,journal_generation) "
          "VALUES(?, 'carel-COM1-1', 'carel_alarm_on', 'alarm', ?)")
BLOB_GENERATIONS = [bytes(32), bytes(range(32)), b"a" * 32, b"\xff" * 32]
NUL_SUFFIX_GENERATIONS = ["a" * 32 + tail for tail in ("\0", "\0junk", "\0\0", "\0legacy")]
NUL_GENERATIONS = ["\0" + "a" * 32, "a" * 31 + "\0z", "legacy\0", "leg\0acy", "\0legacy", *NUL_SUFFIX_GENERATIONS]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_DEVICES_EVENTS_STATE_DIR", raising=False)
    monkeypatch.delenv("STAND_DEVICES_HISTORY_DB", raising=False)
    path = tmp_path / "events.db"
    events.ensure_events_schema(path)
    return path


def indexes(conn):
    return {row[1] for row in conn.execute("PRAGMA index_list(device_events)")}


@pytest.mark.parametrize("after_clear", [False, True])
def test_real_list_work_does_not_scale_with_valid_archive(archive, monkeypatch, after_clear):
    if after_clear:
        events.clear_events(archive)
    generation = journal.read_atomic(journal.domain_for(archive)).generation
    original = sqlite3.connect
    steps, trace = [0], []

    def progress():
        steps[0] += 100
        return 0

    def measured_connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_progress_handler(progress, 100)
        conn.set_trace_callback(trace.append)
        return conn

    counts = []
    for previous, total in ((0, 1000), (1000, 20000)):
        with original(archive) as conn:
            conn.executemany(INSERT, ((i, generation) for i in range(previous, total)))
        conn.close()
        steps[0] = 0
        trace.clear()
        with monkeypatch.context() as measured:
            measured.setattr(sqlite3, "connect", measured_connect)
            result = events.list_events(path=archive, limit=500)
        assert result["count"] == 500
        assert [row["ts"] for row in result["events"]] == list(range(total - 1, total - 501, -1))
        counts.append(steps[0])
    print(f"list_events VM steps ({generation == 'legacy'=}): {counts}")
    assert counts[1] <= counts[0] + 1000, counts
    validation = [sql for sql in trace if sql.startswith("SELECT 1 FROM device_events")]
    assert len(validation) == 1, trace
    with original(archive) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN " + validation[0]).fetchall()
    conn.close()
    assert any(INVALID_INDEX in row[3] for row in plan), plan


@pytest.mark.parametrize("bad", ["broken", "A" * 32, "a" * 31, "g" * 32, "", *BLOB_GENERATIONS, *NUL_GENERATIONS])
@pytest.mark.parametrize("needs_migration", [False, True])
def test_corrupt_event_outside_page_still_refuses(archive, bad, needs_migration):
    with sqlite3.connect(archive) as conn:
        conn.executemany(INSERT, ((i, "legacy" if i % 2 else "a" * 32) for i in range(601)))
    conn.close()
    assert events.list_events(path=archive, limit=500)["count"] == 500
    with sqlite3.connect(archive) as conn:
        if needs_migration:
            conn.execute(f"DROP INDEX IF EXISTS {INVALID_INDEX}")
        # Model an external writer bypassing CHECK, not a normal package insert.
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE device_events SET journal_generation=? WHERE ts=0", (bad,))
        assert conn.execute("SELECT min(ts) FROM (SELECT ts FROM device_events ORDER BY ts DESC LIMIT 500)").fetchone() == (101,)
    conn.close()
    with pytest.raises(journal.JournalStateError, match="invalid stored event generation"):
        events.list_events(path=archive, limit=500)


@pytest.mark.parametrize("bad", [*BLOB_GENERATIONS, *NUL_GENERATIONS])
def test_new_schema_check_refuses_malformed_values_without_casting(archive, bad):
    with sqlite3.connect(archive) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(INSERT, (1, bad))
        assert conn.execute("SELECT count(*) FROM device_events").fetchone() == (0,)
        conn.execute(INSERT, (2, "legacy"))
        conn.execute(INSERT, (3, "a" * 32))
    conn.close()
    assert events.list_events(path=archive)["count"] == 2


@pytest.mark.parametrize("bad", [*BLOB_GENERATIONS, *NUL_SUFFIX_GENERATIONS])
def test_legacy_check_archive_migration_refuses_malformed_value_outside_page(tmp_path, monkeypatch, bad):
    monkeypatch.delenv("STAND_DEVICES_EVENTS_STATE_DIR", raising=False)
    monkeypatch.delenv("STAND_DEVICES_HISTORY_DB", raising=False)
    path = tmp_path / "legacy-check.db"
    # The historical TEXT affinity and CHECK accepted BLOBs: neither converts
    # bound bytes to text, and GLOB reports no text match for a BLOB.
    legacy_check = ("(journal_generation = 'legacy' OR (length(journal_generation) = 32 "
                    "AND journal_generation NOT GLOB '*[^0-9a-f]*'))")
    with sqlite3.connect(path) as conn:
        conn.execute(events._CREATE_EVENTS.replace(events._GENERATION_VALID, legacy_check))
        conn.executemany(INSERT, ((i, "legacy") for i in range(1, 602)))
    conn.close()
    assert events.list_events(path=path, limit=500)["count"] == 500
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP INDEX {INVALID_INDEX}")
        conn.execute(INSERT, (0, bad))
        expected_type = "blob" if isinstance(bad, bytes) else "text"
        assert conn.execute("SELECT typeof(journal_generation) FROM device_events WHERE ts=0").fetchone() == (expected_type,)
        assert INVALID_INDEX not in indexes(conn)
    conn.close()
    with pytest.raises(journal.JournalStateError, match="invalid stored event generation"):
        events.list_events(path=path, limit=500)
    with sqlite3.connect(path) as conn:
        assert INVALID_INDEX in indexes(conn)
        assert conn.execute("SELECT journal_generation, typeof(journal_generation) FROM device_events WHERE ts=0").fetchone() == (bad, expected_type)
    conn.close()


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
@pytest.mark.parametrize("value", ["legacy", "0123456789abcdef" * 2, "a" * 32,
                                    "A" * 32, "a" * 31, "a" * 33, "a" * 31 + "é", "a" * 31 + "ａ",
                                    *BLOB_GENERATIONS, *NUL_GENERATIONS])
def test_sql_and_python_generation_grammar_agree_for_each_db_encoding(encoding, value):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f"PRAGMA encoding='{encoding}'")
        events.ensure_events_schema_on(conn)
        assert conn.execute("PRAGMA encoding").fetchone() == (encoding,)
        if journal.valid_generation(value):
            conn.execute(INSERT, (1, value))
            events.validate_event_tags(conn)
        else:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                conn.execute(INSERT, (1, value))
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(INSERT, (2, value))
            with pytest.raises(journal.JournalStateError, match="invalid stored event generation"):
                events.validate_event_tags(conn)
    finally:
        conn.close()


def test_validator_refuses_missing_or_incompatible_index(archive):
    with sqlite3.connect(archive) as conn:
        conn.execute(f"DROP INDEX {INVALID_INDEX}")
        with pytest.raises(sqlite3.OperationalError, match="no such index"):
            events.validate_event_tags(conn)
        conn.execute(f"CREATE INDEX {INVALID_INDEX} ON device_events(journal_generation) "
                     "WHERE journal_generation='legacy'")
        with pytest.raises(sqlite3.OperationalError, match="no query solution"):
            events.validate_event_tags(conn)
    conn.close()


def test_null_is_invalid_even_in_nullable_external_table():
    # Borrow the package's actual index DDL; only this corruption fixture's
    # table permits NULL, which normal NOT NULL inserts cannot manufacture.
    with sqlite3.connect(":memory:") as canonical:
        events.ensure_events_schema_on(canonical)
        ddl = canonical.execute("SELECT sql FROM sqlite_master WHERE name=?", (INVALID_INDEX,)).fetchone()
    canonical.close()
    assert ddl is not None
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE device_events(id INTEGER PRIMARY KEY, journal_generation TEXT)")
        conn.execute(ddl[0])
        conn.execute("INSERT INTO device_events(journal_generation) VALUES(NULL)")
        with pytest.raises(journal.JournalStateError, match="invalid stored event generation"):
            events.validate_event_tags(conn)
    conn.close()


def test_index_tracks_external_repairs_deletes_and_transaction_rollback(archive):
    with sqlite3.connect(archive) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(INSERT, (1, "broken"))
        conn.commit()
        with pytest.raises(journal.JournalStateError):
            events.validate_event_tags(conn)
        conn.execute("UPDATE device_events SET journal_generation='legacy'")
        events.validate_event_tags(conn)
        conn.rollback()
        with pytest.raises(journal.JournalStateError):
            events.validate_event_tags(conn)
        conn.execute("DELETE FROM device_events")
        events.validate_event_tags(conn)
        conn.rollback()
        with pytest.raises(journal.JournalStateError):
            events.validate_event_tags(conn)
    conn.close()


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_existing_archive_gets_index_with_correct_transaction_owner(archive, caller_transaction):
    conn = sqlite3.connect(archive)
    try:
        conn.execute(f"DROP INDEX IF EXISTS {INVALID_INDEX}")
        conn.execute(INSERT, (1, "legacy"))
        conn.commit()
        if caller_transaction:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(INSERT, (2, "a" * 32))
        trace = []
        conn.set_trace_callback(trace.append)
        events.ensure_events_schema_on(conn)
        assert INVALID_INDEX in indexes(conn)
        assert conn.in_transaction == caller_transaction
        if caller_transaction:
            assert not any(sql.upper().startswith(("COMMIT", "ROLLBACK")) for sql in trace)
            conn.rollback()
            assert INVALID_INDEX not in indexes(conn)
            assert conn.execute("SELECT ts FROM device_events").fetchall() == [(1,)]
            events.ensure_events_schema_on(conn)
        with sqlite3.connect(archive) as observer:
            assert INVALID_INDEX in indexes(observer)
            assert observer.execute("SELECT ts FROM device_events").fetchall() == [(1,)]
        observer.close()
        trace.clear()
        events.ensure_events_schema_on(conn)
        assert not any(sql.upper().startswith(("CREATE", "ALTER", "BEGIN")) for sql in trace)
    finally:
        conn.close()


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_index_migration_failure_does_not_commit_or_steal_transaction(archive, caller_transaction):
    conn = sqlite3.connect(archive)
    try:
        for name in (INVALID_INDEX, "idx_device_events_ts"):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        before = indexes(conn)
        if caller_transaction:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(INSERT, (1, "legacy"))
        trace = []
        conn.set_trace_callback(trace.append)
        conn.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_CREATE_INDEX and name == INVALID_INDEX
                            else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            events.ensure_events_schema_on(conn)
        conn.set_authorizer(None)
        assert conn.in_transaction == caller_transaction
        if caller_transaction:
            assert not any(sql.upper().startswith(("COMMIT", "ROLLBACK")) for sql in trace)
            assert conn.execute("SELECT ts FROM device_events").fetchall() == [(1,)]
            conn.rollback()
        assert indexes(conn) == before
        assert conn.execute("SELECT count(*) FROM device_events").fetchone() == (0,)
    finally:
        conn.close()
