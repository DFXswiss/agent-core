from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.config import Config, ConfigError


REQUIRED = (
    "AGENT_CORE_PUBLIC_URL",
    "AGENT_CORE_SESSION_SECRET",
    "AGENT_CORE_GITHUB_CLIENT_ID",
    "AGENT_CORE_GITHUB_CLIENT_SECRET",
    "AGENT_CORE_GITHUB_AUTHORIZE_URL",
    "AGENT_CORE_GITHUB_TOKEN_URL",
    "AGENT_CORE_GITHUB_USER_URL",
    "AGENT_CORE_DATABASE",
    "AGENT_CORE_TEAMS",
    "AGENT_CORE_HOST",
    "AGENT_CORE_PORT",
)


def test_from_env_requires_every_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    teams = tmp_path / "teams.yaml"
    teams.write_text("teams:\n  dfx:\n    members: []\n", encoding="utf-8")
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    env = {
        "AGENT_CORE_PUBLIC_URL": "http://127.0.0.1:8787",
        "AGENT_CORE_SESSION_SECRET": "x" * 32,
        "AGENT_CORE_GITHUB_CLIENT_ID": "id",
        "AGENT_CORE_GITHUB_CLIENT_SECRET": "secret",
        "AGENT_CORE_GITHUB_AUTHORIZE_URL": "https://github.com/login/oauth/authorize",
        "AGENT_CORE_GITHUB_TOKEN_URL": "https://github.com/login/oauth/access_token",
        "AGENT_CORE_GITHUB_USER_URL": "https://api.github.com/user",
        "AGENT_CORE_DATABASE": str(tmp_path / "db.sqlite"),
        "AGENT_CORE_TEAMS": str(teams),
        "AGENT_CORE_HOST": "127.0.0.1",
        "AGENT_CORE_PORT": "8787",
    }
    with pytest.raises(ConfigError, match="is not set"):
        Config.from_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env()
    assert cfg.port == 8787
    monkeypatch.setenv("AGENT_CORE_SESSION_SECRET", "short")
    with pytest.raises(ConfigError, match="32"):
        Config.from_env()
    monkeypatch.setenv("AGENT_CORE_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("AGENT_CORE_TEAMS", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError, match="not a file"):
        Config.from_env()
    assert os.environ["AGENT_CORE_HOST"] == "127.0.0.1"
