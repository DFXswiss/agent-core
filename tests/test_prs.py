from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.github import FakeGitHub, GitHubError, RealGitHub, _pr_from_search_item
from tests.conftest import sign_in


def test_pr_from_search_item() -> None:
    row = _pr_from_search_item(
        {
            "html_url": "https://github.com/DFXswiss/agent/pull/11",
            "title": "Add allow",
            "state": "open",
            "draft": True,
            "user": {"login": "TaprootFreakAI"},
        }
    )
    assert row == {
        "author": "taprootfreakai",
        "org": "DFXswiss",
        "repo": "agent",
        "number": 11,
        "title": "Add allow",
        "status": "draft",
        "url": "https://github.com/DFXswiss/agent/pull/11",
    }


def test_api_prs_requires_login(hub) -> None:
    res = hub.get("/api/prs")
    assert res.status_code == 401


def test_api_prs_team_filter(hub, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "draft",
            "url": "https://github.com/acme/app/pull/7",
        },
        {
            "author": "bob",
            "org": "acme",
            "repo": "app",
            "number": 8,
            "title": "Add sync",
            "status": "open",
            "url": "https://github.com/acme/app/pull/8",
        },
        {
            "author": "dave",
            "org": "other",
            "repo": "x",
            "number": 1,
            "title": "secret",
            "status": "open",
            "url": "https://github.com/other/x/pull/1",
        },
    ]
    sign_in(hub, "code-alice")
    body = hub.get("/api/prs").json()
    assert body["source"] == "github"
    numbers = sorted(p["number"] for p in body["prs"])
    assert numbers == [7, 8]
    authors = {p["author"] for p in body["prs"]}
    assert authors == {"alice", "bob"}


def test_api_prs_self_only(hub, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "dave",
            "org": "other",
            "repo": "x",
            "number": 1,
            "title": "secret",
            "status": "open",
            "url": "https://github.com/other/x/pull/1",
        },
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "draft",
            "url": "https://github.com/acme/app/pull/7",
        },
    ]
    sign_in(hub, "code-dave")
    body = hub.get("/api/prs").json()
    assert [p["number"] for p in body["prs"]] == [1]


def test_api_prs_includes_assignee(hub, github: FakeGitHub) -> None:
    github._prs = [
        {
            "author": "outside",
            "assignee": "alice",
            "org": "acme",
            "repo": "app",
            "number": 9,
            "title": "Help alice",
            "status": "open",
            "url": "https://github.com/acme/app/pull/9",
        }
    ]
    sign_in(hub, "code-alice")
    body = hub.get("/api/prs").json()
    assert [p["number"] for p in body["prs"]] == [9]


def test_prs_survive_new_hub(cfg, github: FakeGitHub) -> None:
    from fastapi.testclient import TestClient

    from agent_core.app import create_app
    from agent_core.db import Store

    github._prs = [
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "draft",
            "url": "https://github.com/acme/app/pull/7",
        }
    ]
    store = Store(cfg.database)
    first = TestClient(create_app(cfg, github=github, store=store))
    sign_in(first, "code-alice")
    assert first.get("/api/prs").json()["source"] == "github"
    second = TestClient(create_app(cfg, github=github, store=store))
    second.cookies.update(first.cookies)
    body = second.get("/api/prs").json()
    assert body["source"] == "github"
    assert [p["number"] for p in body["prs"]] == [7]


def test_auth_me_has_no_token(hub) -> None:
    sign_in(hub, "code-alice")
    me = hub.get("/auth/me").json()
    assert set(me) == {"login", "visible", "teams"}
    assert "token" not in me and "github_token" not in me
    cookie = ";".join(f"{k}={v}" for k, v in hub.cookies.items())
    assert "tok-alice" not in cookie


def test_logout_drops_token(hub, github: FakeGitHub, cfg) -> None:
    github._prs = [
        {
            "author": "alice",
            "org": "acme",
            "repo": "app",
            "number": 7,
            "title": "Fix login",
            "status": "open",
            "url": "https://github.com/acme/app/pull/7",
        }
    ]
    sign_in(hub, "code-alice")
    assert hub.get("/api/prs").json()["source"] == "github"
    hub.post("/auth/logout")
    assert hub.get("/api/prs").status_code == 401
    from agent_core.db import Store

    store = Store(cfg.database)
    assert store.get_oauth_token("alice") == ""


def test_expired_github_token_asks_reauth(hub, github: FakeGitHub) -> None:
    sign_in(hub, "code-alice")
    github.search_status = 401
    body = hub.get("/api/prs").json()
    assert body["source"] == "none"
    assert body["prs"] == []
    github.search_status = 200
    assert hub.get("/api/prs").json()["source"] == "none"


