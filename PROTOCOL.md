# Agent sync protocol

This document is the contract between the `agent` client and this hub.

## Identity

Anyone may sign in with GitHub. Authorization is not the login; it is team membership.

- A user always reads and writes their own store.
- A user listed on a team in `teams.yaml` also reads every member of that team.
- Membership changes only through a pull request to `teams.yaml`.
- The optional top-level `agents` map assigns owners to agent GitHub logins. Agent and owner logins are lowercase, the agent must belong to a team, and every owner must share a team with it. Duplicate owners, self-ownership, and invalid references fail closed.

## Device pairing

1. `POST /pair/prepare` with `{device_id, challenge, device_name}`.
2. Open `/pair?challenge=...` in a browser, sign in, `POST /pair/confirm`. The body may include `as_login` for an agent owned by the signed-in person.
3. Client polls `GET /pair/wait?device_id&challenge` until `{status: "paired", token, login}`.

The token is a device credential. It is not a GitHub token. Normally the hub binds `origin_device_id` to the signed-in GitHub login. An owner may select one of their configured agents on the pair page; the device and token are then bound to that agent. Unknown agents return 404 and non-owners return 403. The device row records the human confirmer as `confirmed_by`, including for normal pairing.

The `web:` device-id prefix is reserved for synthetic website origins. `POST /pair/prepare` rejects that prefix case-insensitively; website origins cannot be paired as devices.

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
- Origins beginning with `web:` (case-insensitive) are reserved for website-created rows. A device bearer cannot push them (403); only the hub may author them through website actions.
- `origin_seq` is per device, starts at 1, and must be `last + 1` (otherwise 409).
- Repeating the exact same event is idempotent.
- Repeating a seq with different content is 409.
- Allowed tables: `session`, `activity`, `task`, `task_round`, `agent`, `checklist_item`, `local_check`, `review_gate`, `open_work`, `ping`.
- Allowed ops: `insert`, `update`, `delete`.
- Open session ids are unique. A colliding `session` insert for an id that is already open is 409.
- Foreign `origin_device_id` on push is 403. A push must not steal a replica row owned by another device (same `table`+`row_id`).

### Session mail

An `activity` insert with `payload.type=message` must set `payload.payload.to_session` to the recipient **session id**. The hub resolves that id against the replica.

`activity` rows with `payload.type=assignment.created` or `assignment.reply` are hub-authored only. A device push using either type is refused with 400; devices cannot manufacture assignment links or replies.

- Missing target session → 404.
- Sender is not allowed to see the owning login of that session → 403.
- Open session ids are unique; the hub uses that uniqueness to route the snapshot.

Website-created person pings use synthetic origin `web:<login>`. `POST /api/pings/{id}/ack` records a `web:<recipient-login>` event, does not transfer replica ownership of the ping row, and does not consume the sender device's `origin_seq`.

### Pull and restore

Devices do **not** default-pull every visible origin. The website still reads the full visibility-filtered replica (`GET /api/state`, `GET /api/stream`).

`GET /sync/pull?cursor=<origin_id>:<last_seq>&cursor=...`:

```json
{
  "events": [],
  "inbox": [],
  "pings": [],
  "subscriptions": []
}
```

- `events` — this device's own events after the cursor for **this** origin, gapless. Other origins are omitted.
- `inbox` — row snapshots: each `activity` with `type=message` whose `payload.to_session` this device owns, plus the parent `session` snapshot for that activity's `session_id`.
- `pings` — person-ping row snapshots this login **sent or received** (not every team-visible ping).
- `subscriptions` — replica snapshots that pass this device's stored matchers and are visible under §6 (`hub.visible`). Always present (empty list when none). Deduplicated by `(table, row_id)`.

`GET /sync/restore` returns:

```json
{
  "device_id": "<uuid>",
  "login": "<github-login>",
  "own_events": [],
  "inbox": [],
  "pings": []
}
```

A wiped laptop replays `own_events` in `origin_seq` order, then applies `inbox` and `pings` as row snapshots (not a holey event stream). Restore does **not** include a `subscriptions` key.

`GET /sync/ws?token=...` pushes `{type: events|ping|hello|subscription}` when new data arrives, including session-mail for the recipient login. The same socket also accepts inbound control messages (see Control). Clients that never send `control-ready` keep working as before.

### Subscriptions and query

Devices keep a **subscription set on the hub** (per device), not in the event log. Subscriptions are not ledger events.

A **match** object is a JSON object. Evaluation is the AND of all entries. Allowed paths only:

- `type`
- `payload.repo`
- `payload.issue_key`
- `payload.to_session`

