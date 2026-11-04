from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import pair_device, sign_in
from tests.test_control import _recv_until, _wait_control_connected, session_event


def _pair_as(client: TestClient, code: str, device_id: str, agent: str) -> str:
    challenge = "challenge-" + device_id.replace("-", "")[:16]
    prepared = client.post(
        "/pair/prepare",
        json={"device_id": device_id, "challenge": challenge, "device_name": "agent-box"},
    )
    assert prepared.status_code == 200
    sign_in(client, code)
    confirmed = client.post("/pair/confirm", json={"challenge": challenge, "as_login": agent})
    assert confirmed.status_code == 200, confirmed.text
    waited = client.get("/pair/wait", params={"device_id": device_id, "challenge": challenge})
    assert waited.status_code == 200
    assert waited.json()["login"] == agent
    return waited.json()["token"]


def test_owner_pairs_as_agent_and_normal_pair_records_confirmer(agent_hub: TestClient) -> None:
    agent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaab01"
    _pair_as(agent_hub, "code-alice", agent_id, "helperbot")
    normal_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaab02"
    pair_device(agent_hub, "code-alice", normal_id)
    state = agent_hub.get("/api/state").json()
    devices = {row["id"]: row for row in state["devices"]}
    assert devices[agent_id]["github_login"] == "helperbot"
    assert devices[agent_id]["confirmed_by"] == "alice"
    assert devices[normal_id]["github_login"] == "alice"
    assert devices[normal_id]["confirmed_by"] == "alice"


def test_pair_as_agent_authorization(agent_hub: TestClient) -> None:
    for suffix, code, target, expected in (
        ("03", "code-bob", "helperbot", 403),
        ("04", "code-alice", "not-an-agent", 404),
    ):
        device_id = f"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaab{suffix}"
        challenge = "challenge-" + device_id.replace("-", "")[:16]
        agent_hub.post(
            "/pair/prepare",
            json={"device_id": device_id, "challenge": challenge, "device_name": "box"},
        )
        sign_in(agent_hub, code)
        response = agent_hub.post(
            "/pair/confirm", json={"challenge": challenge, "as_login": target}
        )
        assert response.status_code == expected


def test_owner_controls_agent_session_including_owner_device_bearer(agent_hub: TestClient) -> None:
    agent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaab05"
    owner_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaab06"
    agent_token = _pair_as(agent_hub, "code-alice", agent_id, "helperbot")
    owner_token = pair_device(agent_hub, "code-alice", owner_id, "owner-box")
    session_id = "agent-session-owner-control"
    pushed = agent_hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"events": [session_event(agent_id, 1, session_id)]},
    )
    assert pushed.status_code == 200

    sign_in(agent_hub, "code-alice")
    assert agent_hub.get(f"/api/sessions/{session_id}").json()["can_control"] is True
    with agent_hub.websocket_connect(f"/sync/ws?token={agent_token}") as ws:
        ws.receive_json()
        ws.send_json({"type": "control-ready"})
        _wait_control_connected(agent_hub, session_id)
        controlled = agent_hub.post(
            f"/api/sessions/{session_id}/control",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"action": "stop"},
        )
        assert controlled.status_code == 202
        assert _recv_until(ws, lambda frame: frame.get("type") == "control")["action"] == "stop"

    sign_in(agent_hub, "code-bob")
    assert agent_hub.get(f"/api/sessions/{session_id}").json()["can_control"] is False
