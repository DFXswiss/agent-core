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
    assert "function conclusionFor(activities, errorSeenId)" in body
    assert 'row.type !== "error.fix" && row.type !== "error.skip"' in body
    assert "payload.error_id === errorSeenId" in body


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
                "repo": "DFXswiss/backend",
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
    assert seen["payload"]["repo"] == "DFXswiss/backend"
    assert seen["payload"]["count"] == 3
    assert "last_seen" in seen["payload"]
    assert fix["payload"]["error_id"] == "error-seen-1"
    assert fix["payload"]["fingerprint"] == "fp-abc"
    assert fix["payload"]["execution_status"] == "pr_opened"
    assert seen["_github_login"] == "alice"
    assert fix["_github_login"] == "alice"
