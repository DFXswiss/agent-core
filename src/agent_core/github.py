"""GitHub OAuth for hub sign-in. Tests inject FakeGitHub."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .config import Config


class GitHubError(ValueError):
    pass


@dataclass(frozen=True)
class GitHubUser:
    login: str


class GitHub:
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        raise NotImplementedError

    def login_for_code(self, code: str) -> GitHubUser:
        raise NotImplementedError


class RealGitHub(GitHub):
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self._cfg.github_client_id,
                "redirect_uri": redirect_uri,
                "scope": "read:user",
                "state": state,
            }
        )
        return f"{self._cfg.github_authorize_url}?{query}"

    def login_for_code(self, code: str) -> GitHubUser:
        if code == "":
            raise GitHubError("authorization code is empty")
        token_resp = httpx.post(
            self._cfg.github_token_url,
            data={
                "client_id": self._cfg.github_client_id,
                "client_secret": self._cfg.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=20.0,
        )
        if token_resp.status_code != 200:
            raise GitHubError(f"token exchange failed: HTTP {token_resp.status_code}")
        token_body = token_resp.json()
        access = token_body.get("access_token")
        if not isinstance(access, str) or access == "":
            raise GitHubError("token exchange returned no access_token")
        user_resp = httpx.get(
            self._cfg.github_user_url,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            timeout=20.0,
        )
        if user_resp.status_code != 200:
            raise GitHubError(f"user lookup failed: HTTP {user_resp.status_code}")
        login = user_resp.json().get("login")
        if not isinstance(login, str) or login.strip() == "":
            raise GitHubError("user lookup returned no login")
        return GitHubUser(login=login.strip().lower())


class FakeGitHub(GitHub):
    """Maps authorization codes to logins. Used only by tests."""

    def __init__(self, codes: dict[str, str]) -> None:
        if not codes:
            raise GitHubError("FakeGitHub requires at least one code")
        self._codes = {k: v.strip().lower() for k, v in codes.items()}

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return f"https://github.test/login/oauth/authorize?state={state}&redirect_uri={redirect_uri}"

    def login_for_code(self, code: str) -> GitHubUser:
        if code not in self._codes:
            raise GitHubError(f"unknown authorization code: {code}")
        return GitHubUser(login=self._codes[code])
