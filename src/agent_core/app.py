"""HTTP and WebSocket hub."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
PAIR_TTL_SECONDS = 600


class Hub:
    def __init__(self, cfg: Config, github: GitHub, store: Store, teams: dict[str, list[str]]) -> None:
        self.cfg = cfg
        self.github = github
        self.store = store
        self.teams = teams
        self.queues: list[tuple[str, asyncio.Queue[dict[str, Any]]]] = []

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


def bearer(request: Request) -> str:
    header = request.headers.get("authorization")
    if header is None or not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer token is required")
    token = header[7:].strip()
    if token == "":
        raise HTTPException(status_code=401, detail="Authorization Bearer token is required")
    return token


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
        dest = request.session.pop("after_login", "/")
        if not isinstance(dest, str) or not dest.startswith("/"):
            dest = "/"
        return RedirectResponse(dest, status_code=302)

    @app.post("/auth/logout")
    def auth_logout(request: Request) -> JSONResponse:
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
        if len(challenge) < 16:
            raise HTTPException(status_code=400, detail="challenge is too short")
        existing = hub.store.query_one("SELECT id FROM device WHERE id = ?", (device_id,))
        if existing is not None:
            raise HTTPException(status_code=409, detail="device is already paired")
        hub.store.execute(
            "INSERT OR REPLACE INTO pending_pair (challenge, device_id, device_name, created_at, token, github_login) "
            "VALUES (?, ?, ?, ?, NULL, NULL)",
            (challenge, device_id, name, utcnow()),
        )
        return {"challenge": challenge, "pair_url": f"{cfg.public_url}/pair?challenge={challenge}"}

    @app.get("/pair/wait")
    def pair_wait(device_id: str, challenge: str) -> dict[str, Any]:
        row = hub.store.query_one(
            "SELECT * FROM pending_pair WHERE challenge = ? AND device_id = ?",
            (challenge, device_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="unknown pairing challenge")
        if _age_seconds(row["created_at"]) > PAIR_TTL_SECONDS:
            raise HTTPException(status_code=410, detail="pairing challenge expired")
        if row["token"] is None:
            return {"status": "pending"}
        hub.store.execute(
            "UPDATE pending_pair SET token = NULL WHERE challenge = ? AND device_id = ?",
            (challenge, device_id),
        )
        return {"status": "paired", "token": row["token"], "login": row["github_login"]}

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
        for event in events:
            if not isinstance(event, dict):
                raise HTTPException(status_code=400, detail="event must be an object")
            _accept_event(hub, device, event)
            accepted += 1
        if accepted:
            await hub.publish({"type": "events", "login": device["github_login"], "count": accepted})
        return {"accepted": accepted}

    @app.get("/sync/pull")
    def sync_pull(request: Request, after_hub_seq: str | None = None) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        after = _require_int_string("after_hub_seq", after_hub_seq if after_hub_seq is not None else "0")
        allowed = hub.visible(device["github_login"])
        rows = hub.store.query(
            "SELECT e.* FROM ledger_event e "
            "JOIN device d ON d.id = e.origin_device_id "
            "WHERE e.hub_seq > ? AND d.github_login IN ({}) "
            "ORDER BY e.hub_seq ASC".format(_placeholders(allowed)),
            (after, *sorted(allowed)),
        )
        return {"events": [_event_out(r) for r in rows]}

    @app.get("/sync/restore")
    def sync_restore(request: Request) -> dict[str, Any]:
        device = hub.device_for_token(bearer(request))
        allowed = hub.visible(device["github_login"])
        own = hub.store.query(
            "SELECT * FROM ledger_event WHERE origin_device_id = ? ORDER BY origin_seq ASC",
            (device["id"],),
        )
        replicas = hub.store.query(
            "SELECT * FROM row_replica WHERE github_login IN ({})".format(_placeholders(allowed)),
            tuple(sorted(allowed)),
        )
        last = hub.store.query_one("SELECT COALESCE(MAX(hub_seq), 0) AS m FROM ledger_event")
        return {
            "device_id": device["id"],
            "login": device["github_login"],
            "own_events": [_event_out(r) for r in own],
            "rows": [_replica_out(r) for r in replicas],
            "hub_seq": int(last["m"]) if last is not None else 0,
        }

    @app.get("/api/state")
    def api_state(request: Request) -> dict[str, Any]:
        login = hub.session_login(request)
        allowed = hub.visible(login)
        tables = {
            "session": "session",
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
            out[key] = [_payload_with_origin(r) for r in rows]
        devices = hub.store.query(
            "SELECT id, github_login, name, created_at, revoked_at FROM device "
            "WHERE github_login IN ({})".format(_placeholders(allowed)),
            tuple(sorted(allowed)),
        )
        out["devices"] = [row_dict(r) for r in devices]
        return out

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
        hub.store.execute(
            "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
            "VALUES ('ping', ?, '', ?, ?, ?)",
            (ping_id, login, dumps(payload), utcnow()),
        )
        await hub.publish({"type": "ping", "from": login, "to": target, "id": ping_id})
        return {"id": ping_id}

    @app.post("/api/pings/{ping_id}/ack")
    def api_ping_ack(request: Request, ping_id: str) -> dict[str, str]:
        login = hub.session_login(request)
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
        hub.store.execute(
            "UPDATE row_replica SET payload = ?, updated_at = ? WHERE table_name = 'ping' AND row_id = ?",
            (dumps(payload), utcnow(), ping_id),
        )
        return {"id": ping_id, "acked_at": payload["acked_at"]}

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

        from fastapi.responses import StreamingResponse

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
        hub.queues.append((device["github_login"], queue))
        try:
            await ws.send_json({"type": "hello", "login": device["github_login"], "device_id": device["id"]})
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
                    continue
                await ws.send_json(item)
        except WebSocketDisconnect:
            pass
        finally:
            item = (device["github_login"], queue)
            if item in hub.queues:
                hub.queues.remove(item)

    return app


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


def _accept_event(hub: Hub, device: dict[str, Any], event: dict[str, Any]) -> None:
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
        return
    last = hub.store.query_one(
        "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = ?",
        (origin,),
    )
    last_seq = int(last["m"]) if last is not None else 0
    if seq != last_seq + 1:
        raise HTTPException(status_code=409, detail=f"origin_seq gap: have {last_seq}, got {seq}")
    replica = hub.store.query_one(
        "SELECT origin_device_id FROM row_replica WHERE table_name = ? AND row_id = ?",
        (table, row_id),
    )
    if replica is not None and replica["origin_device_id"] != origin:
        raise HTTPException(status_code=403, detail="row belongs to another device")
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
        return
    hub.store.execute(
        "INSERT INTO row_replica (table_name, row_id, origin_device_id, github_login, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(table_name, row_id) DO UPDATE SET origin_device_id = excluded.origin_device_id, "
        "github_login = excluded.github_login, payload = excluded.payload, updated_at = excluded.updated_at",
        (table, row_id, origin, device["github_login"], encoded, utcnow()),
    )
