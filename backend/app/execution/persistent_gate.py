"""Atomic write reservations that survive process restarts.

No transport or platform operation lives here. Offline and live keys are isolated
by namespace, and only hashes of logical targets are stored.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.execution.write_safety import WriteResult, WriteStatus


class PersistentWriteSafetyGate:
    def __init__(self, db_path: str | Path, *, namespace: str = "offline") -> None:
        if namespace not in {"offline", "live"}:
            raise ValueError("namespace must be offline or live")
        self.db_path = Path(db_path)
        self.namespace = namespace
        self._claims: dict[str, str] = {}
        with self._connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS write_reservations (
                namespace TEXT NOT NULL, target_hash TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL, reservation_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(namespace, target_hash))""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(write_reservations)")}
            if "reservation_id" not in columns:
                con.execute("ALTER TABLE write_reservations ADD COLUMN reservation_id TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(str(self.db_path), timeout=15)
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _key(self, key: str) -> tuple[str, str]:
        return self.namespace, hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _decision(row) -> tuple[bool, str]:
        if row and row[0] == WriteStatus.SENT_OK.value:
            return False, "already_sent"
        if row and row[0] == WriteStatus.UNCERTAIN.value:
            return False, "uncertain_pending_manual"
        return True, "allowed"

    def can_attempt(self, key: str) -> tuple[bool, str]:
        with self._connect() as con:
            row = con.execute("SELECT status FROM write_reservations "
                              "WHERE namespace=? AND target_hash=?", self._key(key)).fetchone()
        return self._decision(row)

    def reserve(self, key: str, *, allow_sent: bool = False) -> tuple[bool, str]:
        with self._connect() as con:
            # Serialize check + reservation even when two independent processes race.
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status FROM write_reservations "
                              "WHERE namespace=? AND target_hash=?", self._key(key)).fetchone()
            decision = self._decision(row)
            # Shared video locks may accept a previous confirmed comment from
            # another account. Pending/unknown outcomes always remain blocked.
            if allow_sent and row and row[0] == WriteStatus.SENT_OK.value:
                decision = (True, "allowed")
            if decision[0]:
                reservation = uuid.uuid4().hex
                con.execute("INSERT INTO write_reservations "
                            "(namespace,target_hash,status,reason,updated_at,reservation_id) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(namespace,target_hash) DO UPDATE SET "
                            "status=excluded.status, reason=excluded.reason, updated_at=excluded.updated_at, "
                            "reservation_id=excluded.reservation_id",
                            (*self._key(key), WriteStatus.UNCERTAIN.value, "reserved", int(time.time()), reservation))
                self._claims[key] = reservation
        return decision

    def reservation_token(self, key: str) -> str:
        return self._claims.get(key, "")

    def record(self, key: str, result: WriteResult, *, token: str | None = None) -> WriteResult:
        token = self.reservation_token(key) if token is None else token
        if not token:
            raise RuntimeError("write result has no owned reservation")
        with self._connect() as con:
            cur = con.execute("UPDATE write_reservations SET status=?, reason=?, updated_at=? "
                              "WHERE namespace=? AND target_hash=? AND status=? AND reservation_id=?",
                              (result.status.value, result.reason, int(time.time()),
                               *self._key(key), WriteStatus.UNCERTAIN.value, token))
            if cur.rowcount != 1:
                raise RuntimeError("write result has no pending reservation")
        return result

    def state(self, key: str) -> str:
        with self._connect() as con:
            row = con.execute("SELECT status FROM write_reservations "
                              "WHERE namespace=? AND target_hash=?", self._key(key)).fetchone()
        return row[0] if row else "none"

    def confirm_manual(self, key: str, *, sent: bool) -> None:
        with self._connect() as con:
            if sent:
                con.execute("UPDATE write_reservations SET status=?, reason='manual_confirmed', "
                            "updated_at=? WHERE namespace=? AND target_hash=? AND status=?",
                            (WriteStatus.SENT_OK.value, int(time.time()), *self._key(key),
                             WriteStatus.UNCERTAIN.value))
            else:
                con.execute("DELETE FROM write_reservations WHERE namespace=? AND target_hash=? "
                            "AND status=?", (*self._key(key), WriteStatus.UNCERTAIN.value))
