from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import agent_core.app as app_module
from agent_core.github import FakeGitHub, RealGitHub
from tests.conftest import pair_device, sign_in
from tests.test_agent_pair_control import _pair_as


ASSIGNMENT = {
    "agent": "helperbot",
    "repo": "Acme/Widget",
    "title": "Investigate the failure",
    "body": "Find the root cause and open a draft pull request.",
}


class _Response:
    def __init__(self, status_code: int, body: object, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> object:
        return self._body


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


def test_assignment_stores_github_canonical_repo_case(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.canonical_repos["acme/widget"] = "Acme/Widget"
    assignment = dict(ASSIGNMENT, repo="acme/widget")

    response = agent_hub.post("/api/assignments", json=assignment)

    assert response.status_code == 201, response.text
    assert response.json()["repo"] == "Acme/Widget"
    created = next(call for call in github.issue_calls if call["operation"] == "create_issue")
    assert created["repo"] == "acme/widget"
    state = agent_hub.get("/api/state").json()
    activity = next(row for row in state["activity"] if row.get("type") == "assignment.created")
    assert activity["payload"]["repo"] == "Acme/Widget"


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
    assert response.json()["number"] == 1
    assert [call["operation"] for call in github.issue_calls] == [
        "create_issue",
        "issue",
        "add_assignees",
        "issue",
    ]


def test_assignment_accepts_201_add_and_verify_get_decides(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    hub.github = RealGitHub(hub.cfg)
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            _Response(
                201,
                {
                    "number": 1,
                    "html_url": "https://github.test/Acme/Widget/issues/1",
                    "repository_url": "https://api.github.test/repos/Acme/Widget",
                    "assignees": [],
                },
            ),
            _Response(200, {"number": 1, "assignees": []}),
            _Response(201, {"number": 1, "assignees": [{"login": "helperbot"}]}),
            _Response(200, {"number": 1, "assignees": []}),
        ]
    )

    def request(method: str, url: str, **_kwargs: object) -> _Response:
        calls.append((method, url))
        return next(responses)

    monkeypatch.setattr(httpx, "request", request)
    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert response.status_code == 502
    assert response.json()["url"] == "https://github.test/Acme/Widget/issues/1"
    assert response.json()["number"] == 1
    assert [method for method, _url in calls] == ["POST", "GET", "POST", "GET"]
    assert calls[2][1].endswith("/repos/Acme/Widget/issues/1/assignees")


def test_assignment_transport_errors_are_bounded_and_preserve_created_issue(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    hub.github = RealGitHub(hub.cfg)

    def fail_create(*_args: object, **_kwargs: object) -> _Response:
        raise httpx.ConnectError("transport included tok-alice and a sensitive query")

    monkeypatch.setattr(httpx, "request", fail_create)
    failed_create = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert failed_create.status_code == 502
    assert failed_create.json() == {"detail": "GitHub request failed: ConnectError"}
    assert "tok-alice" not in failed_create.text

    calls = 0

    def fail_after_create(*_args: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(
                201,
                {
                    "number": 2,
                    "html_url": "https://github.test/Acme/Widget/issues/2",
                    "repository_url": "https://api.github.test/repos/Acme/Widget",
                    "assignees": [{"login": "helperbot"}],
                },
            )
        raise httpx.ReadTimeout("transport included tok-alice and a sensitive query")

    monkeypatch.setattr(httpx, "request", fail_after_create)
    failed_verify = agent_hub.post("/api/assignments", json=ASSIGNMENT)
    assert failed_verify.status_code == 502
    assert failed_verify.json() == {
        "detail": "GitHub request failed: ReadTimeout",
        "url": "https://github.test/Acme/Widget/issues/2",
        "number": 2,
    }
    assert "tok-alice" not in failed_verify.text


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


def test_assignment_list_returns_newest_20_of_130_fake_comments(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    github: FakeGitHub = agent_hub.app.state.hub.github
    github.comments[("Acme/Widget", 1)] = [
        {
            "html_url": f"https://github.test/Acme/Widget/issues/1#issuecomment-{index}",
            "user": {"login": "helperbot"},
            "created_at": f"{index:03d}",
            "body": f"comment-{index:03d}",
        }
        for index in range(130)
    ]

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    assert response.status_code == 200
    comments = response.json()["assignments"][0]["comments"]
    assert [comment["body"] for comment in comments] == [
        f"comment-{index:03d}" for index in range(129, 109, -1)
    ]


def test_real_github_comments_follows_last_page_for_newest_comments(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    first_page = [{"id": index} for index in range(100)]
    last_page = [{"id": index} for index in range(100, 130)]

    def request(_method: str, _url: str, **kwargs: object) -> _Response:
        params = kwargs["params"]
        assert isinstance(params, dict)
        calls.append(params)
        if params.get("page") == 2:
            return _Response(200, last_page)
        return _Response(
            200,
            first_page,
            {
                "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="next", '
                '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="last"'
            },
        )

    monkeypatch.setattr(httpx, "request", request)
    comments = RealGitHub(cfg).issue_comments("tok-alice", "Acme/Widget", 1)
    assert [comment["id"] for comment in comments[-20:]] == list(range(110, 130))
    assert calls == [{"per_page": 100}, {"per_page": 100, "page": 2}]


def test_real_github_comments_fetches_previous_page_when_last_page_is_short(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    first_page = [{"id": index} for index in range(100)]
    last_page = [{"id": index} for index in range(100, 105)]

    def request(_method: str, _url: str, **kwargs: object) -> _Response:
        params = kwargs["params"]
        assert isinstance(params, dict)
        calls.append(params)
        if params.get("page") == 2:
            return _Response(
                200,
                last_page,
                {
                    "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=1>; rel="prev"'
                },
            )
        return _Response(
            200,
            first_page,
            {
                "Link": '<https://api.github.com/repos/Acme/Widget/issues/1/comments?per_page=100&page=2>; rel="last"'
            },
        )

    monkeypatch.setattr(httpx, "request", request)
    comments = RealGitHub(cfg).issue_comments("tok-alice", "Acme/Widget", 1)
    assert [comment["id"] for comment in comments[-20:]] == list(range(85, 105))
    assert calls == [
        {"per_page": 100},
        {"per_page": 100, "page": 2},
        {"per_page": 100, "page": 1},
    ]


def test_assignment_github_calls_use_to_thread(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_to_thread = app_module.asyncio.to_thread
    calls: list[str] = []

    async def recording_to_thread(function, *args, **kwargs):
        calls.append(function.__name__)
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments",
        json={"body": "The first reply."},
    )
    assert response.status_code == 201
    assert calls == ["create_issue", "issue", "issue", "create_issue_comment"]


def test_assignment_form_links_created_issue_and_locks_resubmission() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert (
        "Issue was created but the agent assignment failed — open it instead of submitting again"
        in html
    )
    assert "submit.disabled = true" in html
    assert 'form.addEventListener("input", unlock)' in html
