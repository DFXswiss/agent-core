# Agent sync protocol

This document is the contract between the `agent` client and this hub.

## Identity

Anyone may sign in with GitHub. Authorization is not the login; it is team membership.

- A user always reads and writes their own ledger.
- A user listed on a team in `teams.yaml` also reads every member of that team.
- Membership changes only through a pull request to `teams.yaml`.

## Device pairing

1. `POST /pair/prepare` with `{device_id, challenge, device_name}`.
2. Open `/pair?challenge=...` in a browser, sign in, `POST /pair/confirm`.
3. Client polls `GET /pair/wait?device_id&challenge` until `{status: "paired", token, login}`.

The token is a device credential. It is not a GitHub token. The hub binds `origin_device_id` to the GitHub login of the session that confirmed the pair. The client cannot choose that login.

## Events

`POST /sync/push` with `Authorization: Bearer <device-token>`:

```json
{
  "events": [
    {
      "origin_device_id": "<uuid of this device>",
      "origin_seq": 1,
      "table": "task",
      "op": "insert",
      "row_id": "<uuid>",
      "payload": {},
      "occurred_at": "2026-08-13T12:00:00Z"
    }
  ]
}
```

Rules:

- `origin_device_id` must be the device in the token (otherwise 403).
- `origin_seq` is per device, starts at 1, and must be `last + 1` (otherwise 409).
- Repeating the exact same event is idempotent.
- Repeating a seq with different content is 409.
- Allowed tables: `session`, `task`, `task_round`, `agent`, `checklist_item`, `local_check`, `review_gate`, `open_work`, `ping`.
- Allowed ops: `insert`, `update`, `delete`.

`GET /sync/pull?cursor=<origin_id>:<last_seq>&cursor=...` returns later events for every visible origin. An origin without a cursor starts at 0, so a new teammate is fully backfilled.

`GET /sync/restore` returns every visible event (`events`) and the caller's own subset (`own_events`). A wiped laptop replays `events` in origin sequence order.

`GET /sync/ws?token=...` pushes `{type: events|ping|hello|ping}` when new data arrives. The same socket also accepts inbound control messages (see Control). Clients that never send `control-ready` keep working as before.

## Control

The hub is a relay only. It does not run tmux, does not start processes, and does not author session ledger rows. Devices publish session rows through the normal push path. Control and terminal bytes ride on top of that.

### Ownership and visibility

- A session is **visible** to a viewer when the replica's `github_login` is in `hub.visible(viewer)` (self plus teammates from `teams.yaml`).
- `can_control(login, origin_device_id)` is true only when a `device` row exists with `id == origin_device_id`, `revoked_at IS NULL`, and `github_login == login`. That is **own-device only**: another teammate may watch a session; only the person who owns the origin device may start, stop, type, or resize.
- Unknown or non-visible session ids return **404** (no existence leak across teams).

### Session state fields

`GET /api/state` keeps its existing payload. Every object in `session` gains two computed (not stored) fields:

- `can_control` — see above.
- `control_connected` — true only while that origin device has an open `/sync/ws` connection that has sent `{ "type": "control-ready" }`. Disconnect clears it.

`GET /api/sessions/{id}` (cookie session required):

```json
{
  "id": "<row_id>",
  "payload": { },
  "_github_login": "...",
  "_origin_device_id": "...",
  "can_control": true,
  "control_connected": false
}
```

404 if missing or not visible.

### Control POST

`POST /api/sessions/{id}/control` — cookie session **or** device bearer (`Authorization: Bearer`).

Body (one of):

- `{ "action": "start", "command"?: string, "provider"?: "grok", "model"?: string, "cols"?: int, "rows"?: int }`
- `{ "action": "stop" }`
- `{ "action": "input", "data": string }` **xor** `{ "action": "input", "key": "enter"|"ctrl-c"|"tab" }`
- `{ "action": "resize", "cols": int, "rows": int }`

