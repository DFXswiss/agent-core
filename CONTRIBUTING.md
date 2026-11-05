# Contributing

- Branch from `develop`. Never push to `develop` or `main`.
- Open a draft pull request. A human merges.
- Sign commits with the GitHub identity that owns the commits.
- Public repository: English for commits, pull requests, and comments.
- Do not name private repositories, internal hostnames, or internal infrastructure.
- Team membership changes belong in `teams.yaml` and go through a pull request.
- Add or update tests in the same change.
- Run `pytest` before you push.

## Dashboard lists (newest first)

Every list on the website, and every array of rows in `GET /api/state`, is **newest first**. Newest means the latest change, not insertion order and not alphabetical id.

There are two documented exceptions:

- `GET /api/sessions/{id}/timeline` is a log and is therefore oldest first.
- The owned-agents list (`GET /auth/me` `owned_agents` and the dashboard **Agents** section) follows the roster in `teams.yaml`, sorted by login, because roster entries carry no change time. The pair page's login selector lists the signed-in login first, then the same owned-agents list.

- Store tables (`session`, `task`, `pings`, and the other replica keys in `/api/state`): `row_replica.updated_at` descending, then `row_id` descending.
- Devices: `created_at` descending, then `id` descending.

The website renders `/api/state` in that order. A filter must keep relative order. Do not append a new row at the bottom.

## Pull request text

Four sentences of summary, then details if needed.
