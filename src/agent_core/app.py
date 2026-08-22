"""HTTP and WebSocket hub."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import Config
from .db import Store, dumps, loads, row_dict, utcnow
from .github import GitHub, GitHubError, RealGitHub
from .teams import load_teams, visible_logins
from .tokens import TokenError, issue, parse

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED_TABLES = frozenset(
    {
        "session",
        "activity",
        "task",
        "task_round",
        "agent",
        "checklist_item",
        "local_check",
        "review_gate",
        "open_work",
        "ping",
    }
)
ALLOWED_OPS = frozenset({"insert", "update", "delete"})
PING_KINDS = frozenset({"review-request", "ping", "question"})
CONTROL_ACTIONS = frozenset({"start", "stop", "input", "resize"})
CONTROL_KEYS = frozenset({"enter", "ctrl-c", "tab"})
PAIR_TTL_SECONDS = 600
TERMINAL_RING_SIZE = 64
TERMINAL_CHUNK_MAX = 8192
MATCH_PATHS = frozenset({"type", "payload.repo", "payload.issue_key", "payload.to_session"})
MAX_SUBSCRIPTIONS = 32
MAX_MATCH_PREDICATES = 4
MAX_IN_VALUES = 16
QUERY_ROW_CAP = 500
_MISSING = object()


class Hub:
    def __init__(self, cfg: Config, github: GitHub, store: Store, teams: dict[str, list[str]]) -> None:
        self.cfg = cfg
        self.github = github
        self.store = store
        self.teams = teams
        self.queues: list[tuple[str, asyncio.Queue[dict[str, Any]]]] = []
        self.device_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.control_ready: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.terminal_rings: dict[str, list[dict[str, Any]]] = {}
        self.terminal_queues: list[tuple[str, str, asyncio.Queue[dict[str, Any]]]] = []
        self.github_tokens: dict[str, str] = {}
        for login, token in self.store.all_oauth_tokens():
            self.github_tokens[login] = token

    def visible(self, login: str) -> set[str]:
        return visible_logins(login, self.teams)

    def device_for_token(self, raw: str) -> dict[str, Any]:
        try:
            parsed = parse(self.cfg.session_secret, raw)
        except TokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        row = self.store.query_one(
            "SELECT * FROM device WHERE id = ? AND token_id = ?",
            (parsed.device_id, parsed.token_id),
        )
        if row is None:
            raise HTTPException(status_code=401, detail="unknown device token")
        if row["revoked_at"] is not None:
            raise HTTPException(status_code=401, detail="device token is revoked")
        if row["github_login"] != parsed.login:
            raise HTTPException(status_code=401, detail="device token login does not match")
        return row_dict(row)

    def session_login(self, request: Request) -> str:
        login = request.session.get("login")
        if not isinstance(login, str) or login == "":
            raise HTTPException(status_code=401, detail="not signed in")
        return login

    def _allowed_for(self, login: str, event: dict[str, Any]) -> bool:
        visible = self.visible(login)
        if event.get("to") == login or event.get("from") == login:
            return True
        owner = event.get("login")
        if isinstance(owner, str):
            return owner in visible
        return False

    async def publish(self, event: dict[str, Any]) -> None:
        dead: list[tuple[str, asyncio.Queue[dict[str, Any]]]] = []
        for item in list(self.queues):
            login, queue = item
            if not self._allowed_for(login, event):
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(item)
        for item in dead:
            if item in self.queues:
                self.queues.remove(item)

    def publish_terminal(self, event: dict[str, Any]) -> None:
        session_id = event.get("session_id")
        dead: list[tuple[str, str, asyncio.Queue[dict[str, Any]]]] = []
        for item in list(self.terminal_queues):
            _login, sid, queue = item
            if sid != session_id:
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(item)
        for item in dead:
            if item in self.terminal_queues:
                self.terminal_queues.remove(item)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization")
    if header is None or not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer token is required")
    token = header[7:].strip()
    if token == "":
        raise HTTPException(status_code=401, detail="Authorization Bearer token is required")
    return token


def can_control(hub: Hub, login: str, origin_device_id: str) -> bool:
    if not isinstance(origin_device_id, str) or origin_device_id == "":
        return False
    row = hub.store.query_one(
        "SELECT id FROM device WHERE id = ? AND revoked_at IS NULL AND github_login = ?",
        (origin_device_id, login),
    )
    return row is not None


def create_app(cfg: Config, github: GitHub | None = None, store: Store | None = None) -> FastAPI:
    teams = load_teams(cfg.teams_path)
    hub = Hub(cfg, github or RealGitHub(cfg), store or Store(cfg.database), teams)
    app = FastAPI(title="agent-core", version="0.1.0")
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        same_site="lax",
        https_only=cfg.public_url.startswith("https://"),
    )
    app.state.hub = hub

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/auth/github")
    def auth_start(request: Request, next: str | None = None) -> RedirectResponse:
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        if next is not None:
            if not next.startswith("/pair?"):
                raise HTTPException(status_code=400, detail="next must be a /pair query")
            request.session["after_login"] = next
        redirect_uri = f"{cfg.public_url}/auth/github/callback"
        return RedirectResponse(hub.github.authorize_url(state, redirect_uri), status_code=302)

    @app.get("/auth/github/callback")
    def auth_callback(request: Request, code: str | None = None, state: str | None = None) -> RedirectResponse:
        expect = request.session.get("oauth_state")
        if not isinstance(expect, str) or expect == "" or state != expect:
            raise HTTPException(status_code=400, detail="oauth state mismatch")
        if not isinstance(code, str) or code == "":
            raise HTTPException(status_code=400, detail="oauth code is missing")
        try:
            user = hub.github.login_for_code(code)
        except GitHubError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        request.session.pop("oauth_state", None)
        request.session["login"] = user.login
        if user.token:
            hub.github_tokens[user.login] = user.token
            hub.store.put_oauth_token(user.login, user.token)
        dest = request.session.pop("after_login", "/")
        if not isinstance(dest, str) or not dest.startswith("/"):
            dest = "/"
        return RedirectResponse(dest, status_code=302)

    @app.post("/auth/logout")
    def auth_logout(request: Request) -> JSONResponse:
        login = request.session.get("login")
        if isinstance(login, str) and login:
            hub.store.delete_oauth_token(login)
            hub.github_tokens.pop(login, None)
        request.session.clear()
        return JSONResponse({"ok": True})

    @app.get("/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        return {"login": login, "visible": sorted(hub.visible(login)), "teams": _teams_for(login, hub.teams)}

    @app.post("/pair/prepare")
    def pair_prepare(body: dict[str, Any]) -> dict[str, str]:
        device_id = _require_str(body, "device_id")
        challenge = _require_str(body, "challenge")
        name = _require_str(body, "device_name")
        if len(challenge) < 16 or len(challenge) > 128:
            raise HTTPException(status_code=400, detail="challenge length is invalid")
        if len(name) > 128 or len(device_id) > 80:
            raise HTTPException(status_code=400, detail="device fields are too long")
        existing = hub.store.query_one("SELECT id FROM device WHERE id = ?", (device_id,))
        if existing is not None:
            raise HTTPException(status_code=409, detail="device is already paired")
        hub.store.execute(
            "DELETE FROM pending_pair WHERE created_at < ?",
            (_cutoff(),),
        )
        pending = hub.store.query_one("SELECT COUNT(*) AS n FROM pending_pair")
        if pending is not None and int(pending["n"]) >= 200:
            raise HTTPException(status_code=429, detail="too many pending pairing challenges")
        hub.store.execute(
            "INSERT OR REPLACE INTO pending_pair (challenge, device_id, device_name, created_at, token, github_login) "
            "VALUES (?, ?, ?, ?, NULL, NULL)",
            (challenge, device_id, name, utcnow()),
        )
        return {"challenge": challenge, "pair_url": f"{cfg.public_url}/pair?challenge={challenge}"}

    @app.get("/pair/wait")
    def pair_wait(device_id: str, challenge: str) -> dict[str, Any]:
        claimed = hub.store.claim_pair_token(challenge, device_id, _cutoff())
        if claimed is not None:
            return {"status": "paired", "token": claimed[0], "login": claimed[1]}
        row = hub.store.query_one(
            "SELECT * FROM pending_pair WHERE challenge = ? AND device_id = ?",
            (challenge, device_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="unknown pairing challenge")
        if _age_seconds(row["created_at"]) > PAIR_TTL_SECONDS:
            raise HTTPException(status_code=410, detail="pairing challenge expired")
        return {"status": "pending"}

    @app.post("/pair/confirm")
    def pair_confirm(request: Request, body: dict[str, Any]) -> dict[str, str]:
        login = hub.session_login(request)
        challenge = _require_str(body, "challenge")
        pending = hub.store.query_one("SELECT * FROM pending_pair WHERE challenge = ?", (challenge,))
        if pending is None:
            raise HTTPException(status_code=404, detail="unknown pairing challenge")
        if pending["token"] is not None:
            raise HTTPException(status_code=409, detail="challenge is already paired")
        created = pending["created_at"]
        if _age_seconds(created) > PAIR_TTL_SECONDS:
            raise HTTPException(status_code=410, detail="pairing challenge expired")
        token_id = str(uuid.uuid4())
        token = issue(cfg.session_secret, login, pending["device_id"], token_id)
        hub.store.execute(
            "INSERT INTO device (id, github_login, name, token_id, created_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
            (pending["device_id"], login, pending["device_name"], token_id, utcnow()),
        )
        hub.store.execute(
            "UPDATE pending_pair SET token = ?, github_login = ? WHERE challenge = ?",
            (token, login, challenge),
        )
        return {"status": "paired", "login": login, "device_id": pending["device_id"]}

    @app.get("/pair")
    def pair_page() -> FileResponse:
        page = STATIC / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="dashboard is missing")
        return FileResponse(page)

    @app.get("/")
    def index() -> FileResponse:
        page = STATIC / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="dashboard is missing")
        return FileResponse(page)

    @app.post("/sync/push")
    async def sync_push(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        events = body.get("events")
        if not isinstance(events, list):
            raise HTTPException(status_code=400, detail="events must be a list")
        accepted = 0
        notify_to: set[str] = set()
        materialized: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                raise HTTPException(status_code=400, detail="event must be an object")
            notify, snap = _accept_event(hub, device, event)
            notify_to.update(notify)
            if snap is not None:
                materialized.append(snap)
            accepted += 1
        if accepted:
            await hub.publish({"type": "events", "login": device["github_login"], "count": accepted})
            for target in notify_to:
                await hub.publish(
                    {
                        "type": "events",
                        "login": device["github_login"],
                        "to": target,
                        "count": accepted,
                    }
                )
            for snap in materialized:
                _enqueue_subscription_fanout(hub, snap)
        return {"accepted": accepted}

    @app.get("/sync/pull")
    def sync_pull(request: Request) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        cursors = _cursors(request)
        return {
            "events": _own_events(hub, device["id"], cursors),
            "inbox": _inbox_snapshots(hub, device["id"]),
            "pings": _ping_snapshots(hub, device["github_login"]),
            "subscriptions": _subscription_snapshots(hub, device),
        }

    @app.get("/sync/restore")
    def sync_restore(request: Request) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        own = _own_events(hub, device["id"], {})
        return {
            "device_id": device["id"],
            "login": device["github_login"],
            "own_events": own,
            "inbox": _inbox_snapshots(hub, device["id"]),
            "pings": _ping_snapshots(hub, device["github_login"]),
        }

    @app.put("/sync/subscriptions")
    def sync_subscriptions_put(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        raw = body.get("subscriptions")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="subscriptions must be a list")
        if len(raw) > MAX_SUBSCRIPTIONS:
            raise HTTPException(status_code=400, detail="at most 32 subscriptions per device")
        match_jsons: list[str] = []
        for item in raw:
            if not isinstance(item, dict) or set(item.keys()) != {"match"}:
                raise HTTPException(status_code=400, detail="each subscription must be an object with exactly one key match")
            match_jsons.append(dumps(_validate_match(item["match"])))
        hub.store.replace_device_subscriptions(device["id"], match_jsons)
        return _subscriptions_response(hub, device["id"])

    @app.get("/sync/subscriptions")
    def sync_subscriptions_get(request: Request) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        return _subscriptions_response(hub, device["id"])

    @app.post("/sync/query")
    def sync_query(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        if not isinstance(body, dict) or "match" not in body:
            raise HTTPException(status_code=400, detail="match is required")
        match = _validate_match(body["match"])
        return {"rows": _query_matching_rows(hub, device["github_login"], match)}

    @app.get("/api/prs")
    def api_prs(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        allowed = sorted(hub.visible(login))
        token = hub.github_tokens.get(login, "")
        if token == "":
            token = hub.store.get_oauth_token(login)
            if token:
                hub.github_tokens[login] = token
        if token == "":
            return {"generated_at": utcnow(), "prs": [], "source": "none"}
        try:
            prs = hub.github.search_open_prs(token, allowed)
        except GitHubError as exc:
            text = str(exc)
            if "HTTP 401" in text or "HTTP 403" in text:
                hub.store.delete_oauth_token(login)
                hub.github_tokens.pop(login, None)
                return {"generated_at": utcnow(), "prs": [], "source": "none"}
            raise HTTPException(status_code=502, detail=text) from exc
        truncated = len(prs) >= 50
        return {"generated_at": utcnow(), "prs": prs, "source": "github", "truncated": truncated}

    @app.get("/api/state")
    def api_state(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        allowed = hub.visible(login)
        tables = {
            "session": "session",
            "activity": "activity",
            "task": "task",
            "task_round": "task_round",
            "agent": "agent",
            "checklist": "checklist_item",
            "checks": "local_check",
            "gates": "review_gate",
            "work": "open_work",
            "pings": "ping",
        }
        out: dict[str, Any] = {
            "generated_at": utcnow(),
            "login": login,
            "visible": sorted(allowed),
            "teams": _teams_for(login, hub.teams),
        }
        for key, table in tables.items():
            rows = hub.store.query(
                "SELECT payload, github_login, origin_device_id FROM row_replica "
                "WHERE table_name = ? AND github_login IN ({})".format(_placeholders(allowed)),
                (table, *sorted(allowed)),
            )
            items = [_payload_with_origin(r) for r in rows]
            if key == "session":
                for item in items:
                    origin = item.get("_origin_device_id")
                    origin_s = origin if isinstance(origin, str) else ""
                    item["can_control"] = can_control(hub, login, origin_s)
                    item["control_connected"] = origin_s in hub.control_ready
            out[key] = items
        devices = hub.store.query(
            "SELECT id, github_login, name, created_at, revoked_at FROM device "
            "WHERE github_login IN ({})".format(_placeholders(allowed)),
            tuple(sorted(allowed)),
        )
        out["devices"] = [row_dict(r) for r in devices]
        return out

    @app.get("/api/sessions/{session_id}")
    def api_session_detail(request: Request, session_id: str) -> dict[str, Any]:
        login = hub.session_login(request)
        row = _visible_session_row(hub, login, session_id)
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            raise HTTPException(status_code=500, detail="replica payload is not an object")
        origin = row["origin_device_id"]
        return {
            "id": session_id,
            "payload": payload,
            "_github_login": row["github_login"],
            "_origin_device_id": origin,
            "can_control": can_control(hub, login, origin),
            "control_connected": origin in hub.control_ready,
        }

    @app.post("/api/sessions/{session_id}/control")
    async def api_session_control(request: Request, session_id: str, body: dict[str, Any]) -> JSONResponse:
        login = _any_login(hub, request)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        action, payload = _parse_control_body(body)
        row = _visible_session_row(hub, login, session_id)
        origin = row["origin_device_id"]
        if not can_control(hub, login, origin):
            raise HTTPException(status_code=403, detail="not the owning device")
        queue = hub.control_ready.get(origin)
        if queue is None:
            raise HTTPException(status_code=409, detail="owning device is not control-connected")
        frame = {
            "type": "control",
            "session_id": session_id,
            "action": action,
            "payload": payload,
        }
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull as exc:
            raise HTTPException(status_code=503, detail="control queue is full") from exc
        return JSONResponse({"queued": True}, status_code=202)

    @app.get("/api/sessions/{session_id}/terminal")
    async def api_session_terminal(request: Request, session_id: str):
        login = hub.session_login(request)
        _visible_session_row(hub, login, session_id)
        accept = request.headers.get("accept") or ""
        if "text/event-stream" in accept:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
            hub.terminal_queues.append((login, session_id, queue))

            async def gen():
                try:
                    yield f"data: {dumps({'type': 'hello', 'session_id': session_id})}\n\n"
                    for chunk in list(hub.terminal_rings.get(session_id, [])):
                        yield (
                            f"data: {dumps({'type': 'terminal', 'session_id': session_id, 'seq': chunk['seq'], 'data': chunk['data']})}\n\n"
                        )
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=20.0)
                        except asyncio.TimeoutError:
                            yield ":\n\n"
                            continue
                        yield f"data: {dumps(item)}\n\n"
                finally:
                    item = (login, session_id, queue)
                    if item in hub.terminal_queues:
                        hub.terminal_queues.remove(item)

            return StreamingResponse(gen(), media_type="text/event-stream")
        chunks = list(hub.terminal_rings.get(session_id, []))
        return {"session_id": session_id, "chunks": chunks}

    @app.post("/api/pings")
    async def api_ping(request: Request, body: dict[str, Any]) -> dict[str, str]:
        login = hub.session_login(request)
        target = _require_str(body, "to").lower()
        kind = _require_str(body, "kind")
        if kind not in PING_KINDS:
            raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(sorted(PING_KINDS))}")
        if target not in hub.visible(login):
            raise HTTPException(status_code=403, detail="target is outside your teams")
        note = body.get("body")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise HTTPException(status_code=400, detail="body must be a string")
        task_id = body.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise HTTPException(status_code=400, detail="task_id must be a string")
        ping_id = str(uuid.uuid4())
        payload = {
            "id": ping_id,
            "from_login": login,
            "to_login": target,
            "kind": kind,
            "task_id": task_id,
            "body": note,
            "created_at": utcnow(),
            "acked_at": None,
        }
        _record_web_event(hub, login, "ping", "insert", ping_id, payload)
        await hub.publish({"type": "ping", "from": login, "to": target, "id": ping_id, "login": login})
        return {"id": ping_id}

    @app.post("/api/pings/{ping_id}/ack")
    async def api_ping_ack(request: Request, ping_id: str) -> dict[str, Any]:
        login = _any_login(hub, request)
        row = hub.store.query_one(
            "SELECT * FROM row_replica WHERE table_name = 'ping' AND row_id = ?",
            (ping_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="unknown ping")
        payload = loads(row["payload"])
        if payload.get("to_login") != login:
            raise HTTPException(status_code=403, detail="only the recipient can ack")
        payload["acked_at"] = utcnow()
        _record_web_event(hub, login, "ping", "update", ping_id, payload)
        owner = row["github_login"]
        await hub.publish(
            {
                "type": "ping",
                "from": payload.get("from_login"),
                "to": login,
                "id": ping_id,
                "login": owner,
            }
        )
        return {"id": ping_id, "acked_at": payload["acked_at"], "payload": payload}

    @app.get("/api/stream")
    async def api_stream(request: Request):
        login = hub.session_login(request)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        hub.queues.append((login, queue))

        async def gen():
            try:
                yield f"data: {dumps({'type': 'hello', 'login': login})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield ":\n\n"
                        continue
                    yield f"data: {dumps(item)}\n\n"
            finally:
                item = (login, queue)
                if item in hub.queues:
                    hub.queues.remove(item)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.websocket("/sync/ws")
    async def sync_ws(ws: WebSocket) -> None:
        token = ws.query_params.get("token")
        if not isinstance(token, str) or token == "":
            await ws.close(code=4401)
            return
        try:
            device = hub.device_for_token(token)
        except HTTPException:
            await ws.close(code=4401)
            return
        await ws.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        login = device["github_login"]
        device_id = device["id"]
        hub.queues.append((login, queue))
        hub.device_queues[device_id] = queue
        out_task: asyncio.Task[None] | None = None
        try:
            await ws.send_json({"type": "hello", "login": login, "device_id": device_id})

            async def pump_out() -> None:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        await ws.send_json({"type": "ping"})
                        continue
                    await ws.send_json(item)

            out_task = asyncio.create_task(pump_out())
            while True:
                try:
                    raw = await ws.receive_json()
                except WebSocketDisconnect:
                    raise
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                msg_type = raw.get("type")
                if msg_type == "control-ready":
                    hub.control_ready[device_id] = queue
                elif msg_type == "terminal":
                    _handle_terminal(hub, device, raw)
                elif msg_type == "control-ack":
                    continue
        except WebSocketDisconnect:
            pass
        finally:
            if out_task is not None:
                out_task.cancel()
                try:
                    await out_task
                except (asyncio.CancelledError, Exception):
                    pass
            if hub.control_ready.get(device_id) is queue:
                hub.control_ready.pop(device_id, None)
            if hub.device_queues.get(device_id) is queue:
                hub.device_queues.pop(device_id, None)
            item = (login, queue)
            if item in hub.queues:
                hub.queues.remove(item)

    return app


def _cutoff() -> str:
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(seconds=PAIR_TTL_SECONDS)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cursors(request: Request) -> dict[str, int]:
    out: dict[str, int] = {}
    for raw in request.query_params.getlist("cursor"):
        if ":" not in raw:
            raise HTTPException(status_code=400, detail="cursor must be origin_id:seq")
        origin, seq = raw.rsplit(":", 1)
        if origin == "" or not seq.isdigit():
            raise HTTPException(status_code=400, detail="cursor must be origin_id:seq")
        out[origin] = int(seq)
    return out


def _own_events(hub: Hub, device_id: str, cursors: dict[str, int]) -> list[dict[str, Any]]:
    after = cursors.get(device_id, 0)
    rows = hub.store.query(
        "SELECT e.* FROM ledger_event e WHERE e.origin_device_id = ? AND e.origin_seq > ? ORDER BY e.origin_seq ASC",
        (device_id, after),
    )
    return [_event_out(r) for r in rows]


def _owned_session_ids(hub: Hub, device_id: str) -> set[str]:
    rows = hub.store.query(
        "SELECT row_id FROM row_replica WHERE table_name = 'session' AND origin_device_id = ?",
        (device_id,),
    )
    return {row["row_id"] for row in rows}


def _message_to_session(payload: dict[str, Any]) -> str | None:
    if payload.get("type") != "message":
        return None
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return None
    target = inner.get("to_session")
    if not isinstance(target, str) or target == "":
        return None
    return target


def _inbox_snapshots(hub: Hub, device_id: str) -> list[dict[str, Any]]:
    owned = _owned_session_ids(hub, device_id)
    if not owned:
        return []
    out: list[dict[str, Any]] = []
    parent_ids: set[str] = set()
    for row in hub.store.query("SELECT * FROM row_replica WHERE table_name = 'activity'"):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        to_session = _message_to_session(payload)
        if to_session is None or to_session not in owned:
            continue
        out.append(_replica_out(row))
        parent_id = payload.get("session_id")
        if isinstance(parent_id, str) and parent_id != "":
            parent_ids.add((parent_id, row["origin_device_id"]))
    for parent_id, origin in sorted(parent_ids):
        parent = hub.store.query_one(
            "SELECT * FROM row_replica WHERE table_name = 'session' AND row_id = ?",
            (parent_id,),
        )
        if parent is not None and parent["origin_device_id"] == origin:
            out.append(_replica_out(parent))
    return out


def _ping_snapshots(hub: Hub, login: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in hub.store.query("SELECT * FROM row_replica WHERE table_name = 'ping'"):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if payload.get("from_login") == login or payload.get("to_login") == login:
            out.append(_replica_out(row))
    return out


def _record_web_event(
    hub: Hub,
    login: str,
    table: str,
    op: str,
    row_id: str,
    payload: dict[str, Any],
) -> None:
    origin = f"web:{login}"
    last = hub.store.query_one(
        "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = ?",
        (origin,),
    )
    seq = int(last["m"]) + 1 if last is not None else 1
    encoded = dumps(payload)
    occurred = utcnow()
    hub.store.execute(
        "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (origin, seq, table, op, row_id, encoded, occurred, occurred),
    )
    if op == "delete":
        hub.store.execute("DELETE FROM row_replica WHERE table_name = ? AND row_id = ?", (table, row_id))
        return
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(table_name, row_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
        (table, row_id, origin, login, encoded, occurred),
    )


def _any_login(hub: Hub, request: Request) -> str:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return hub.device_for_token(header[7:].strip())["github_login"]
    return hub.session_login(request)


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value.strip()


def _require_int_string(name: str, value: str) -> int:
    if not value.isdigit() and not (value.startswith("-") and value[1:].isdigit()):
        raise HTTPException(status_code=400, detail=f"{name} must be an integer")
    return int(value)


def _placeholders(items: set[str]) -> str:
    if not items:
        raise HTTPException(status_code=500, detail="visibility set is empty")
    return ",".join("?" for _ in items)


def _teams_for(login: str, teams: dict[str, list[str]]) -> list[str]:
    self = login.lower()
    return sorted(name for name, members in teams.items() if self in members)


def _age_seconds(created_at: str) -> float:
    from datetime import datetime, timezone

    stamp = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _event_out(row: Any) -> dict[str, Any]:
    return {
        "hub_seq": row["hub_seq"],
        "origin_device_id": row["origin_device_id"],
        "origin_seq": row["origin_seq"],
        "table": row["table_name"],
        "op": row["op"],
        "row_id": row["row_id"],
        "payload": loads(row["payload"]),
        "occurred_at": row["occurred_at"],
    }


def _replica_out(row: Any) -> dict[str, Any]:
    return {
        "table": row["table_name"],
        "row_id": row["row_id"],
        "origin_device_id": row["origin_device_id"],
        "github_login": row["github_login"],
        "payload": loads(row["payload"]),
        "updated_at": row["updated_at"],
    }


def _payload_with_origin(row: Any) -> dict[str, Any]:
    payload = loads(row["payload"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="replica payload is not an object")
    payload["_github_login"] = row["github_login"]
    payload["_origin_device_id"] = row["origin_device_id"]
    return payload


def _visible_session_row(hub: Hub, login: str, session_id: str) -> Any:
    row = hub.store.query_one(
        "SELECT * FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (session_id,),
    )
    if row is None or row["github_login"] not in hub.visible(login):
        raise HTTPException(status_code=404, detail="unknown session")
    return row


def _require_dim(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 500:
        raise HTTPException(status_code=400, detail=f"{key} must be an integer from 1 to 500")
    return value


def _optional_dim(body: dict[str, Any], key: str) -> int | None:
    if key not in body or body[key] is None:
        return None
    return _require_dim(body, key)


def _parse_control_body(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action = body.get("action")
    if not isinstance(action, str) or action not in CONTROL_ACTIONS:
        raise HTTPException(status_code=400, detail="action must be one of start, stop, input, resize")
    if action == "start":
        payload: dict[str, Any] = {}
        if "command" in body and body["command"] is not None:
            command = body["command"]
            if not isinstance(command, str) or len(command) < 1 or len(command) > 4000:
                raise HTTPException(status_code=400, detail="command must be a string of length 1..4000")
            payload["command"] = command
        if "provider" in body and body["provider"] is not None:
            provider = body["provider"]
            if provider != "grok":
                raise HTTPException(status_code=400, detail="provider must be grok")
            payload["provider"] = "grok"
        if "model" in body and body["model"] is not None:
            model = body["model"]
            if not isinstance(model, str) or len(model) < 1 or len(model) > 64:
                raise HTTPException(status_code=400, detail="model must be a string of length 1..64")
            payload["model"] = model
        if payload.get("provider") and payload.get("command"):
            raise HTTPException(status_code=400, detail="provider and command cannot both be set")
        if "model" in payload and payload.get("provider") != "grok":
            raise HTTPException(status_code=400, detail="model requires provider grok")
        cols = _optional_dim(body, "cols")
        rows = _optional_dim(body, "rows")
        if cols is not None:
            payload["cols"] = cols
        if rows is not None:
            payload["rows"] = rows
        return action, payload
    if action == "stop":
        return action, {}
    if action == "input":
        data = body.get("data")
        key = body.get("key")
        has_data = data is not None
        has_key = key is not None
        if has_data == has_key:
            raise HTTPException(status_code=400, detail="input requires exactly one of data or key")
        if has_data:
            if not isinstance(data, str):
                raise HTTPException(status_code=400, detail="data must be a string")
            byte_len = len(data.encode("utf-8"))
            if byte_len < 1 or byte_len > 4096:
                raise HTTPException(status_code=400, detail="data must be 1..4096 utf-8 bytes")
            return action, {"data": data}
        if not isinstance(key, str) or key not in CONTROL_KEYS:
            raise HTTPException(status_code=400, detail="key must be one of enter, ctrl-c, tab")
        return action, {"key": key}
    cols = body.get("cols")
    rows = body.get("rows")
    if cols is None or rows is None:
        raise HTTPException(status_code=400, detail="resize requires cols and rows")
    return action, {"cols": _require_dim(body, "cols"), "rows": _require_dim(body, "rows")}


def _handle_terminal(hub: Hub, device: dict[str, Any], raw: dict[str, Any]) -> None:
    session_id = raw.get("session_id")
    seq = raw.get("seq")
    data = raw.get("data")
    if not isinstance(session_id, str) or session_id == "":
        return
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return
    if not isinstance(data, str):
        return
    row = hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (session_id,),
    )
    if row is None:
        return
    if row["origin_device_id"] != device["id"]:
        return
    if len(data) > TERMINAL_CHUNK_MAX:
        data = data[:TERMINAL_CHUNK_MAX]
    ring = hub.terminal_rings.setdefault(session_id, [])
    ring.append({"seq": seq, "data": data})
    if len(ring) > TERMINAL_RING_SIZE:
        del ring[:-TERMINAL_RING_SIZE]
    event = {"type": "terminal", "session_id": session_id, "seq": seq, "data": data}
    hub.publish_terminal(event)


def _session_mail_notify(hub: Hub, device: dict[str, Any], table: str, op: str, payload: dict[str, Any]) -> list[str]:
    if table != "activity" or op not in ("insert", "update"):
        return []
    to_session = _message_to_session(payload)
    if to_session is None:
        if payload.get("type") == "message":
            raise HTTPException(status_code=400, detail="message activity requires payload.to_session")
        return []
    target = hub.store.query_one(
        "SELECT github_login FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (to_session,),
    )
    if target is None:
        raise HTTPException(status_code=404, detail="unknown session")
    owner = target["github_login"]
    if owner not in hub.visible(device["github_login"]):
        raise HTTPException(status_code=403, detail="target session is outside your teams")
    parent_id = payload.get("session_id")
    if not isinstance(parent_id, str) or parent_id == "":
        raise HTTPException(status_code=400, detail="message activity requires session_id")
    parent = hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (parent_id,),
    )
    if parent is None:
        raise HTTPException(status_code=400, detail="message activity session_id is unknown")
    if parent["origin_device_id"] != device["id"]:
        raise HTTPException(status_code=403, detail="message activity session_id is not owned by this device")
    return [owner]


def _validate_ping(hub: Hub, device: dict[str, Any], table: str, op: str, payload: dict[str, Any], replica: Any) -> None:
    if table != "ping":
        return
    login = device["github_login"]
    if op == "insert":
        if payload.get("from_login") != login:
            raise HTTPException(status_code=403, detail="from_login must be this device login")
        target = payload.get("to_login")
        if not isinstance(target, str) or target == "":
            raise HTTPException(status_code=400, detail="to_login is required")
        if target != target.lower():
            raise HTTPException(status_code=400, detail="to_login must be lowercase")
        if target not in hub.visible(login):
            raise HTTPException(status_code=403, detail="target is outside your teams")
        return
    if op != "update":
        return
    if replica is None:
        raise HTTPException(status_code=404, detail="unknown ping")
    previous = loads(replica["payload"])
    if not isinstance(previous, dict):
        return
    if payload.get("from_login") != previous.get("from_login") or payload.get("to_login") != previous.get("to_login"):
        raise HTTPException(status_code=403, detail="cannot retarget a ping")


def _reject_open_session_collision(hub: Hub, origin: str, table: str, op: str, row_id: str) -> None:
    if table != "session" or op != "insert":
        return
    seen = hub.store.query_one(
        "SELECT origin_seq FROM ledger_event WHERE table_name = 'session' AND row_id = ? LIMIT 1",
        (row_id,),
    )
    if seen is not None:
        raise HTTPException(status_code=409, detail="session id is already open")
    replica = hub.store.query_one(
        "SELECT origin_device_id, payload FROM row_replica WHERE table_name = 'session' AND row_id = ?",
        (row_id,),
    )
    if replica is None:
        return
    raise HTTPException(status_code=409, detail="session id is already open")


def _accept_event(
    hub: Hub, device: dict[str, Any], event: dict[str, Any]
) -> tuple[list[str], dict[str, Any] | None]:
    origin = event.get("origin_device_id")
    if origin != device["id"]:
        raise HTTPException(status_code=403, detail="cannot push events for another device")
    table = event.get("table")
    op = event.get("op")
    row_id = event.get("row_id")
    seq = event.get("origin_seq")
    payload = event.get("payload")
    occurred = event.get("occurred_at")
    if table not in ALLOWED_TABLES:
        raise HTTPException(status_code=400, detail=f"unknown table: {table}")
    if op not in ALLOWED_OPS:
        raise HTTPException(status_code=400, detail=f"unknown op: {op}")
    if not isinstance(row_id, str) or row_id == "":
        raise HTTPException(status_code=400, detail="row_id is required")
    if not isinstance(seq, int) or seq < 1:
        raise HTTPException(status_code=400, detail="origin_seq must be a positive integer")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    if not isinstance(occurred, str) or occurred == "":
        raise HTTPException(status_code=400, detail="occurred_at is required")
    existing = hub.store.query_one(
        "SELECT * FROM ledger_event WHERE origin_device_id = ? AND origin_seq = ?",
        (origin, seq),
    )
    encoded = dumps(payload)
    if existing is not None:
        if (
            existing["table_name"] != table
            or existing["op"] != op
            or existing["row_id"] != row_id
            or existing["payload"] != encoded
            or existing["occurred_at"] != occurred
        ):
            raise HTTPException(status_code=409, detail=f"origin_seq {seq} already exists with different content")
        return [], None
    notify = _session_mail_notify(hub, device, table, op, payload)
    _reject_open_session_collision(hub, origin, table, op, row_id)
    last = hub.store.query_one(
        "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = ?",
        (origin,),
    )
    last_seq = int(last["m"]) if last is not None else 0
    if seq != last_seq + 1:
        raise HTTPException(status_code=409, detail=f"origin_seq gap: have {last_seq}, got {seq}")
    replica = hub.store.query_one(
        "SELECT origin_device_id, payload FROM row_replica WHERE table_name = ? AND row_id = ?",
        (table, row_id),
    )
    _validate_ping(hub, device, table, op, payload, replica)
    ping_ack = False
    if table == "session" and op == "update" and replica is None:
        raise HTTPException(status_code=404, detail="unknown session")
    replica_encoded = encoded
    if table == "ping" and op == "update" and replica is not None:
        previous = loads(replica["payload"])
        ping_ack = previous.get("to_login") == device["github_login"] and previous.get("id") == payload.get("id")
        if ping_ack and isinstance(previous, dict):
            previous["acked_at"] = payload.get("acked_at")
            replica_encoded = dumps(previous)
    if replica is not None and replica["origin_device_id"] != origin and not ping_ack:
        raise HTTPException(status_code=403, detail="row belongs to another device")
    write_origin = replica["origin_device_id"] if ping_ack and replica is not None else origin
    hub.store.execute(
        "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (origin, seq, table, op, row_id, encoded, occurred, utcnow()),
    )
    if op == "delete":
        hub.store.execute(
            "DELETE FROM row_replica WHERE table_name = ? AND row_id = ?",
            (table, row_id),
        )
        return [], None
    updated_at = utcnow()
    if ping_ack:
        hub.store.execute(
            "UPDATE row_replica SET payload = ?, updated_at = ? WHERE table_name = ? AND row_id = ?",
            (replica_encoded, updated_at, table, row_id),
        )
        row = hub.store.query_one(
            "SELECT * FROM row_replica WHERE table_name = ? AND row_id = ?",
            (table, row_id),
        )
        return [], _replica_out(row) if row is not None else None
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(table_name, row_id) DO UPDATE SET origin_device_id = excluded.origin_device_id, "
        "github_login = excluded.github_login, payload = excluded.payload, updated_at = excluded.updated_at",
        (table, row_id, write_origin, device["github_login"], replica_encoded, updated_at),
    )
    return notify, {
        "table": table,
        "row_id": row_id,
        "origin_device_id": write_origin,
        "github_login": device["github_login"],
        "payload": loads(replica_encoded),
        "updated_at": updated_at,
    }


def _validate_match(match: Any) -> dict[str, Any]:
    if not isinstance(match, dict):
        raise HTTPException(status_code=400, detail="match must be an object")
    if len(match) > MAX_MATCH_PREDICATES:
        raise HTTPException(status_code=400, detail="at most 4 predicates per match")
    out: dict[str, Any] = {}
    for path, value in match.items():
        if path not in MATCH_PATHS:
            raise HTTPException(status_code=400, detail="unknown match path")
        if isinstance(value, str):
            if value == "":
                raise HTTPException(status_code=400, detail="match equality string must be non-empty")
            out[path] = value
            continue
        if isinstance(value, dict):
            if set(value.keys()) != {"in"}:
                raise HTTPException(status_code=400, detail="match object value must have only key in")
            in_list = value["in"]
            if not isinstance(in_list, list) or len(in_list) == 0:
                raise HTTPException(status_code=400, detail="in must be a non-empty list of strings")
            if len(in_list) > MAX_IN_VALUES:
                raise HTTPException(status_code=400, detail="at most 16 strings in each in")
            if not all(isinstance(item, str) for item in in_list):
                raise HTTPException(status_code=400, detail="in must be a non-empty list of strings")
            out[path] = {"in": list(in_list)}
            continue
        raise HTTPException(status_code=400, detail="match value must be a string or in-object")
    return out


def _match_path_lookup(payload: dict[str, Any], path: str) -> Any:
    if path == "type":
        if "type" not in payload:
            return _MISSING
        return payload["type"]
    if path.startswith("payload."):
        key = path[len("payload.") :]
        inner = payload.get("payload")
        if not isinstance(inner, dict) or key not in inner:
            return _MISSING
        return inner[key]
    return _MISSING


def _match_applies(match: dict[str, Any], payload: dict[str, Any]) -> bool:
    for path, pred in match.items():
        value = _match_path_lookup(payload, path)
        if value is _MISSING:
            return False
        if isinstance(pred, str):
            if value != pred:
                return False
            continue
        if value not in pred["in"]:
            return False
    return True


def _device_matches(hub: Hub, device_id: str) -> list[dict[str, Any]]:
    rows = hub.store.query(
        "SELECT match_json FROM device_subscription WHERE device_id = ? ORDER BY seq ASC",
        (device_id,),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        loaded = loads(row["match_json"])
        if isinstance(loaded, dict):
            out.append(loaded)
    return out


def _subscriptions_response(hub: Hub, device_id: str) -> dict[str, Any]:
    return {"subscriptions": [{"match": m} for m in _device_matches(hub, device_id)]}


def _visible_replica_rows(hub: Hub, login: str) -> list[Any]:
    allowed = hub.visible(login)
    return hub.store.query(
        "SELECT * FROM row_replica WHERE github_login IN ({}) ORDER BY table_name ASC, row_id ASC".format(
            _placeholders(allowed)
        ),
        tuple(sorted(allowed)),
    )


def _subscription_snapshots(hub: Hub, device: dict[str, Any]) -> list[dict[str, Any]]:
    matches = _device_matches(hub, device["id"])
    if not matches:
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _visible_replica_rows(hub, device["github_login"]):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if not any(_match_applies(match, payload) for match in matches):
            continue
        key = (row["table_name"], row["row_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(_replica_out(row))
    return out


def _query_matching_rows(hub: Hub, login: str, match: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _visible_replica_rows(hub, login):
        payload = loads(row["payload"])
        if not isinstance(payload, dict):
            continue
        if not _match_applies(match, payload):
            continue
        out.append(_replica_out(row))
        if len(out) >= QUERY_ROW_CAP:
            break
    return out


def _enqueue_subscription_fanout(hub: Hub, snap: dict[str, Any]) -> None:
    payload = snap.get("payload")
    if not isinstance(payload, dict):
        return
    owner = snap.get("github_login")
    if not isinstance(owner, str):
        return
    devices = hub.store.query(
        "SELECT DISTINCT d.id AS device_id, d.github_login AS github_login "
        "FROM device d "
        "JOIN device_subscription s ON s.device_id = d.id "
        "WHERE d.revoked_at IS NULL"
    )
    for device in devices:
        device_id = device["device_id"]
        login = device["github_login"]
        if owner not in hub.visible(login):
            continue
        matches = _device_matches(hub, device_id)
        if not any(_match_applies(match, payload) for match in matches):
            continue
        queue = hub.device_queues.get(device_id)
        if queue is None:
            continue
        try:
            queue.put_nowait({"type": "subscription", "rows": [snap]})
        except asyncio.QueueFull:
            continue
