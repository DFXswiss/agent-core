from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import agent_core.app as app_module
from agent_core.db import dumps
from agent_core.github import FakeGitHub, GitHubError, RealGitHub
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


def test_real_github_error_exposes_http_status(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *_args, **_kwargs: _Response(401, {"message": "Bad credentials"}),
    )

    with pytest.raises(GitHubError) as raised:
        RealGitHub(cfg).issue("expired-token", "Acme/Widget", 1)

    assert raised.value.status_code == 401


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
    assert activity["payload"]["issue_key"] == "Acme/Widget#1"
    assert activity["payload"]["url"] == "https://github.com/Acme/Widget/issues/1"
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
    assert activity["payload"]["issue_key"] == "Acme/Widget#1"

    device_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac03"
    token = pair_device(agent_hub, "code-alice", device_id)
    query = agent_hub.post(
        "/sync/query",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "match": {
                "type": "assignment.created",
                "payload.issue_key": "Acme/Widget#1",
            }
        },
    )
    assert query.status_code == 200, query.text
    assert [row["row_id"] for row in query.json()["rows"]] == [activity["id"]]


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


@pytest.mark.parametrize(
    ("github_status", "response_status", "token_retained"),
    ((401, 409, False), (403, 502, True)),
)
def test_assignment_create_handles_github_auth_status(
    agent_hub: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    github_status: int,
    response_status: int,
    token_retained: bool,
) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub

    def reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise GitHubError(
            f"GitHub request failed: HTTP {github_status}", status_code=github_status
        )

    monkeypatch.setattr(hub.github, "create_issue", reject)
    response = agent_hub.post("/api/assignments", json=ASSIGNMENT)

    assert response.status_code == response_status
    if github_status == 401:
        assert response.json() == {"detail": "sign in on the website again"}
    else:
        assert "HTTP 403" in response.json()["detail"]
    assert hub.store.get_oauth_token("alice") == ("tok-alice" if token_retained else "")
    assert (hub.github_tokens.get("alice") == "tok-alice") is token_retained


def test_assignment_reply_expired_token_asks_reauth(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub

    def expired(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise GitHubError("GitHub request failed: HTTP 401", status_code=401)

    monkeypatch.setattr(hub.github, "issue", expired)
    response = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": "A reply."}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "sign in on the website again"}
    assert hub.store.get_oauth_token("alice") == ""
    assert "alice" not in hub.github_tokens


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
    reply = next(row for row in state["activity"] if row.get("type") == "assignment.reply")
    assert reply["payload"]["agent"] == "helperbot"
    assert reply["payload"]["issue_key"] == "Acme/Widget#1"
    assert reply["payload"]["url"] == "https://github.com/Acme/Widget/issues/1"
    replica = agent_hub.app.state.hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'activity' AND row_id = ?",
        (reply["id"],),
    )
    assert replica["origin_device_id"] == "web:alice"

    sign_in(agent_hub, "code-bob")
    denied = agent_hub.post(
        "/api/assignments/Acme/Widget/1/comments", json={"body": "I do not own this agent."}
    )
    assert denied.status_code == 403


def test_device_cannot_push_hub_authored_assignment_activity(agent_hub: TestClient) -> None:
    device_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaac04"
    token = pair_device(agent_hub, "code-alice", device_id)
    pushed = agent_hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "origin_device_id": device_id,
                    "origin_seq": 1,
                    "table": "activity",
                    "op": "insert",
                    "row_id": "forged-assignment",
                    "payload": {
                        "id": "forged-assignment",
                        "type": "assignment.created",
                        "payload": {
                            "agent": "helperbot",
                            "actor": "alice",
                            "url": "javascript:alert(1)",
                        },
                    },
                    "occurred_at": "2026-11-05T10:00:00Z",
                }
            ]
        },
    )
    assert pushed.status_code == 400
    assert pushed.json()["detail"] == "assignment activity is hub-authored only"
    assert (
        agent_hub.app.state.hub.store.query_one(
            "SELECT row_id FROM row_replica WHERE table_name = 'activity' AND row_id = ?",
            ("forged-assignment",),
        )
        is None
    )


def test_assignment_list_ignores_non_web_and_actor_mismatched_rows(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    store = agent_hub.app.state.hub.store
    for row_id, origin, actor in (
        ("trusted-assignment", "web:alice", "alice"),
        ("device-assignment", "device-alice", "alice"),
        ("mismatched-assignment", "web:alice", "bob"),
    ):
        store.execute(
            "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "activity",
                row_id,
                origin,
                "alice",
                dumps(
                    {
                        "id": row_id,
                        "type": "assignment.created",
                        "payload": {
                            "agent": "helperbot",
                            "actor": actor,
                            "repo": "Acme/Widget",
                            "number": 99,
                            "title": row_id,
                            "url": "javascript:alert(1)",
                        },
                    }
                ),
                "2026-11-05T10:00:00Z",
            ),
        )

    listed = agent_hub.get("/api/assignments", params={"agent": "helperbot"})
    assert listed.status_code == 200
    assert [row["title"] for row in listed.json()["assignments"]] == ["trusted-assignment"]


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


