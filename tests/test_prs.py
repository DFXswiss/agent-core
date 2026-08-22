from __future__ import annotations

import json
import sqlite3
from base64 import b64decode
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from agent_core.app import create_app
from agent_core.config import Config
from agent_core.db import Store
from agent_core.github import FakeGitHub, RealGitHub
from tests.conftest import sign_in


def _session_payload(cfg: Config, client: TestClient) -> dict:
    raw = client.cookies["session"]
    data = TimestampSigner(cfg.session_secret).unsign(raw.encode("utf-8"), max_age=14 * 24 * 60 * 60)
    payload = json.loads(b64decode(data))
    if not isinstance(payload, dict):
        raise AssertionError("session cookie is not an object")
    return payload


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


def test_auth_me_has_no_token(cfg, hub) -> None:
    sign_in(hub, "code-alice")
    me = hub.get("/auth/me").json()
    assert set(me) == {"login", "visible", "teams"}
    assert "token" not in me and "github_token" not in me
    assert set(_session_payload(cfg, hub)) == {"login"}


def test_real_oauth_access_token_is_not_stored(cfg, monkeypatch) -> None:
    class _Resp:
        def __init__(self, body: dict) -> None:
            self.status_code = 200
            self._body = body

        def json(self) -> dict:
            return self._body

    monkeypatch.setattr(
        "agent_core.github.httpx.post",
        lambda *a, **k: _Resp({"access_token": "gh-secret-token"}),
    )
    monkeypatch.setattr(
        "agent_core.github.httpx.get",
        lambda *a, **k: _Resp({"login": "Alice"}),
    )
    client = TestClient(create_app(cfg, github=RealGitHub(cfg), store=Store(cfg.database)))
    sign_in(client, "any-code")
    me = client.get("/auth/me").json()
    assert set(me) == {"login", "visible", "teams"}
    assert me["login"] == "alice"
    session = _session_payload(cfg, client)
    assert session == {"login": "alice"}
    assert "gh-secret-token" not in json.dumps(session)
    db = sqlite3.connect(cfg.database)
    try:
        tables = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]
        assert "oauth_token" not in tables
    finally:
        db.close()
    assert b"gh-secret-token" not in Path(cfg.database).read_bytes()


def test_logout_clears_session(hub) -> None:
    sign_in(hub, "code-alice")
    assert hub.get("/auth/me").status_code == 200
    hub.post("/auth/logout")
    assert hub.get("/auth/me").status_code == 401


def test_oauth_token_table_is_dropped(cfg, github: FakeGitHub) -> None:
    conn = sqlite3.connect(cfg.database)
    conn.execute(
        "CREATE TABLE oauth_token (github_login TEXT PRIMARY KEY, token TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO oauth_token (github_login, token) VALUES (?, ?)",
        ("alice", "legacy-secret-token"),
    )
    conn.commit()
    conn.close()
    store = Store(cfg.database)
    create_app(cfg, github=github, store=store)
    row = store.query_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'oauth_token'"
    )
    assert row is None
    leftover = sqlite3.connect(cfg.database)
    try:
        n = leftover.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'oauth_token'"
        ).fetchone()[0]
        assert n == 0
    finally:
        leftover.close()