Any other key is HTTP **400** `unknown match path`.

Each value is either:

- a string → equality against the dotted path on the replica payload (`type` → `payload["type"]`; `payload.repo` → `payload["payload"]["repo"]` when `payload["payload"]` is a dict)
- `{"in": ["a", "b"]}` → membership; `in` must be a non-empty list of strings

A missing path fails that predicate (the row does not match); it is not 400. Caps: at most 32 subscriptions per device; at most 4 predicates per match; at most 16 strings in each `in`. Exceed → 400.

`PUT /sync/subscriptions` (device bearer) replaces the whole set in one transaction:

```json
{ "subscriptions": [ { "match": { "type": "investigate.step" } } ] }
```

Response 200 echoes `{ "subscriptions": [ { "match": { ... } } ] }` in stored order. An empty list clears. `GET /sync/subscriptions` returns the same body for the calling device.

`POST /sync/query` is a one-shot matcher, not a pull and not stored:

```json
{ "match": { "type": "pr.open", "payload.repo": "owner/repo" } }
```

Response 200: `{ "rows": [ <replica_out>, ... ] }` — only rows whose `github_login` is in `hub.visible(caller)`, only matcher-passing rows, ordered by `table_name`, `row_id`, capped at 500.

When a push materializes a replica row (insert/update), device WebSocket queues may also receive `{ "type": "subscription", "rows": [ <replica_out> ] }` for matchers that pass and visibility that allows it. Browser cookie SSE (`/api/stream`) does not get subscription frames. If the device has no open `/sync/ws` queue, pull catches up.

## Control

The hub is a relay only. It does not run tmux, does not start processes, and does not author session rows. Devices publish session rows through the normal push path. Control and terminal bytes ride on top of that.

### Ownership and visibility

- A session is **visible** to a viewer when the replica's `github_login` is in `hub.visible(viewer)` (self plus teammates from `teams.yaml`).
- `can_control(login, origin_device_id)` is true only when the session's origin device row exists, is not revoked, and its `github_login` either equals the actor login or is an agent login that actor owns. Other teammates may watch but may not control.
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

`provider=grok` is launch metadata for the owning device. The hub does not run `grok`. The device mints a UUID for Grok `--session-id` (it never forwards the store session id) and later uses `--resume` with `runtime.grok_session_id`. An empty model becomes `grok-4.6` on the device.

Authz:

- 401 if not signed in / bad token
- 404 if session missing or not visible
- 403 if not `can_control`
- 409 with detail `owning device is not control-connected` if the origin device has no live control-ready socket
- 202 `{ "queued": true }` after the hub has queued the control frame on **that** origin device's socket (`device.id == origin_device_id`, never another laptop of the same login)

The hub does **not** call store write helpers and does not insert a `session` (or any) store event for control.

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

Terminal bytes are **ephemeral** and **team-visible** (same visibility class as evidence). They are **not** store events. Restore does **not** replay them. They are never written to SQLite.

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

- `GET /auth/me` — login, visible logins, teams, and `owned_agents`. GitHub OAuth is sign-in and GitHub API access (`read:user repo`); the token is not in the cookie.
- `GET /api/prs` — open GitHub pull requests authored or assigned to visible logins. Search uses the signed-in user's OAuth token (scope `read:user repo`), stored in hub sqlite (`oauth_token`) and memory, not in the cookie. `source` is `github` or `none` (signed in, but no token — sign in again). Columns: author, org, repo, number, title, status (`open`|`draft`), url. `truncated` is true when 50 rows were returned.
- `GET /api/state` — materialized rows the caller may see (sessions include `can_control` and `control_connected`; devices include nullable `confirmed_by`). The signed-in website leads with `/api/prs`, then replica tables from `/api/state`. Replica arrays are newest-first (`updated_at` descending, then `row_id`). `devices` is newest-first (`created_at` descending, then `id`).
- `GET /api/sessions/{id}` — one visible session plus control flags.
- `POST /api/sessions/{id}/control` — start / stop / input / resize (device login or configured agent owner only).
- `GET /api/sessions/{id}/terminal` — JSON ring snapshot or SSE terminal stream.
- `GET /api/sessions/{id}/timeline` — the newest 5,000 related replica rows, returned oldest first, with `truncated: true` when capped. A missing website cookie returns 401; the session page renders this as **Signed out — sign in on the dashboard** with a link to `/`.
- `POST /api/assignments` — create and verify an agent-assigned GitHub issue using the owner's OAuth token.
- `GET /api/assignments?agent=<login>` — newest-first assignment activity, enriched best-effort with issue state, assignees, and the newest 20 comments.
- `POST /api/assignments/{owner}/{name}/{number}/comments` — reply using an owner OAuth token after a fresh assignee check.
- `GET /api/usage` — lane-token aggregates, newest OpenRouter account snapshots, and unchanged Grok snapshot rows.
- `POST /api/pings` — `{to, kind, task_id?, body?}`; `kind` is `review-request|ping|question`. Target must be visible.
- `POST /api/pings/{id}/ack` — recipient only.
- `GET /api/stream` — server-sent events for the signed-in browser.