def test_index_lists_pr_columns() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    for col in ("Author", "Org", "Repo", "PR", "Description", "Status"):
        assert f"<th>{col}</th>" in html
    assert 'id="prs"' in html
    assert html.index('id="prs"') < html.index('id="sessions"')
    assert html.index('id="err"') < html.index('id="signed-in"')
    render_fn = html.split("async function render()", 1)[1].split("function kickState()", 1)[0]
    assert 'k-people").textContent' in render_fn
    assert "kickState();" in render_fn
    assert "kickPrs();" in render_fn
    assert "await kickState" not in render_fn
    assert "await kickPrs" not in render_fn
    assert html.index('k-people").textContent') < html.index("kickState();")
    assert html.index("kickState();") < html.index("kickPrs();")
    after_me = render_fn.split("await me()", 1)[1]
    assert "if (gen !== renderGen) return" in after_me.split("catch", 1)[0]
    assert html.index("if (stateInflight)") < html.index('jsonGet("/api/state"')
    assert html.index("if (prsInflight)") < html.index('jsonGet("/api/prs"')
    assert "stateInflight.gen !== gen" in html
    assert "prsInflight.gen !== gen" in html
    assert "if (stateCtl) stateCtl.abort()" not in html
    assert "if (prsCtl) prsCtl.abort()" not in html
    signed_out = html.split("if (!user)", 1)[1].split("window.__login", 1)[0]
    assert "abortHubFetches()" in signed_out
    assert "err.hidden = true" in signed_out
    switch_fn = html.split("window.__login !== user.login", 1)[1].split("window.__login = user.login", 1)[0]
    assert "abortHubFetches()" in switch_fn
    assert "clearHubTables()" in switch_fn
    clear_fn = html.split("function clearHubTables()", 1)[1].split("function jsonGet", 1)[0]
    assert 'id="prs-sub"' in html
    assert "prs-sub" in clear_fn
    assert "Open PRs authored or assigned to people you can see." in clear_fn
    catch_fn = html.split("async function render()", 1)[1].split("function kickState()", 1)[0].split("} catch (e)", 1)[1]
    assert "abortHubFetches()" in catch_fn
    assert "err.hidden = false" in catch_fn
    assert 'jsonGet("/api/state", stateCtl, 15000)' in html
    assert 'jsonGet("/api/prs", prsCtl, 25000)' in html
    assert "Timed out loading sessions." in html
    assert "Timed out loading PRs." in html
    assert "GitHub access is missing" in html
    assert "source === \"none\"" in html or "source === 'none'" in html


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_real_github_searches_one_login_at_a_time(cfg, monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        q = params["q"]
        calls.append(q)
        login = q.rsplit("involves:", 1)[-1]
        n = {"alice": 1, "bob": 2, "cara": 3}[login]
        return _Resp(
            200,
            {
                "items": [
                    {
                        "html_url": f"https://github.com/acme/app/pull/{n}",
                        "title": login,
                        "state": "open",
                        "draft": False,
                        "user": {"login": login},
                    }
                ]
            },
        )

    monkeypatch.setattr("agent_core.github.httpx.get", fake_get)
    rows = RealGitHub(cfg).search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice", "bob", "cara"])
    assert len(calls) == 3
    assert all(q.startswith("is:pr is:open involves:") and " OR " not in q for q in calls)
    assert {r["author"] for r in rows} == {"alice", "bob", "cara"}


def test_real_github_skips_unsearchable_login(cfg, monkeypatch) -> None:
    def fake_get(url, params=None, headers=None, timeout=None):
        login = params["q"].rsplit("involves:", 1)[-1]
        if login == "bob":
            return _Resp(422, {"message": "Validation Failed"})
        return _Resp(
            200,
            {
                "items": [
                    {
                        "html_url": "https://github.com/acme/app/pull/7",
                        "title": "ok",
                        "state": "open",
                        "draft": False,
                        "user": {"login": "alice"},
                    }
                ]
            },
        )

    monkeypatch.setattr("agent_core.github.httpx.get", fake_get)
    rows = RealGitHub(cfg).search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice", "bob"])
    assert [r["number"] for r in rows] == [7]


def test_real_github_401_still_raises(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_core.github.httpx.get",
        lambda *a, **k: _Resp(401, {"message": "Bad credentials"}),
    )
    with pytest.raises(GitHubError, match="HTTP 401"):
        RealGitHub(cfg).search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice"])


def test_real_github_blank_logins_are_empty(cfg, monkeypatch) -> None:
    monkeypatch.setattr("agent_core.github.httpx.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http")))
    assert RealGitHub(cfg).search_open_prs("tok-xxxxxxxxxxxxxxxx", ["", "  "]) == []


def test_real_github_403_raises(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_core.github.httpx.get",
        lambda *a, **k: _Resp(403, {"message": "rate limit"}),
    )
    with pytest.raises(GitHubError, match="HTTP 403"):
        RealGitHub(cfg).search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice"])


def test_real_github_does_not_cache_http_500(cfg, monkeypatch) -> None:
    n = {"calls": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        n["calls"] += 1
        return _Resp(500, {"message": "nope"})

    monkeypatch.setattr("agent_core.github.httpx.get", fake_get)
    gh = RealGitHub(cfg)
    with pytest.raises(GitHubError, match="HTTP 500"):
        gh.search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice"])
    with pytest.raises(GitHubError, match="HTTP 500"):
        gh.search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice"])
    assert n["calls"] == 2


def test_real_github_caches_search(cfg, monkeypatch) -> None:
    n = {"calls": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        n["calls"] += 1
        return _Resp(
            200,
            {
                "items": [
                    {
                        "html_url": "https://github.com/acme/app/pull/1",
                        "title": "x",
                        "state": "open",
                        "draft": False,
                        "user": {"login": "alice"},
                    }
                ]
            },
        )

    monkeypatch.setattr("agent_core.github.httpx.get", fake_get)
    gh = RealGitHub(cfg)
    assert gh.search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice"])[0]["number"] == 1
    assert gh.search_open_prs("tok-xxxxxxxxxxxxxxxx", ["alice"])[0]["number"] == 1
    assert n["calls"] == 1
