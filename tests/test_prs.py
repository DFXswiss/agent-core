from __future__ import annotations

from pathlib import Path

from agent_core.github import FakeGitHub, _pr_from_search_item
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
