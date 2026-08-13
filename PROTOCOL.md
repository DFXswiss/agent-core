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

`GET /sync/pull?after_hub_seq=N` returns events from devices whose GitHub login is visible to the caller.

`GET /sync/restore` returns the caller's full own event log plus the current row replica for every visible login. A wiped laptop replays `own_events` in `origin_seq` order and applies `rows` for teammates.

`GET /sync/ws?token=...` pushes `{type: events|ping|hello|ping}` when new data arrives.

## Website

Cookie session after GitHub OAuth.

- `GET /auth/me` — login, visible logins, teams.
- `GET /api/state` — materialized rows the caller may see.
- `POST /api/pings` — `{to, kind, task_id?, body?}`; `kind` is `review-request|ping|question`. Target must be visible.
- `POST /api/pings/{id}/ack` — recipient only.
- `GET /api/stream` — server-sent events for the signed-in browser.
