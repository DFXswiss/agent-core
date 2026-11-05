"""SQLite store for devices, pairing, events and materialized rows."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
  id TEXT PRIMARY KEY,
  github_login TEXT NOT NULL,
  name TEXT NOT NULL,
  token_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  confirmed_by TEXT
);

CREATE TABLE IF NOT EXISTS pending_pair (
  challenge TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  device_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  token TEXT,
  github_login TEXT
);

CREATE TABLE IF NOT EXISTS ledger_event (
  hub_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  origin_device_id TEXT NOT NULL,
  origin_seq INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  op TEXT NOT NULL,
  row_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  UNIQUE (origin_device_id, origin_seq)
);

CREATE TABLE IF NOT EXISTS row_replica (
  table_name TEXT NOT NULL,
  row_id TEXT NOT NULL,
  origin_device_id TEXT NOT NULL,
  github_login TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (table_name, row_id)
);

CREATE TABLE IF NOT EXISTS device_subscription (
  device_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  match_json TEXT NOT NULL,
  PRIMARY KEY (device_id, seq),
  FOREIGN KEY (device_id) REFERENCES device(id)
);

CREATE TABLE IF NOT EXISTS oauth_token (
  github_login TEXT PRIMARY KEY,
  token TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_origin_idx ON ledger_event (origin_device_id, origin_seq);
CREATE INDEX IF NOT EXISTS replica_login_idx ON row_replica (github_login, table_name);
CREATE INDEX IF NOT EXISTS device_login_idx ON device (github_login);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
        except BaseException:
            self._conn.close()
            raise

    def _migrate(self) -> None:
        device_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(device)")}
        if "confirmed_by" not in device_columns:
            self._conn.execute("ALTER TABLE device ADD COLUMN confirmed_by TEXT")
        self._migrate_request_reservation()

    def _migrate_request_reservation(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS request_reservation ("
            "actor TEXT, activity_type TEXT, request_id TEXT, status TEXT NOT NULL, "
            "lease_until TEXT NOT NULL, PRIMARY KEY (actor, activity_type, request_id))"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return list(cur.fetchall())

    def claim_pair_token(self, challenge: str, device_id: str, cutoff: str) -> tuple[str, str] | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "SELECT token, github_login FROM pending_pair "
                "WHERE challenge = ? AND device_id = ? AND token IS NOT NULL AND created_at >= ?",
                (challenge, device_id, cutoff),
            )
            row = cur.fetchone()
            if row is None or row["token"] is None or row["github_login"] is None:
                self._conn.commit()
                return None
            updated = self._conn.execute(
                "UPDATE pending_pair SET token = NULL WHERE challenge = ? AND device_id = ? AND token = ?",
                (challenge, device_id, row["token"]),
            )
            if updated.rowcount != 1:
                self._conn.rollback()
                return None
            self._conn.commit()
            return (row["token"], row["github_login"])

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError(f"expected 0 or 1 row, got {len(rows)}")
        return rows[0]

    def replace_device_subscriptions(self, device_id: str, match_jsons: list[str]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM device_subscription WHERE device_id = ?", (device_id,))
                for seq, match_json in enumerate(match_jsons):
                    self._conn.execute(
                        "INSERT INTO device_subscription (device_id, seq, match_json) VALUES (?, ?, ?)",
                        (device_id, seq, match_json),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def reserve_request(
        self,
        actor: str,
        activity_type: str,
        request_id: str,
        now: str,
        lease_until: str,
    ) -> str:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status, lease_until FROM request_reservation "
                    "WHERE actor = ? AND activity_type = ? AND request_id = ?",
                    (actor, activity_type, request_id),
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO request_reservation "
                        "(actor, activity_type, request_id, status, lease_until) "
                        "VALUES (?, ?, ?, 'pending', ?)",
                        (actor, activity_type, request_id, lease_until),
                    )
                    result = "reserved"
                elif row["status"] == "done":
                    result = "done"
                elif row["lease_until"] > now:
                    result = "busy"
                else:
                    self._conn.execute(
                        "UPDATE request_reservation SET lease_until = ? "
                        "WHERE actor = ? AND activity_type = ? AND request_id = ?",
                        (lease_until, actor, activity_type, request_id),
                    )
                    result = "reserved"
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def finish_request(self, actor: str, activity_type: str, request_id: str) -> None:
        self.execute(
            "UPDATE request_reservation SET status = 'done' "
            "WHERE actor = ? AND activity_type = ? AND request_id = ?",
            (actor, activity_type, request_id),
        )

    def release_request(self, actor: str, activity_type: str, request_id: str) -> None:
        self.execute(
            "DELETE FROM request_reservation WHERE actor = ? AND activity_type = ? "
            "AND request_id = ? AND status = 'pending'",
            (actor, activity_type, request_id),
        )

    def record_web_event(
        self,
        origin: str,
        login: str,
        table: str,
        op: str,
        row_id: str,
        encoded: str,
        occurred: str,
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                last = self._conn.execute(
                    "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event "
                    "WHERE origin_device_id = ?",
                    (origin,),
                ).fetchone()
                seq = int(last["m"]) + 1 if last is not None else 1
                self._conn.execute(
                    "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (origin, seq, table, op, row_id, encoded, occurred, occurred),
                )
                if op == "delete":
                    self._conn.execute(
                        "DELETE FROM row_replica WHERE table_name = ? AND row_id = ?",
                        (table, row_id),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(table_name, row_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                        (table, row_id, origin, login, encoded, occurred),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def put_oauth_token(self, github_login: str, token: str) -> None:
        self.execute(
            "INSERT INTO oauth_token (github_login, token, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(github_login) DO UPDATE SET token = excluded.token, updated_at = excluded.updated_at",
            (github_login, token, utcnow()),
        )

    def get_oauth_token(self, github_login: str) -> str:
        row = self.query_one("SELECT token FROM oauth_token WHERE github_login = ?", (github_login,))
        if row is None:
            return ""
        token = row["token"]
        return token if isinstance(token, str) else ""

    def delete_oauth_token(self, github_login: str, token: str | None = None) -> None:
        if token is None:
            self.execute("DELETE FROM oauth_token WHERE github_login = ?", (github_login,))
            return
        self.execute(
            "DELETE FROM oauth_token WHERE github_login = ? AND token = ?",
            (github_login, token),
        )

    def all_oauth_tokens(self) -> list[tuple[str, str]]:
        rows = self.query("SELECT github_login, token FROM oauth_token")
        out: list[tuple[str, str]] = []
        for row in rows:
            login = row["github_login"]
            token = row["token"]
            if isinstance(login, str) and isinstance(token, str) and login and token:
                out.append((login, token))
        return out


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def loads(raw: str) -> Any:
    return json.loads(raw)
