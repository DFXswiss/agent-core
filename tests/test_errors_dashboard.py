from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import sign_in
from tests.test_newest_first import _insert_replica


def test_dashboard_html_has_errors_table(hub: TestClient) -> None:
    r = hub.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="errors"' in body
    assert 'id="k-errors"' in body
    assert "fillErrors(data.activity" in body
    assert "Open errors" in body
    assert "<h2>Errors</h2>" in body
    assert ".reverse(" not in body
    assert 'row.type !== "error.seen"' in body
    assert "seen.has(fingerprint)" in body
    assert "function buildConclusionMap(activities)" in body
    assert 'row.type !== "error.fix" && row.type !== "error.skip"' in body
    assert "!map.has(payload.error_id)" in body


def test_api_state_error_activity_newest_first(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    _insert_replica(
        hub,
        table="activity",
        row_id="error-seen-1",
        updated_at="2026-08-21T10:00:00Z",
        payload={
            "id": "error-seen-1",
            "type": "error.seen",
            "payload": {
                "fingerprint": "fp-abc",
                "service": "api",
                "environment": "prod",
                "class": "TimeoutError",
                "repo": "acme/app",
                "count": 3,
                "first_seen": "2026-08-21T09:00:00Z",
                "last_seen": "2026-08-21T10:00:00Z",
                "excerpt": "timed out waiting",
                "evidence": "log://example",
                "line_fingerprint": "line-fp-1",
            },
        },
    )
    _insert_replica(
        hub,
        table="activity",
        row_id="error-fix-1",
        updated_at="2026-08-22T21:00:00Z",
        payload={
            "id": "error-fix-1",
            "type": "error.fix",
            "payload": {
                "error_id": "error-seen-1",
                "fingerprint": "fp-abc",
                "execution_status": "pr_opened",
            },
        },
    )
    state = hub.get("/api/state").json()
    errors = [
        row
        for row in state["activity"]
        if row.get("type") in ("error.seen", "error.fix", "error.skip")
    ]
    assert [row["id"] for row in errors] == ["error-fix-1", "error-seen-1"]
    seen = next(row for row in errors if row["type"] == "error.seen")
    fix = next(row for row in errors if row["type"] == "error.fix")
    assert seen["payload"]["fingerprint"] == "fp-abc"
    assert seen["payload"]["service"] == "api"
    assert seen["payload"]["class"] == "TimeoutError"
    assert seen["payload"]["repo"] == "acme/app"
    assert seen["payload"]["count"] == 3
    assert "last_seen" in seen["payload"]
    assert fix["payload"]["error_id"] == "error-seen-1"
    assert fix["payload"]["fingerprint"] == "fp-abc"
    assert fix["payload"]["execution_status"] == "pr_opened"
    assert seen["_github_login"] == "alice"
    assert fix["_github_login"] == "alice"


def test_api_state_error_seen_rows_newest_first(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    _insert_replica(
        hub,
        table="activity",
        row_id="error-seen-old",
        updated_at="2026-08-21T10:00:00Z",
        payload={
            "id": "error-seen-old",
            "type": "error.seen",
            "payload": {
                "fingerprint": "fp-old",
                "service": "api",
                "environment": "prod",
                "class": "TimeoutError",
                "repo": "acme/app",
                "count": 2,
                "first_seen": "2026-08-21T09:00:00Z",
                "last_seen": "2026-08-21T10:00:00Z",
                "excerpt": "timed out waiting",
                "evidence": "log://example-old",
                "line_fingerprint": "line-fp-old",
            },
        },
    )
    _insert_replica(
        hub,
        table="activity",
        row_id="error-seen-new",
        updated_at="2026-08-22T21:00:00Z",
        payload={
            "id": "error-seen-new",
            "type": "error.seen",
            "payload": {
                "fingerprint": "fp-new",
                "service": "api",
                "environment": "prod",
                "class": "TimeoutError",
                "repo": "acme/app",
                "count": 5,
                "first_seen": "2026-08-22T20:00:00Z",
                "last_seen": "2026-08-22T21:00:00Z",
                "excerpt": "timed out waiting again",
                "evidence": "log://example-new",
                "line_fingerprint": "line-fp-new",
            },
        },
    )
    state = hub.get("/api/state").json()
    seen = [row for row in state["activity"] if row.get("type") == "error.seen"]
    assert [row["id"] for row in seen] == ["error-seen-new", "error-seen-old"]
    for row in seen:
        inner = row["payload"]
        assert inner["service"] == "api"
        assert inner["environment"] == "prod"
        assert inner["class"] == "TimeoutError"
        assert inner["repo"] == "acme/app"
        assert "count" in inner
        assert "first_seen" in inner
        assert "last_seen" in inner
        assert "excerpt" in inner
        assert "evidence" in inner
        assert "line_fingerprint" in inner
    assert seen[0]["payload"]["fingerprint"] == "fp-new"
    assert seen[1]["payload"]["fingerprint"] == "fp-old"
    assert seen[0]["payload"]["count"] == 5
    assert seen[1]["payload"]["count"] == 2
    assert seen[0]["_github_login"] == "alice"
    assert seen[1]["_github_login"] == "alice"


def test_api_state_keeps_duplicate_fingerprint_error_seen_rows(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    _insert_replica(
        hub,
        table="activity",
        row_id="error-seen-dup-a",
        updated_at="2026-08-21T11:00:00Z",
        payload={
            "id": "error-seen-dup-a",
            "type": "error.seen",
            "payload": {
                "fingerprint": "fp-dup",
                "service": "api",
                "environment": "prod",
                "class": "TimeoutError",
                "repo": "acme/app",
                "count": 2,
                "first_seen": "2026-08-21T10:00:00Z",
                "last_seen": "2026-08-21T11:00:00Z",
                "excerpt": "timed out waiting",
                "evidence": "log://example-a",
                "line_fingerprint": "line-fp-dup-a",
            },
        },
    )
    _insert_replica(
        hub,
        table="activity",
        row_id="error-seen-dup-b",
        updated_at="2026-08-21T12:00:00Z",
        payload={
            "id": "error-seen-dup-b",
            "type": "error.seen",
            "payload": {
                "fingerprint": "fp-dup",
                "service": "api",
                "environment": "prod",
                "class": "TimeoutError",
                "repo": "acme/app",
                "count": 5,
                "first_seen": "2026-08-21T10:30:00Z",
                "last_seen": "2026-08-21T12:00:00Z",
                "excerpt": "timed out waiting again",
                "evidence": "log://example-b",
                "line_fingerprint": "line-fp-dup-b",
            },
        },
    )
    state = hub.get("/api/state").json()
    seen_ids = {
        row["id"]
        for row in state["activity"]
        if row.get("type") == "error.seen"
    }
    assert "error-seen-dup-a" in seen_ids
    assert "error-seen-dup-b" in seen_ids


def test_api_state_preserves_conclusion_error_id_matching_shape(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    _insert_replica(
        hub,
        table="activity",
        row_id="error-seen-2",
        updated_at="2026-08-21T13:00:00Z",
        payload={
            "id": "error-seen-2",
            "type": "error.seen",
            "payload": {
                "fingerprint": "fp-match",
                "service": "worker",
                "environment": "prod",
                "class": "ValueError",
                "repo": "acme/app",
                "count": 1,
                "first_seen": "2026-08-21T12:00:00Z",
                "last_seen": "2026-08-21T13:00:00Z",
                "excerpt": "invalid value",
                "evidence": "log://example-2",
                "line_fingerprint": "line-fp-2",
            },
        },
    )
    _insert_replica(
        hub,
        table="activity",
        row_id="error-fix-2",
        updated_at="2026-08-22T14:00:00Z",
        payload={
            "id": "error-fix-2",
            "type": "error.fix",
            "payload": {
                "error_id": "error-seen-2",
                "fingerprint": "fp-match",
                "execution_status": "pr_opened",
            },
        },
    )
    _insert_replica(
        hub,
        table="activity",
        row_id="error-skip-other",
        updated_at="2026-08-22T15:00:00Z",
        payload={
            "id": "error-skip-other",
            "type": "error.skip",
            "payload": {
                "error_id": "error-seen-other",
                "fingerprint": "fp-other",
                "reason": "unmapped-repo",
            },
        },
    )
    state = hub.get("/api/state").json()
    fix = next(row for row in state["activity"] if row["id"] == "error-fix-2")
    skip = next(row for row in state["activity"] if row["id"] == "error-skip-other")
    assert fix["type"] == "error.fix"
    assert fix["payload"]["error_id"] == "error-seen-2"
    assert skip["type"] == "error.skip"
    assert skip["payload"]["error_id"] == "error-seen-other"
    assert skip["payload"]["reason"] == "unmapped-repo"
