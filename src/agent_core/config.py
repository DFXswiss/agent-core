"""Fail-closed configuration. Missing or empty values abort."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(SystemExit):
    pass


def require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ConfigError(f"{name} is not set")
    return value


@dataclass(frozen=True)
class Config:
    public_url: str
    session_secret: str
    github_client_id: str
    github_client_secret: str
    github_authorize_url: str
    github_token_url: str
    github_user_url: str
    database: str
    teams_path: Path
    host: str
    port: int

    @staticmethod
    def from_env() -> "Config":
        secret = require("AGENT_CORE_SESSION_SECRET")
        if len(secret) < 32:
            raise ConfigError("AGENT_CORE_SESSION_SECRET must be at least 32 characters")
        port_raw = require("AGENT_CORE_PORT")
        if not port_raw.isdigit():
            raise ConfigError("AGENT_CORE_PORT must be an integer")
        teams = Path(require("AGENT_CORE_TEAMS"))
        if not teams.is_file():
            raise ConfigError(f"AGENT_CORE_TEAMS is not a file: {teams}")
        return Config(
            public_url=require("AGENT_CORE_PUBLIC_URL").rstrip("/"),
            session_secret=secret,
            github_client_id=require("AGENT_CORE_GITHUB_CLIENT_ID"),
            github_client_secret=require("AGENT_CORE_GITHUB_CLIENT_SECRET"),
            github_authorize_url=require("AGENT_CORE_GITHUB_AUTHORIZE_URL"),
            github_token_url=require("AGENT_CORE_GITHUB_TOKEN_URL"),
            github_user_url=require("AGENT_CORE_GITHUB_USER_URL"),
            database=require("AGENT_CORE_DATABASE"),
            teams_path=teams,
            host=require("AGENT_CORE_HOST"),
            port=int(port_raw),
        )
