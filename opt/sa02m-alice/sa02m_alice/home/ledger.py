"""Durable bounded dedupe. A crash after dispatch starts is never auto-replayed."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .contract import APIError, IDENTIFIER, encoded


# Leave room for the envelope and ASCII escaping on the cloud transport.
RESULT_DETAIL_BYTES = 64 * 1024


def _bounded_receipt(row):
    result = row.get("result")
    if result is None or len(encoded(result)) <= RESULT_DETAIL_BYTES:
        return row
    summary = {"details_omitted": True}
    if isinstance(result, dict):
        if isinstance(result.get("ok"), bool):
            summary["ok"] = result["ok"]
        kind = row["operation"].partition(".")[0]
        item = result.get(kind)
        if isinstance(item, dict) and isinstance(item.get("id"), str) and IDENTIFIER.fullmatch(item["id"]):
            summary[kind] = {"id": item["id"]}
    return {**row, "result": summary}


class Ledger:
    def __init__(self, path, *, clock=time.time, monotonic=time.monotonic, max_rows=10000, retention=86400):
        self.clock, self.max_rows, self.retention = clock, max_rows, retention
        self.monotonic = monotonic
        self.lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=2, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA max_page_count=4096")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, hash TEXT NOT NULL, created REAL NOT NULL, receipt TEXT NOT NULL)")
        self.db.commit()
        # Wall-clock changes must never evict a receipt while its lease can
        # still authorize replay. Monotonic ages do not survive a restart, so
        # conservatively retain every loaded row for a full new retention
        # period. Restart already invalidates all in-memory leases.
        started = self.monotonic()
        self.ages = {cid: started for (cid,) in self.db.execute("SELECT id FROM receipts")}
        with self.lock:
            for cid, raw in self.db.execute("SELECT id,receipt FROM receipts").fetchall():
                row = json.loads(raw)
                if row["status"] in ("accepted", "dispatch_started", "published"):
                    row.update(status="outcome_unknown", confirmation="daemon_restarted", updated_at=self.clock())
                    self._save(cid, row)

    def close(self):
        self.db.close()

    def _save(self, cid, receipt):
        receipt = _bounded_receipt(receipt)
        with self.db:
            self.db.execute("UPDATE receipts SET receipt=? WHERE id=?", (encoded(receipt).decode(), cid))
        return receipt

    def get(self, cid):
        with self.lock:
            row = self.db.execute("SELECT receipt FROM receipts WHERE id=?", (cid,)).fetchone()
            return _bounded_receipt(json.loads(row[0])) if row else None

    def existing(self, cid, digest):
        with self.lock:
            row = self.db.execute("SELECT hash,receipt FROM receipts WHERE id=?", (cid,)).fetchone()
            if row and row[0] != digest:
                raise APIError(409, "command_conflict")
            return _bounded_receipt(json.loads(row[1])) if row else None

    def create(self, cid, digest, operation, target):
        with self.lock:
            now = self.clock()
            mono = self.monotonic()
            with self.db:
                expired = [cid for cid, born in self.ages.items()
                           if mono - born > max(60, self.retention)]
                self.db.executemany("DELETE FROM receipts WHERE id=?", ((cid,) for cid in expired))
                if self.db.execute("SELECT count(*) FROM receipts").fetchone()[0] >= self.max_rows:
                    raise APIError(429, "receipt_store_full")
                row = {"command_id": cid, "operation": operation, "target": target,
                       "status": "accepted", "created_at": now, "updated_at": now,
                       "confirmation": "none", "physical_confirmation": False}
                self.db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (cid, digest, now, encoded(row).decode()))
            for old in expired:
                self.ages.pop(old, None)
            self.ages[cid] = mono
            return row

    def update(self, cid, status, **fields):
        with self.lock:
            row = self.get(cid)
            if row is None:
                raise APIError(404, "receipt_not_found")
            row.update(status=status, updated_at=self.clock(), **fields)
            return self._save(cid, row)
