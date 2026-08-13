from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import pair_device, sign_in


def test_me_requires_login(hub: TestClient) -> None:
    assert hub.get("/auth/me").status_code == 401


def test_github_login_and_visibility(hub: TestClient) -> None:
    sign_in(hub, "code-alice")
    me = hub.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["login"] == "alice"
    assert body["visible"] == ["alice", "bob"]
    assert body["teams"] == ["dfx"]


def test_dave_sees_only_self(hub: TestClient) -> None:
    sign_in(hub, "code-dave")
    body = hub.get("/auth/me").json()
    assert body["visible"] == ["dave"]
    assert body["teams"] == []


def test_pair_issues_token(hub: TestClient) -> None:
    token = pair_device(hub, "code-alice", "11111111-1111-1111-1111-111111111111")
    assert "." in token
    pushed = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": []},
    )
    assert pushed.status_code == 200
    assert pushed.json()["accepted"] == 0


def test_unknown_code_rejected(hub: TestClient) -> None:
    start = hub.get("/auth/github", follow_redirects=False)
    state = start.headers["location"].split("state=")[1].split("&")[0]
    bad = hub.get("/auth/github/callback", params={"code": "nope", "state": state})
    assert bad.status_code == 401


def test_expired_state_rejected(hub: TestClient) -> None:
    bad = hub.get("/auth/github/callback", params={"code": "code-alice", "state": "wrong"})
    assert bad.status_code == 400
