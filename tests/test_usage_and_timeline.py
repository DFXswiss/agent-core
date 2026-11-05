from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.db import dumps
from tests.conftest import sign_in
from tests.test_newest_first import _insert_replica


def test_usage_aggregates_lanes_openrouter_and_keeps_grok_shape(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="lane-new",
        updated_at="2026-11-04T11:00:00Z",
        login="helperbot",
        payload={
            "id": "lane-new",
            "type": "lane.run",
            "session_id": "session-usage",
            "ran_at": "2026-11-04T11:00:00Z",
            "payload": {
                "vendor": "anthropic",
                "model": "model-a",
                "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3},
            },
        },
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="lane-old",
        updated_at="2026-11-04T10:00:00Z",
        login="helperbot",
        payload={
            "id": "lane-old",
            "type": "lane.run",
            "session_id": "session-usage",
            "ran_at": "2026-11-04T10:00:00Z",
            "payload": {"vendor": "anthropic", "model": "model-a", "usage": None},
        },
    )
    for row_id, fetched, used in (
        ("openrouter-new", "2026-11-04T12:00:00Z", 9.5),
        ("openrouter-old", "2026-11-03T12:00:00Z", 4.0),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=row_id,
            updated_at=fetched,
            login="helperbot",
            payload={
                "id": row_id,
                "type": "usage.snapshot",
                "payload": {
                    "vendor": "openrouter",
                    "account": "team-account",
                    "usage_usd": used,
                    "usage_daily_usd": 1.0,
                    "usage_weekly_usd": 5.0,
                    "usage_monthly_usd": used,
                    "limit_usd": 100.0,
                    "limit_remaining_usd": 90.5,
                    "fetched_at": fetched,
                },
            },
        )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="grok-snapshot",
        updated_at="2026-11-04T09:00:00Z",
        login="helperbot",
        payload={
            "id": "grok-snapshot",
            "type": "usage.snapshot",
            "payload": {"provider": "grok", "account_email": "agent@example.test", "used_percent": 12},
        },
    )

    response = agent_hub.get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["lanes"] == [
        {
            "login": "helperbot",
            "session_id": "session-usage",
            "provider": "anthropic",
            "model": "model-a",
            "runs": 2,
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 0,
            "last_at": "2026-11-04T11:00:00Z",
        }
    ]
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["usage_usd"] == 9.5
    assert body["grok"][0]["id"] == "grok-snapshot"
    assert body["grok"][0]["payload"]["used_percent"] == 12
    assert body["grok"][0]["_github_login"] == "helperbot"


def test_usage_arrays_are_newest_first_with_row_id_tiebreak(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    for suffix, updated_at in (
        ("old", "2026-11-04T08:00:00Z"),
        ("tie-a", "2026-11-04T09:00:00Z"),
        ("tie-b", "2026-11-04T09:00:00Z"),
    ):
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"lane-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"lane-{suffix}",
                "type": "lane.run",
                "session_id": f"session-{suffix}",
                "payload": {"vendor": "anthropic", "model": "model-a"},
            },
        )
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"openrouter-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"openrouter-{suffix}",
                "type": "usage.snapshot",
                "payload": {"vendor": "openrouter", "account": f"account-{suffix}"},
            },
        )
        _insert_replica(
            agent_hub,
            table="activity",
            row_id=f"grok-{suffix}",
            updated_at=updated_at,
            login="helperbot",
            payload={
                "id": f"grok-{suffix}",
                "type": "usage.snapshot",
                "payload": {"vendor": "grok", "account_email": f"{suffix}@example.test"},
            },
        )

    response = agent_hub.get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    assert [row["session_id"] for row in body["lanes"]] == [
        "session-tie-b",
        "session-tie-a",
        "session-old",
    ]
    assert [row["account"] for row in body["accounts"]] == [
        "account-tie-b",
        "account-tie-a",
        "account-old",
    ]
    assert [row["id"] for row in body["grok"]] == [
        "grok-tie-b",
        "grok-tie-a",
        "grok-old",
    ]


def test_timeline_order_relations_and_visibility(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    _insert_replica(
        agent_hub,
        table="session",
        row_id="timeline-session",
        updated_at="2026-11-04T10:00:00Z",
        login="helperbot",
        payload={"id": "timeline-session", "created_at": "2026-11-04T10:00:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="timeline-lane",
        updated_at="2026-11-04T10:01:00Z",
        login="helperbot",
        payload={"id": "timeline-lane", "type": "lane.run", "session_id": "timeline-session", "ran_at": "2026-11-04T10:01:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="task",
        row_id="timeline-task",
        updated_at="2026-11-04T10:02:00Z",
        login="helperbot",
        payload={"id": "", "session_id": "timeline-session", "started_at": "2026-11-04T10:02:00Z", "state": "working"},
    )
    _insert_replica(
        agent_hub,
        table="local_check",
        row_id="timeline-check",
        updated_at="2026-11-04T10:03:00Z",
        login="helperbot",
        payload={"id": "timeline-check", "task_id": "timeline-task", "ran_at": "2026-11-04T10:03:00Z", "result": "passed"},
    )
    _insert_replica(
        agent_hub,
        table="activity",
        row_id="empty-task-relation",
        updated_at="2026-11-04T10:04:00Z",
        login="helperbot",
        payload={"id": "empty-task-relation", "task_id": "", "created_at": "2026-11-04T10:04:00Z"},
    )
    _insert_replica(
        agent_hub,
        table="local_check",
        row_id="non-string-task-relation",
        updated_at="2026-11-04T10:05:00Z",
        login="helperbot",
        payload={"id": "non-string-task-relation", "task_id": 7, "ran_at": "2026-11-04T10:05:00Z"},
    )
    response = agent_hub.get("/api/sessions/timeline-session/timeline")
    assert response.status_code == 200
    body = response.json()
    assert [row["table"] for row in body["timeline"]] == ["session", "activity", "task", "local_check"]
    assert [row["at"] for row in body["timeline"]] == sorted(row["at"] for row in body["timeline"])
    assert body["truncated"] is False
    assert agent_hub.get("/sessions/timeline-session").status_code == 200

    sign_in(agent_hub, "code-dave")
    assert agent_hub.get("/api/sessions/timeline-session/timeline").status_code == 404
    assert agent_hub.get("/sessions/timeline-session").status_code == 404


def test_timeline_truncates_at_5000(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    store = agent_hub.app.state.hub.store
    store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "session",
            "long-session",
            "origin-long",
            "helperbot",
            dumps({"id": "long-session", "created_at": "2026-01-01T00:00:00Z"}),
            "2026-01-01T00:00:00Z",
        ),
    )
    for index in range(5001):
        stamp = f"2026-01-02T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z"
        store.execute(
            "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "activity",
                f"long-activity-{index:04d}",
                "origin-long",
                "helperbot",
                dumps({"id": f"long-activity-{index:04d}", "session_id": "long-session", "created_at": stamp}),
                stamp,
            ),
        )
    body = agent_hub.get("/api/sessions/long-session/timeline").json()
    assert body["truncated"] is True
    assert len(body["timeline"]) == 5000
    assert body["timeline"][0]["row_id"] == "long-activity-0001"
    assert body["timeline"][-1]["row_id"] == "long-activity-5000"
    assert all(row["table"] != "session" for row in body["timeline"])
