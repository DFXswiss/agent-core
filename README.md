# agent-core

Hub for the [agent](https://github.com/DFXswiss/agent) session store.

Anyone can sign in with GitHub. Each person sees their own work. People listed on a team in `teams.yaml` also see that team. Add or remove members with a pull request.

The hub stores a full copy of every paired device. Sync is bidirectional: the device is the write owner of its own rows; the hub fans those events out and can restore a wiped device.

This repository does not describe a particular deployment environment. Run it anywhere that can reach GitHub OAuth and serve HTTPS.

Image builds on `develop` / `main` push `dfxswiss/agent-core:beta` / `:latest` and the git SHA. After a successful push they notify the configured infrastructure repo (`DISPATCH_TOKEN` + `DISPATCH_REPO`) so the running hub pulls the new tag. If those secrets are unset, the image is still published.

## Run locally

Create a GitHub OAuth App whose callback is `http://127.0.0.1:8787/auth/github/callback`. The hub requests scope `read:user` so people can sign in. The website shows only the local hub replica — sessions, tasks, agents, pings, devices, usage snapshots. Then:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
export AGENT_CORE_PUBLIC_URL=http://127.0.0.1:8787
export AGENT_CORE_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export AGENT_CORE_GITHUB_CLIENT_ID=...
export AGENT_CORE_GITHUB_CLIENT_SECRET=...
export AGENT_CORE_GITHUB_AUTHORIZE_URL=https://github.com/login/oauth/authorize
export AGENT_CORE_GITHUB_TOKEN_URL=https://github.com/login/oauth/access_token
export AGENT_CORE_GITHUB_USER_URL=https://api.github.com/user
export AGENT_CORE_DATABASE=./hub.sqlite
export AGENT_CORE_TEAMS="$(pwd)/teams.yaml"
export AGENT_CORE_HOST=127.0.0.1
export AGENT_CORE_PORT=8787
python -m agent_core
```

Open http://127.0.0.1:8787 and sign in.

Every variable above is required. The process exits if one is missing.

## Teams

Edit `teams.yaml` and open a pull request:

```yaml
teams:
  dfx:
    members:
      - your-github-login
```

Logins are compared in lowercase. A login that appears on no team still signs in and still syncs; they only see themselves.

## Protocol

See [PROTOCOL.md](PROTOCOL.md).

## Control

Anyone who can see a session may watch its live terminal on the website. Start, stop, type, and resize are allowed only when the signed-in person owns the session's origin device and that device has a control-ready WebSocket. The hub relays control and terminal bytes; it does not run processes and does not author session rows. Details are in [PROTOCOL.md](PROTOCOL.md).

## Tests

```bash
pip install -e ".[test]"
pytest
```

## License

MIT. See [LICENSE](LICENSE).
