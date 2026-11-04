"""GitHub OAuth and open-PR search. Tests inject FakeGitHub."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from .config import Config

GITHUB_SEARCH_URL = "https://api.github.com/search/issues"
GITHUB_API_URL = "https://api.github.com"


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

    def create_issue(
        self, token: str, repo: str, title: str, body: str, assignees: list[str]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def issue(self, token: str, repo: str, number: int) -> dict[str, Any]:
        raise NotImplementedError

    def add_assignees(self, token: str, repo: str, number: int, assignees: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    def create_issue_comment(self, token: str, repo: str, number: int, body: str) -> dict[str, Any]:
        raise NotImplementedError

    def issue_events(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def issue_comments(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
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

    def _request_json(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        resp = httpx.request(
            method,
            f"{GITHUB_API_URL}{path}",
            json=json_body,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20.0,
        )
        if resp.status_code not in expected:
            message = ""
            try:
                raw_message = resp.json().get("message")
                if isinstance(raw_message, str):
                    message = ": " + raw_message[:500]
            except Exception:
                pass
            raise GitHubError(f"GitHub request failed: HTTP {resp.status_code}{message}")
        try:
            return resp.json()
        except Exception as exc:
            raise GitHubError(f"GitHub request failed: HTTP {resp.status_code} returned invalid JSON") from exc

    def create_issue(
        self, token: str, repo: str, title: str, body: str, assignees: list[str]
    ) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"/repos/{repo}/issues",
            token,
            json_body={"title": title, "body": body, "assignees": assignees},
            expected=(201,),
        )
        if not isinstance(result, dict):
            raise GitHubError("GitHub issue creation returned an invalid response")
        return result

    def issue(self, token: str, repo: str, number: int) -> dict[str, Any]:
        result = self._request_json("GET", f"/repos/{repo}/issues/{number}", token)
        if not isinstance(result, dict):
            raise GitHubError("GitHub issue lookup returned an invalid response")
        return result

    def add_assignees(self, token: str, repo: str, number: int, assignees: list[str]) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"/repos/{repo}/issues/{number}/assignees",
            token,
            json_body={"assignees": assignees},
        )
        if not isinstance(result, dict):
            raise GitHubError("GitHub assignee update returned an invalid response")
        return result

    def create_issue_comment(self, token: str, repo: str, number: int, body: str) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            token,
            json_body={"body": body},
            expected=(201,),
        )
        if not isinstance(result, dict):
            raise GitHubError("GitHub comment creation returned an invalid response")
        return result

    def issue_events(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
        result = self._request_json(
            "GET", f"/repos/{repo}/issues/{number}/events", token, params={"per_page": 100}
        )
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def issue_comments(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
        result = self._request_json(
            "GET", f"/repos/{repo}/issues/{number}/comments", token, params={"per_page": 100}
        )
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


class FakeGitHub(GitHub):
    """Maps authorization codes to logins. Used only by tests."""

    def __init__(self, codes: dict[str, str], prs: list[dict[str, Any]] | None = None) -> None:
        if not codes:
            raise GitHubError("FakeGitHub requires at least one code")
        self._codes = {k: v.strip().lower() for k, v in codes.items()}
        self._prs = list(prs or [])
        self.search_status = 200
        self.issues: dict[tuple[str, int], dict[str, Any]] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.issue_calls: list[dict[str, Any]] = []
        self.omit_assignees_on_create = False
        self.omit_assignees_after_add = False

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

    def create_issue(
        self, token: str, repo: str, title: str, body: str, assignees: list[str]
    ) -> dict[str, Any]:
        number = 1 + max((n for r, n in self.issues if r == repo), default=0)
        assigned = [] if self.omit_assignees_on_create else [{"login": a} for a in assignees]
        issue = {
            "number": number,
            "html_url": f"https://github.test/{repo}/issues/{number}",
            "state": "open",
            "title": title,
            "body": body,
            "assignees": assigned,
        }
        self.issues[(repo, number)] = issue
        self.issue_calls.append(
            {
                "operation": "create_issue",
                "token": token,
                "repo": repo,
                "title": title,
                "body": body,
                "assignees": list(assignees),
            }
        )
        return dict(issue)

    def issue(self, token: str, repo: str, number: int) -> dict[str, Any]:
        self.issue_calls.append({"operation": "issue", "token": token, "repo": repo, "number": number})
        issue = self.issues.get((repo, number))
        if issue is None:
            raise GitHubError("GitHub request failed: HTTP 404: Not Found")
        return dict(issue)

    def add_assignees(self, token: str, repo: str, number: int, assignees: list[str]) -> dict[str, Any]:
        self.issue_calls.append(
            {
                "operation": "add_assignees",
                "token": token,
                "repo": repo,
                "number": number,
                "assignees": list(assignees),
            }
        )
        issue = self.issues.get((repo, number))
        if issue is None:
            raise GitHubError("GitHub request failed: HTTP 404: Not Found")
        if not self.omit_assignees_after_add:
            issue["assignees"] = [{"login": a} for a in assignees]
        return dict(issue)

    def create_issue_comment(self, token: str, repo: str, number: int, body: str) -> dict[str, Any]:
        if (repo, number) not in self.issues:
            raise GitHubError("GitHub request failed: HTTP 404: Not Found")
        author = token.removeprefix("tok-") if token.startswith("tok-") else "unknown"
        comment = {
            "html_url": f"https://github.test/{repo}/issues/{number}#issuecomment-{len(self.comments.get((repo, number), [])) + 1}",
            "user": {"login": author},
            "created_at": "2026-11-04T12:00:00Z",
            "body": body,
        }
        self.comments.setdefault((repo, number), []).append(comment)
        self.issue_calls.append(
            {
                "operation": "create_issue_comment",
                "token": token,
                "repo": repo,
                "number": number,
                "body": body,
            }
        )
        return dict(comment)

    def issue_events(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
        return []

    def issue_comments(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
        self.issue_calls.append(
            {"operation": "issue_comments", "token": token, "repo": repo, "number": number}
        )
        return [dict(comment) for comment in self.comments.get((repo, number), [])]
