from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import pair_device


def event(device: str, seq: int, table: str = "task", row_id: str = "row-1", title: str = "Work") -> dict:
    return {
        "origin_device_id": device,
        "origin_seq": seq,
        "table": table,
        "op": "insert",
        "row_id": row_id,
        "payload": {"id": row_id, "title": title, "state": "open"},
        "occurred_at": "2026-08-13T12:00:00Z",
    }


def test_push_pull_restore_and_team_isolation(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    dave_dev = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    alice = pair_device(hub, "code-alice", alice_dev, "alice-laptop")
    bob = pair_device(hub, "code-bob", bob_dev, "bob-laptop")
    dave = pair_device(hub, "code-dave", dave_dev, "dave-laptop")

    r = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {alice}"},
        json={"events": [event(alice_dev, 1, title="Alice task")]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1

    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {bob}"},
        json={"events": [event(bob_dev, 1, row_id="row-b", title="Bob task")]},
    )
    hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {dave}"},
        json={"events": [event(dave_dev, 1, row_id="row-d", title="Dave secret")]},
    )

    alice_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {alice}"})
    titles = {e["payload"]["title"] for e in alice_pull.json()["events"]}
    assert titles == {"Alice task", "Bob task"}
    assert "Dave secret" not in titles

    dave_pull = hub.get("/sync/pull", headers={"Authorization": f"Bearer {dave}"})
    dave_titles = {e["payload"]["title"] for e in dave_pull.json()["events"]}
    assert dave_titles == {"Dave secret"}

    restore = hub.get("/sync/restore", headers={"Authorization": f"Bearer {alice}"})
    body = restore.json()
    assert body["login"] == "alice"
    assert len(body["own_events"]) == 1
    restore_titles = {e["payload"]["title"] for e in body["events"]}
    assert restore_titles == {"Alice task", "Bob task"}


def test_foreign_origin_rejected(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01"
    token = pair_device(hub, "code-alice", alice_dev)
    r = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [event("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01", 1)]},
    )
    assert r.status_code == 403


def test_gap_rejected_idempotent_ok(hub: TestClient) -> None:
    dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02"
    token = pair_device(hub, "code-alice", dev)
    first = event(dev, 1)
    assert hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [first]}).status_code == 200
    gap = hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [event(dev, 3)]})
    assert gap.status_code == 409
    again = hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [first]})
    assert again.status_code == 200
    clash = event(dev, 1, title="other")
    bad = hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [clash]})
    assert bad.status_code == 409
    time_clash = event(dev, 1)
    time_clash["occurred_at"] = "2026-08-13T12:00:01Z"
    assert hub.post("/sync/push", headers={"Authorization": f"Bearer {token}"}, json={"events": [time_clash]}).status_code == 409


def test_cannot_overwrite_teammate_row(hub: TestClient) -> None:
    alice_dev = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03"
    bob_dev = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb03"
    alice = pair_device(hub, "code-alice", alice_dev)
    bob = pair_device(hub, "code-bob", bob_dev)
    shared = "shared-row"
    assert (
        hub.post(
            "/sync/push",
            headers={"Authorization": f"Bearer {alice}"},
            json={"events": [event(alice_dev, 1, row_id=shared, title="Alice")]},
        ).status_code
        == 200
    )
    stolen = hub.post(
        "/sync/push",
        headers={"Authorization": f"Bearer {bob}"},
        json={"events": [event(bob_dev, 1, row_id=shared, title="Bob stole it")]},
    )
    assert stolen.status_code == 403


def test_ping_team_only(hub: TestClient) -> None:
    sign_alice = hub
    from tests.conftest import sign_in

    sign_in(sign_alice, "code-alice")
    ok = sign_alice.post("/api/pings", json={"to": "bob", "kind": "review-request", "body": "please review"})
    assert ok.status_code == 200, ok.text
    denied = sign_alice.post("/api/pings", json={"to": "dave", "kind": "ping"})
    assert denied.status_code == 403

    sign_in(sign_alice, "code-bob")
    ping_id = ok.json()["id"]
    ack = sign_alice.post(f"/api/pings/{ping_id}/ack")
    assert ack.status_code == 200
    state = sign_alice.get("/api/state").json()
    found = [p for p in state["pings"] if p["id"] == ping_id]
    assert len(found) == 1
    assert found[0]["acked_at"]
