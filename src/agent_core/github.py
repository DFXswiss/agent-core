"""GitHub OAuth and open-PR search. Tests inject FakeGitHub."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Config

GITHUB_SEARCH_URL = "https://api.github.com/search/issues"
GITHUB_API_URL = "https://api.github.com"
REPOSITORY_OWNER_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
REPOSITORY_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubError(ValueError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def valid_repository_name(repo: object) -> bool:
    if not isinstance(repo, str):
        return False
    parts = repo.split("/")
    if len(parts) != 2:
        return False
    owner, name = parts
    return (
        REPOSITORY_OWNER_RE.fullmatch(owner) is not None
        and REPOSITORY_NAME_RE.fullmatch(name) is not None
        and name not in {".", ".."}
    )


def _repository_path(repo: str) -> str:
    if not valid_repository_name(repo):
        raise GitHubError("invalid GitHub repository name")
    return f"/repos/{repo}"


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

    def recent_issues_by_creator(
        self, token: str, repo: str, creator: str
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def repository(self, token: str, repo: str) -> dict[str, Any]:
        raise NotImplementedError

    def issue(self, token: str, repo: str, number: int) -> dict[str, Any]:
        raise NotImplementedError

    def add_assignees(self, token: str, repo: str, number: int, assignees: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    def create_issue_comment(self, token: str, repo: str, number: int, body: str) -> dict[str, Any]:
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
            raise GitHubError(
                f"token exchange failed: HTTP {token_resp.status_code}",
                status_code=token_resp.status_code,
            )
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
            raise GitHubError(
                f"user lookup failed: HTTP {user_resp.status_code}",
                status_code=user_resp.status_code,
            )
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
            raise GitHubError(
                f"PR search failed: HTTP {resp.status_code}", status_code=resp.status_code
            )
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
        result, _headers = self._request_json_with_headers(
            method,
            path,
            token,
            json_body=json_body,
            params=params,
            expected=expected,
        )
        return result

    def _request_json_with_headers(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[Any, dict[str, str]]:
        try:
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
                # GitHub answers a renamed or transferred repository with 301 (GET) or
                # 307 (writes); httpx drops Authorization if a redirect leaves the host.
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from exc
        if resp.status_code not in expected:
            message = ""
            try:
                raw_message = resp.json().get("message")
                if isinstance(raw_message, str):
                    message = ": " + raw_message[:500]
            except Exception:
                pass
            raise GitHubError(
                f"GitHub request failed: HTTP {resp.status_code}{message}",
                status_code=resp.status_code,
            )
        try:
            result = resp.json()
        except Exception as exc:
            raise GitHubError(
                f"GitHub request failed: HTTP {resp.status_code} returned invalid JSON",
                status_code=resp.status_code,
            ) from exc
        raw_headers = getattr(resp, "headers", {})
        headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
        return result, headers

    def create_issue(
        self, token: str, repo: str, title: str, body: str, assignees: list[str]
    ) -> dict[str, Any]:
        repository_path = _repository_path(repo)
        result = self._request_json(
            "POST",
            f"{repository_path}/issues",
            token,
            json_body={"title": title, "body": body, "assignees": assignees},
            expected=(201,),
        )
        if not isinstance(result, dict):
            raise GitHubError("GitHub issue creation returned an invalid response")
        return result

    def recent_issues_by_creator(
        self, token: str, repo: str, creator: str
    ) -> list[dict[str, Any]]:
        repository_path = _repository_path(repo)
        result = self._request_json(
            "GET",
            f"{repository_path}/issues",
            token,
            params={
                "creator": creator,
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 30,
            },
        )
        if not isinstance(result, list):
            raise GitHubError("GitHub recent issue lookup returned an invalid response")
        return [item for item in result if isinstance(item, dict)]

    def repository(self, token: str, repo: str) -> dict[str, Any]:
        result = self._request_json("GET", _repository_path(repo), token)
        if not isinstance(result, dict):
            raise GitHubError("GitHub repository lookup returned an invalid response")
        return result

    def issue(self, token: str, repo: str, number: int) -> dict[str, Any]:
        result = self._request_json(
            "GET", f"{_repository_path(repo)}/issues/{number}", token
        )
        if not isinstance(result, dict):
            raise GitHubError("GitHub issue lookup returned an invalid response")
        return result

    def add_assignees(self, token: str, repo: str, number: int, assignees: list[str]) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"{_repository_path(repo)}/issues/{number}/assignees",
            token,
            json_body={"assignees": assignees},
            expected=(200, 201),
        )
        if not isinstance(result, dict):
            raise GitHubError("GitHub assignee update returned an invalid response")
        return result

    def create_issue_comment(self, token: str, repo: str, number: int, body: str) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"{_repository_path(repo)}/issues/{number}/comments",
            token,
            json_body={"body": body},
            expected=(201,),
        )
        if not isinstance(result, dict):
            raise GitHubError("GitHub comment creation returned an invalid response")
        return result

    def issue_comments(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
        path = f"{_repository_path(repo)}/issues/{number}/comments"
        first, headers = self._request_json_with_headers(
            "GET", path, token, params={"per_page": 100}
        )
        first_items = _comment_page_items(first)
        last_page = _link_page(headers.get("link", ""), "last")
        if last_page is None or last_page <= 1:
            return first_items

        last, last_headers = self._request_json_with_headers(
            "GET", path, token, params={"per_page": 100, "page": last_page}
        )
        last_items = _comment_page_items(last)
        if len(last_items) >= 20:
            return last_items

        previous_page = _link_page(last_headers.get("link", ""), "prev")
        if previous_page is None:
            previous_page = last_page - 1
        if previous_page < 1:
            return last_items
        previous = self._request_json(
            "GET", path, token, params={"per_page": 100, "page": previous_page}
        )
        previous_items = _comment_page_items(previous)
        return previous_items + last_items


def _comment_page_items(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, list):
        raise GitHubError("GitHub comment lookup returned an invalid response")
    return [item for item in body if isinstance(item, dict)]


def _link_page(link_header: str, relation: str) -> int | None:
    for raw_link in link_header.split(","):
        parts = [part.strip() for part in raw_link.split(";")]
        if not parts or not (parts[0].startswith("<") and parts[0].endswith(">")):
            continue
        relations: set[str] = set()
        for parameter in parts[1:]:
            if parameter.startswith("rel="):
                relations.update(parameter[4:].strip('"').split())
        if relation not in relations:
            continue
        pages = parse_qs(urlparse(parts[0][1:-1]).query).get("page", [])
        if pages and pages[-1].isdigit() and int(pages[-1]) > 0:
            return int(pages[-1])
    return None


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
        self.canonical_repos: dict[str, str] = {}

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return f"https://github.test/login/oauth/authorize?state={state}&redirect_uri={redirect_uri}"

    def login_for_code(self, code: str) -> GitHubUser:
        if code not in self._codes:
            raise GitHubError(f"unknown authorization code: {code}")
        login = self._codes[code]
        return GitHubUser(login=login, token="tok-" + login)

    def search_open_prs(self, token: str, logins: list[str]) -> list[dict[str, Any]]:
        if self.search_status in (401, 403):
            raise GitHubError(
                f"PR search failed: HTTP {self.search_status}", status_code=self.search_status
            )
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
        canonical_repo = self.canonical_repos.get(repo.lower(), repo)
        number = 1 + max(
            (n for r, n in self.issues if r.lower() == canonical_repo.lower()), default=0
        )
        assigned = [] if self.omit_assignees_on_create else [{"login": a} for a in assignees]
        issue = {
            "number": number,
            "html_url": f"https://github.test/{canonical_repo}/issues/{number}",
            "repository_url": f"https://api.github.test/repos/{canonical_repo}",
            "state": "open",
            "title": title,
            "body": body,
            "assignees": assigned,
            "creator": token.removeprefix("tok-") if token.startswith("tok-") else "unknown",
        }
        self.issues[(canonical_repo, number)] = issue
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

    def recent_issues_by_creator(
        self, token: str, repo: str, creator: str
    ) -> list[dict[str, Any]]:
        self.issue_calls.append(
            {
                "operation": "recent_issues_by_creator",
                "token": token,
                "repo": repo,
                "creator": creator,
            }
        )
        canonical_repo = self.canonical_repos.get(repo.lower(), repo)
        rows = [
            issue
            for (issue_repo, _number), issue in self.issues.items()
            if issue_repo.lower() == canonical_repo.lower() and issue.get("creator") == creator
        ]
        rows.sort(key=lambda issue: int(issue.get("number", 0)), reverse=True)
        return [dict(issue) for issue in rows[:30]]

    def repository(self, token: str, repo: str) -> dict[str, Any]:
        canonical_repo = self.canonical_repos.get(repo.lower(), repo)
        self.issue_calls.append({"operation": "repository", "token": token, "repo": repo})
        return {"full_name": canonical_repo}

    def issue(self, token: str, repo: str, number: int) -> dict[str, Any]:
        self.issue_calls.append({"operation": "issue", "token": token, "repo": repo, "number": number})
        canonical_repo = self.canonical_repos.get(repo.lower(), repo)
        issue = self.issues.get((canonical_repo, number))
        if issue is None:
            raise GitHubError("GitHub request failed: HTTP 404: Not Found", status_code=404)
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
        canonical_repo = self.canonical_repos.get(repo.lower(), repo)
        issue = self.issues.get((canonical_repo, number))
        if issue is None:
            raise GitHubError("GitHub request failed: HTTP 404: Not Found", status_code=404)
        if not self.omit_assignees_after_add:
            issue["assignees"] = [{"login": a} for a in assignees]
        return dict(issue)

    def create_issue_comment(self, token: str, repo: str, number: int, body: str) -> dict[str, Any]:
        canonical_repo = self.canonical_repos.get(repo.lower(), repo)
        if (canonical_repo, number) not in self.issues:
            raise GitHubError("GitHub request failed: HTTP 404: Not Found", status_code=404)
        author = token.removeprefix("tok-") if token.startswith("tok-") else "unknown"
        comment_number = len(self.comments.get((canonical_repo, number), [])) + 1
        comment = {
            "html_url": (
                f"https://github.test/{canonical_repo}/issues/{number}"
                f"#issuecomment-{comment_number}"
            ),
            "user": {"login": author},
            "created_at": "2026-11-04T12:00:00Z",
            "body": body,
        }
        self.comments.setdefault((canonical_repo, number), []).append(comment)
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

    def issue_comments(self, token: str, repo: str, number: int) -> list[dict[str, Any]]:
        self.issue_calls.append(
            {"operation": "issue_comments", "token": token, "repo": repo, "number": number}
        )
        canonical_repo = self.canonical_repos.get(repo.lower(), repo)
        if (canonical_repo, number) not in self.issues:
            raise GitHubError("GitHub request failed: HTTP 404: Not Found", status_code=404)
        comments = self.comments.get((canonical_repo, number), [])
        if not comments:
            return []
        last_page_start = ((len(comments) - 1) // 100) * 100
        if len(comments) - last_page_start < 20 and last_page_start > 0:
            last_page_start = max(0, last_page_start - 100)
        return [dict(comment) for comment in comments[last_page_start:]]