### Assignments

`POST /api/assignments` accepts a website cookie or device bearer and this body:

```json
{"agent":"agent-login","repo":"Owner/Name","title":"Work title","body":"Work details"}
```

The actor must own the agent. A bearer belonging to an agent is refused. Repository names use `Owner/Name`; titles are 1..256 characters and bodies are 1..60000. A missing stored OAuth token returns 409 with a request to sign in on the website. If GitHub returns 401, the hub removes that actor's token from memory and SQLite and the write returns 409 `sign in on the website again`; a 403 remains a 502 and does not remove the token. The hub creates one issue with the agent in `assignees`, verifies the resulting issue, retries the assignee endpoint once if needed, and returns the issue URL on a final 502 so callers do not recreate it blindly. The issue body ends with `Assigned through <public URL> by @<actor>.` The response and stored `assignment.created` payload use GitHub's canonical repository case, even when the request used different casing. Success is 201:

```json
{"repo":"Owner/Name","number":42,"url":"https://github.com/Owner/Name/issues/42","agent":"agent-login","actor":"owner-login"}
```

Creation records an `activity` replica with type `assignment.created`; replies record `assignment.reply`. Both inner payloads include `agent` (a reply also keeps the complete `agents` list), include `issue_key` as `<canonical Owner/Name>#<number>`, use `web:<actor>` origins, and contain no OAuth token. Their stored URL is canonicalized to `https://github.com/<owner>/<repo>/issues/<number>`; a reply may retain a valid `#issuecomment-<number>` fragment. `GET /api/assignments` accepts only `assignment.created` rows whose replica login matches both the `web:<login>` origin and the inner `actor`. Assignment enrichment stops making GitHub calls for the request after a 401 and removes the viewer's failed token from memory and SQLite; a 403 leaves the token stored. Agents look up mandates with `POST /sync/query` by matching both `type` and `payload.issue_key`, for example:

```json
{"match":{"type":"assignment.created","payload.issue_key":"Owner/Name#42"}}
```

GitHub transport errors return bounded 502 messages.

### Usage

`GET /api/usage` is cookie-authenticated and covers visible logins:

```json
{"lanes":[],"accounts":[],"grok":[]}
```

`lanes` groups `lane.run` activity by login, session, provider, and model, counts every run, sums token fields (missing usage counts as zero), and is newest `last_at` first. `accounts` contains the newest `usage.snapshot` per login, OpenRouter vendor, and account with USD usage and limit fields. `grok` contains the same Grok snapshot row shape previously rendered from `/api/state`.

### Session timeline

`GET /api/sessions/{id}/timeline` returns 404 unless the session is visible. It includes the session, its activities and tasks, and task/session-related `task_round`, `agent`, `checklist_item`, `local_check`, and `review_gate` replicas. It also includes visible `assignment.created` and `assignment.reply` activity rows whose inner `payload.agent` equals the session row's owning `github_login`; for a multi-agent reply, membership in its inner `payload.agents` also targets that owning login. “Visible” here means the assignment row's replica login is in the requesting viewer's normal visible-login set. Each row has `table`, `row_id`, `at`, and `payload`; `at` prefers `created_at`, `started_at`, `ran_at`, or `recorded_at` and otherwise uses replica `updated_at`. When more than 5,000 rows match, including these assignment rows, it keeps the newest 5,000 rows and sets `truncated` to true; the oldest rows are dropped, normally including the session row. Unlike every dashboard list, the returned timeline remains oldest first.

`GET /sessions/{id}` renders lane runs and tool calls, coordinator task phase and blocker information, checks and gates, and assignment links. For `lane.run`, the renderer reads the inner `payload.role`, `payload.vendor` (with `payload.provider` as a fallback), `payload.model`, `payload.status`, and `payload.usage.input_tokens` / `output_tokens`. The producer's tool array is inner `payload.tools`, with entries shaped as `{"tool": string, "target": string, "error": boolean}`; legacy inner `payload.tool_calls` remains a display fallback. Inner `payload.tools_truncated: true` is shown even when the retained tool array is empty.
