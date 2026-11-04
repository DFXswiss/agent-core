from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.github import FakeGitHub
from tests.conftest import pair_device, sign_in
from tests.test_agent_pair_control import _pair_as


ASSIGNMENT = {
    "agent": "helperbot",
    "repo": "Acme/Widget",
    "title": "Investigate the failure",
    "body": "Find the root cause and open a draft pull request.",
}


def test_owner_creates_assignment_and_web_activity(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert response.status_code == 201, response.text
    assert response.json() == {
        "repo": "Acme/Widget",
        "number": 1,
        "url": "https://github.test/Acme/Widget/issues/1",
        "agent": "helperbot",
        "actor": "alice",
    }
    github: FakeGitHub = agent_hub.app.state.hub.github
    created = next(call for call in github.issue_calls if call["operation"] == "create_issue")
    assert created["token"] == "tok-alice"
    assert created["assignees"] == ["helperbot"]
    assert created["body"].endswith("Assigned through http://testserver by @alice.")
    state = agent_hub.get("/api/state").json()
    activity = next(row for row in state["activity"] if row.get("type") == "assignment.created")
    assert activity["payload"]["agent"] == "helperbot"
    assert activity["payload"]["actor"] == "alice"
    replica = agent_hub.app.state.hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'activity' AND row_id = ?",
        (activity["id"],),
    )
    assert replica["origin_device_id"] == "web:alice"


def test_assignment_authorization_and_missing_token(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-bob")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 403

    agent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac01"
    agent_token = _pair_as(agent_hub, "code-alice", agent_id, "helperbot")
    denied = agent_hub.post(
        "/api/assignments",
        headers={"Authorization": f"Bearer {agent_token}"},
        json=ASSIGNMENT,
    )
    assert denied.status_code == 403

    owner_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac02"
    owner_token = pair_device(agent_hub, "code-alice", owner_id)
    hub = agent_hub.app.state.hub
    hub.github_tokens.pop("alice", None)
    hub.store.delete_oauth_token("alice")
    missing = agent_hub.post(
        "/api/assignments",
        headers={"Authorization": f"Bearer {owner_token}"},
        json=ASSIGNMENT,
    )
    assert missing.status_code == 409
    assert "sign in on the website once" in missing.json()["detail"]


def test_assignment_retries_missing_assignee_then_returns_issue_url(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.omit_assignees_on_create = True
    github.omit_assignees_after_add = True
    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert response.status_code == 502
    assert response.json()["url"] == "https://github.test/Acme/Widget/issues/1"
    assert [call["operation"] for call in github.issue_calls].count("add_assignees") == 1


def test_assignment_list_comments_and_reply_authorization(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    created = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert created.status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.create_issue_comment(
        "tok-helperbot", "Acme/Widget", 1, "Which failure signature should I prioritize?"
    )
    listed = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    assert listed.status_code == 200
    row = listed.json()["assignments"][0]
    assert row["state"] == "open"
    assert row["assignees"] == ["helperbot"]
    assert row["comments"][0]["author"] == "helperbot"
    assert "prioritize" in row["comments"][0]["body"]

    replied = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "Prioritize the first reproducible 500 response."},
    )
    assert replied.status_code == 201
    assert "issuecomment" in replied.json()["url"]
    reply_call = [call for call in github.issue_calls if call["operation"] == "create_issue_comment"][-1]
    assert reply_call["token"] == "tok-alice"
    state = agent_hub.get("/api/state").json()
    assert any(row.get("type") == "assignment.reply" for row in state["activity"])

    sign_in(agent_hub, "code-bob")
    denied = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": "I do not own this agent."}
    )
    assert denied.status_code == 403
