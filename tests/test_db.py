from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_core.app import create_app
from agent_core.db import Store
from agent_core.github import FakeGitHub


class _FailingReplicaConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def execute(self, sql: str, params=()):
        if sql.startswith("INSERT INTO row_replica"):
            raise sqlite3.OperationalError("forced replica failure")
        return self.connection.execute(sql, params)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


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


def test_record_web_event_writes_ledger_and_replica_with_per_origin_sequences(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "events.sqlite"))
    try:
        store.record_web_event(
            "web:alice",
            "alice",
            "activity",
            "insert",
            "one",
            '{"id":"one"}',
            "2026-11-05T10:00:00Z",
        )
        store.record_web_event(
            "web:alice",
            "alice",
            "activity",
            "update",
            "two",
            '{"id":"two"}',
            "2026-11-05T10:01:00Z",
        )
        store.record_web_event(
            "web:bob",
            "bob",
            "activity",
            "insert",
            "three",
            '{"id":"three"}',
            "2026-11-05T10:02:00Z",
        )

        ledger = store.query(
            "SELECT origin_device_id, origin_seq, row_id FROM ledger_event ORDER BY hub_seq"
        )
        replicas = store.query(
            "SELECT origin_device_id, github_login, row_id FROM row_replica ORDER BY row_id"
        )

        assert [
            (row["origin_device_id"], row["origin_seq"], row["row_id"])
            for row in ledger
        ] == [
            ("web:alice", 1, "one"),
            ("web:alice", 2, "two"),
            ("web:bob", 1, "three"),
        ]
        assert [
            (row["origin_device_id"], row["github_login"], row["row_id"])
            for row in replicas
        ] == [
            ("web:alice", "alice", "one"),
            ("web:bob", "bob", "three"),
            ("web:alice", "alice", "two"),
        ]
    finally:
        store.close()


def test_record_web_event_rolls_back_ledger_when_replica_write_fails(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "rollback.sqlite"))
    store._conn = _FailingReplicaConnection(store._conn)
    try:
        with pytest.raises(sqlite3.OperationalError, match="forced replica failure"):
            store.record_web_event(
                "web:alice",
                "alice",
                "activity",
                "insert",
                "one",
                '{"id":"one"}',
                "2026-11-05T10:00:00Z",
            )

        assert store.query("SELECT * FROM ledger_event") == []
        assert store.query("SELECT * FROM row_replica") == []
    finally:
        store.close()


def test_create_app_closes_internally_created_store_after_lifespan(cfg) -> None:
    app = create_app(cfg, github=FakeGitHub({"code-alice": "alice"}))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    with pytest.raises(sqlite3.ProgrammingError):
        app.state.hub.store.query("SELECT 1")


def test_create_app_closes_internal_store_when_hub_construction_fails(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[Store] = []
    original_close = Store.close

    def close_spy(store: Store) -> None:
        closed.append(store)
        original_close(store)

    def fail_loading_tokens(_store: Store):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(Store, "close", close_spy)
    monkeypatch.setattr(Store, "all_oauth_tokens", fail_loading_tokens)

    with pytest.raises(RuntimeError, match="construction failed"):
        create_app(cfg, github=FakeGitHub({"code-alice": "alice"}))

    assert len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        closed[0].query("SELECT 1")