Validation (400): `action` required and one of those four; `command` if present is a string of length 1..4000; `provider` if present is exactly `grok`; `model` if present is a string of length 1..64 and requires `provider=grok`; `provider` and `command` cannot both be set; `data` if present is a string with utf-8 byte length 1..4096; `key` if present is exactly `enter`|`ctrl-c`|`tab`; input must have exactly one of `data` or `key`; `cols`/`rows` if present are integers 1..500; resize requires both; unknown extra keys are ignored.

`provider=grok` is launch metadata for the owning device. The hub does not run `grok`. The device mints a UUID for Grok `--session-id` (it never forwards the ledger session id) and later uses `--resume` with `runtime.grok_session_id`. An empty model becomes `grok-4.6` on the device.

Authz:

- 401 if not signed in / bad token
- 404 if session missing or not visible
- 403 if not `can_control`
- 409 with detail `owning device is not control-connected` if the origin device has no live control-ready socket
- 202 `{ "queued": true }` after the hub has queued the control frame on **that** origin device's socket (`device.id == origin_device_id`, never another laptop of the same login)

The hub does **not** call ledger write helpers and does not insert a `session` (or any) ledger event for control.

Forwarded WebSocket frame to the device:

```json
{
  "type": "control",
  "session_id": "<id>",
  "action": "start|stop|input|resize",
  "payload": { }
}
```

Payload contents:

- start: include `command` only if provided; include `provider` / `model` only if provided; include `cols`/`rows` only if provided
- stop: `{}`
- input: `{ "data": "..." }` or `{ "key": "enter" }`
- resize: `{ "cols": N, "rows": N }`

### Bidirectional `/sync/ws`

Existing behaviour stays: hello on accept; fan-out of visibility-filtered events via `hub.queues`; keepalive `{ "type": "ping" }` every 20s when idle.

Receive loop on the same socket. A device is control-connected only after it sends `{ "type": "control-ready" }`. Disconnect removes it.

Device → hub (unknown types are ignored; the socket stays open):

```json
{ "type": "control-ready" }
{ "type": "terminal", "session_id": "<id>", "seq": 1, "data": "<base64>" }
{ "type": "control-ack", "session_id": "<id>", "action": "...", "ok": true, "error": "optional" }
```

On `terminal`:

- Reject silently if `session_id` is empty, `seq` is not a positive int, or `data` is not a string.
- Drop if the session replica is missing.
- Drop if the connected device's id is not the session's `origin_device_id` (a device cannot publish another device's terminal).
- Append to an in-memory ring: last 64 chunks per `session_id`, drop oldest; each `data` string max 8192 chars (truncate longer).
- Fan out `{ "type": "terminal", "session_id", "seq", "data" }` to browser terminal subscribers for that session.

Terminal bytes are **ephemeral** and **team-visible** (same visibility class as evidence). They are **not** ledger events. Restore does **not** replay them. They are never written to SQLite.

### Terminal stream

`GET /api/sessions/{id}/terminal` — cookie session. 404 if session missing or not visible.

If `Accept` contains `text/event-stream` (SSE):

- first event: `data: {"type":"hello","session_id":"..."}`
- then any currently buffered ring chunks as `data: {"type":"terminal","session_id","seq","data"}`
- then live chunks
- keepalive comment `: \n\n` every 20s
- on disconnect, unsubscribe

Otherwise JSON snapshot:

```json
{ "session_id": "...", "chunks": [ { "seq": 1, "data": "..." } ] }
```

## Website

Cookie session after GitHub OAuth.

- `GET /auth/me` — login, visible logins, teams.
- `GET /api/state` — materialized rows the caller may see (sessions include `can_control` and `control_connected`).
- `GET /api/sessions/{id}` — one visible session plus control flags.
- `POST /api/sessions/{id}/control` — start / stop / input / resize (owner device only).
- `GET /api/sessions/{id}/terminal` — JSON ring snapshot or SSE terminal stream.
- `POST /api/pings` — `{to, kind, task_id?, body?}`; `kind` is `review-request|ping|question`. Target must be visible.
- `POST /api/pings/{id}/ack` — recipient only.
- `GET /api/stream` — server-sent events for the signed-in browser.
