from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_core.app import create_app
from agent_core.db import Store
from agent_core.github import FakeGitHub, RealGitHub
from tests.conftest import sign_in


def test_api_prs_is_gone(hub) -> None:
    sign_in(hub, "code-alice")
    assert hub.get("/api/prs").status_code == 404


def test_index_has_no_github_pr_ui() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="prs"' not in html
    assert 'id="k-prs"' not in html
    assert 'id="prs-sub"' not in html
    assert "/api/prs" not in html
    assert "fillPrs" not in html
    assert "kickPrs" not in html
    assert "Open PRs" not in html
    assert "api.github.com" not in html
    assert "jsonGet(\"/api/state\"" in html
    assert "kickState();" in html
    assert "await kickState" not in html


def test_oauth_scope_is_read_user_only(cfg) -> None:
    url = RealGitHub(cfg).authorize_url("st", "http://127.0.0.1/auth/github/callback")
    query = parse_qs(urlparse(url).query)
    assert query.get("scope") == ["read:user"]


def test_dashboard_state_comes_from_replica(hub) -> None:
    sign_in(hub, "code-alice")
    res = hub.get("/api/state")
    assert res.status_code == 200
    body = res.json()
    assert "session" in body and "task" in body and "devices" in body
    assert "prs" not in body


def test_auth_me_has_no_token(hub) -> None:
    sign_in(hub, "code-alice")
    me = hub.get("/auth/me").json()
    assert set(me) == {"login", "visible", "teams"}
    assert "token" not in me and "github_token" not in me
    cookie = ";".join(f"{k}={v}" for k, v in hub.cookies.items())
    assert "tok-alice" not in cookie


def test_logout_clears_session(hub) -> None:
    sign_in(hub, "code-alice")
    assert hub.get("/auth/me").status_code == 200
    hub.post("/auth/logout")
    assert hub.get("/auth/me").status_code == 401


def test_oauth_token_table_is_dropped(cfg, github: FakeGitHub) -> None:
    store = Store(cfg.database)
    create_app(cfg, github=github, store=store)
    row = store.query_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'oauth_token'"
    )
    assert row is None
