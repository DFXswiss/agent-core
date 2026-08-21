from __future__ import annotations

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


def test_index_lists_pr_columns() -> None:
    html = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "agent_core"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    for col in ("Author", "Org", "Repo", "PR", "Description", "Status"):
        assert f"<th>{col}</th>" in html
    assert 'id="prs"' in html
    assert html.index('id="prs"') < html.index('id="sessions"')
