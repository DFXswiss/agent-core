from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_core.db import Store


def test_store_migrates_device_confirmed_by_without_losing_rows(tmp_path: Path) -> None:
    database = tmp_path / "old-hub.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE device ("
            "id TEXT PRIMARY KEY, github_login TEXT NOT NULL, name TEXT NOT NULL, "
            "token_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, revoked_at TEXT)"
        )
        connection.execute(
            "INSERT INTO device (id, github_login, name, token_id, created_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            ("old-device", "alice", "old laptop", "old-token", "2026-01-01T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()

    store = Store(str(database))
    try:
        columns = {row["name"] for row in store.query("PRAGMA table_info(device)")}
        row = store.query_one("SELECT * FROM device WHERE id = ?", ("old-device",))

        assert "confirmed_by" in columns
        assert row is not None
        assert row["github_login"] == "alice"
        assert row["confirmed_by"] is None
    finally:
        store.close()