def test_assignment_list_enriches_only_newest_50(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    github: FakeGitHub = hub.github
    for number in range(1, 53):
        github.issues[("Acme/Widget", number)] = {
            "number": number,
            "html_url": f"https://github.test/Acme/Widget/issues/{number}",
            "repository_url": "https://api.github.test/repos/Acme/Widget",
            "state": "open",
            "assignees": [{"login": "helperbot"}],
        }
        hub.store.execute(
            "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "activity",
                f"assignment-{number:02d}",
                "web:alice",
                "alice",
                dumps(
                    {
                        "id": f"assignment-{number:02d}",
                        "type": "assignment.created",
                        "payload": {
                            "agent": "helperbot",
                            "actor": "alice",
                            "repo": "Acme/Widget",
                            "number": number,
                            "title": f"assignment-{number:02d}",
                        },
                    }
                ),
                f"2026-11-05T10:{number:02d}:00Z",
            ),
        )

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    rows = response.json()["assignments"]
    assert [row["number"] for row in rows] == list(range(52, 0, -1))
    assert [row["enriched"] for row in rows] == [True] * 50 + [False] * 2
    assert len([call for call in github.issue_calls if call["operation"] == "issue"]) == 50
    assert (
        len([call for call in github.issue_calls if call["operation"] == "issue_comments"])
        == 50
    )


def test_assignment_list_stops_enrichment_and_drops_expired_token(
    agent_hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(agent_hub, "code-alice")
    assert agent_hub.post("/api/assignments", json=ASSIGNMENT).status_code == 201
    hub = agent_hub.app.state.hub
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "activity",
            "second-assignment",
            "web:alice",
            "alice",
            dumps(
                {
                    "id": "second-assignment",
                    "type": "assignment.created",
                    "payload": {
                        "agent": "helperbot",
                        "actor": "alice",
                        "repo": "Acme/Widget",
                        "number": 1,
                        "title": "Second mandate",
                    },
                }
            ),
            "2099-01-01T00:00:00Z",
        ),
    )
    calls = 0

    def expired(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise GitHubError("GitHub request failed: HTTP 401", status_code=401)

    monkeypatch.setattr(hub.github, "issue", expired)
    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    assert len(response.json()["assignments"]) == 2
    assert all(not row["enriched"] for row in response.json()["assignments"])
    assert calls == 1
    assert hub.store.get_oauth_token("alice") == ""
    assert "alice" not in hub.github_tokens


def test_assignment_list_prunes_and_bounds_issue_cache(agent_hub: TestClient) -> None:
    sign_in(agent_hub, "code-alice")
    hub = agent_hub.app.state.hub
    now = time.monotonic()
    hub.issue_cache[("alice", "expired/old", 1)] = (now - 2, {})
    hub.issue_cache[("alice", "expired/new", 2)] = (now - 1, {})
    for number in range(502):
        hub.issue_cache[("alice", "Acme/Widget", number)] = (now + number + 1, {})

    response = agent_hub.get("/api/assignments", params={"agent": "helperbot"})

    assert response.status_code == 200
    assert len(hub.issue_cache) == 500
    assert ("alice", "expired/old", 1) not in hub.issue_cache
    assert ("alice", "expired/new", 2) not in hub.issue_cache
    assert ("alice", "Acme/Widget", 0) not in hub.issue_cache
    assert ("alice", "Acme/Widget", 1) not in hub.issue_cache
    assert ("alice", "Acme/Widget", 2) in hub.issue_cache


def test_fake_github_mutations_resolve_mixed_case_repo_to_canonical_name() -> None:
    github = FakeGitHub({"code-alice": "alice"})
    github.canonical_repos["acme/widget"] = "Acme/Widget"
    created = github.create_issue("tok-alice", "acme/widget", "Title", "Body", [])

    fetched = github.issue("tok-alice", "ACME/WIDGET", created["number"])
    assigned = github.add_assignees(
        "tok-alice", "ACME/WIDGET", created["number"], ["helperbot"]
    )
    comment = github.create_issue_comment(
        "tok-alice", "ACME/WIDGET", created["number"], "Reply"
    )

    assert fetched["html_url"] == "https://github.test/Acme/Widget/issues/1"
    assert assigned["assignees"] == [{"login": "helperbot"}]
    assert comment["html_url"] == (
        "https://github.test/Acme/Widget/issues/1#issuecomment-1"
    )


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


def test_assignment_and_timeline_links_use_safe_href() -> None:
    static = Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static"
    index = (static / "index.html").read_text(encoding="utf-8")
    session = (static / "session.html").read_text(encoding="utf-8")

    for html in (index, session):
        assert "function safeHref(url)" in html
        assert 'parsed.protocol === "https:"' in html
        assert 'parsed.host === "github.com"' in html
    assert "safeHref(r.url)" in index
    assert "safeHref(a.url)" in index
    assert "safeHref(c.url)" in index
    assert "safeHref(url)" in index
    assert "safeHref(inner.url)" in session
    assert 'href="${esc(a.url)}"' not in index
    assert 'href="${esc(inner.url)}"' not in session
