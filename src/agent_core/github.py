"""GitHub OAuth and open-PR search. Tests inject FakeGitHub."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from .config import Config

GITHUB_SEARCH_URL = "https://api.github.com/search/issues"


class GitHubError(ValueError):
    pass


@dataclass(frozen=True)
class GitHubUser:
    login: str
    token: str = ""


class GitHub:
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        raise NotImplementedError

    def login_for_code(self, code: str) -> GitHubUser:
        raise NotImplementedError

    def search_open_prs(self, token: str, logins: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError


def _pr_from_search_item(item: dict[str, Any]) -> dict[str, Any] | None:
    html = item.get("html_url")
    if not isinstance(html, str) or "/pull/" not in html:
        return None
    path = urlparse(html).path.strip("/").split("/")
    # owner/repo/pull/N
    if len(path) < 4 or path[2] != "pull":
        return None
    org, repo, _, num = path[0], path[1], path[2], path[3]
    if not num.isdigit():
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    author = user.get("login") if isinstance(user, dict) else ""
    if not isinstance(author, str) or author == "":
        author = ""
    title = item.get("title") if isinstance(item.get("title"), str) else ""
    draft = bool(item.get("draft"))
    status = "draft" if draft else (item.get("state") if isinstance(item.get("state"), str) else "open")
    return {
        "author": author.lower(),
        "org": org,
        "repo": repo,
        "number": int(num),
        "title": title,
        "status": status,
        "url": html,
    }


class RealGitHub(GitHub):
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self._cfg.github_client_id,
                "redirect_uri": redirect_uri,
                "scope": "read:user repo",
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
        return GitHubUser(login=login.strip().lower(), token=access)

    def search_open_prs(self, token: str, logins: list[str]) -> list[dict[str, Any]]:
        if token == "" or not logins:
            return []
        unique = []
        seen: set[str] = set()
        for login in logins:
            low = login.strip().lower()
            if low == "" or low in seen:
                continue
            seen.add(low)
            unique.append(low)
        clauses = []
        for login in unique:
            clauses.append(f"author:{login}")
            clauses.append(f"assignee:{login}")
        q = "is:pr is:open (" + " OR ".join(clauses) + ")"
        resp = httpx.get(
            GITHUB_SEARCH_URL,
            params={"q": q, "sort": "updated", "order": "desc", "per_page": 50},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20.0,
        )
        if resp.status_code != 200:
            raise GitHubError(f"PR search failed: HTTP {resp.status_code}")
        body = resp.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise GitHubError("PR search failed: HTTP 200")
        out: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, int]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            row = _pr_from_search_item(item)
            if row is None:
                continue
            key = (row["org"], row["repo"], row["number"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(row)
        return out


class FakeGitHub(GitHub):
    """Maps authorization codes to logins. Used only by tests."""

    def __init__(self, codes: dict[str, str], prs: list[dict[str, Any]] | None = None) -> None:
        if not codes:
            raise GitHubError("FakeGitHub requires at least one code")
        self._codes = {k: v.strip().lower() for k, v in codes.items()}
        self._prs = list(prs or [])
        self.search_status = 200

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return f"https://github.test/login/oauth/authorize?state={state}&redirect_uri={redirect_uri}"

    def login_for_code(self, code: str) -> GitHubUser:
        if code not in self._codes:
            raise GitHubError(f"unknown authorization code: {code}")
        login = self._codes[code]
        return GitHubUser(login=login, token="tok-" + login)

    def search_open_prs(self, token: str, logins: list[str]) -> list[dict[str, Any]]:
        if self.search_status in (401, 403):
            raise GitHubError(f"PR search failed: HTTP {self.search_status}")
        if token == "" or not token.startswith("tok-"):
            return []
        allowed = {m.strip().lower() for m in logins}
        return [
            p
            for p in self._prs
            if str(p.get("author", "")).lower() in allowed
            or str(p.get("assignee", "")).lower() in allowed
        ]
