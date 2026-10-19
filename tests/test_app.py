"""Behavioral tests cover genuine edge cases without third-party data."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from fastapi.testclient import TestClient

from hooklab.app import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("HOOKLAB_DB", str(tmp_path / "hooklab.db"))
    with TestClient(app) as test_client:
        yield test_client


def create(client, event_type="invoice.paid"):
    response = client.post("/api/events", json={"event_type": event_type, "payload": {"id": "synthetic"}})
    assert response.status_code == 201
    return response.json()


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_create_and_list_events(client):
    created = create(client)
    assert created["payload"] == {"id": "synthetic"}
    assert client.get("/api/events").json()[0]["id"] == created["id"]


def test_event_validation_and_not_found(client):
    assert client.post("/api/events", json={"event_type": "", "payload": {}}).status_code == 422
    assert client.get("/api/events/987").status_code == 404
    assert client.get("/api/events?limit=0").status_code == 422


def test_delivery_success(client):
    event_id = create(client)["id"]
    response = client.post(f"/api/events/{event_id}/deliveries", json={"behavior": "success"})
    assert response.status_code == 201
    assert response.json()["status"] == "delivered"
    assert response.json()["attempts"] == 1


def test_fail_once_then_retry(client):
    event_id = create(client)["id"]
    delivery = client.post(f"/api/events/{event_id}/deliveries", json={"behavior": "fail-once"}).json()
    assert delivery["status"] == "failed"
    retried = client.post(f"/api/deliveries/{delivery['id']}/retry").json()
    assert retried["status"] == "delivered"
    assert retried["attempts"] == 2
    assert retried["error"] is None


def test_always_fail_and_error_handling(client):
    event_id = create(client)["id"]
    delivery = client.post(f"/api/events/{event_id}/deliveries", json={"behavior": "always-fail"}).json()
    assert delivery["status"] == "failed"
    assert client.post(f"/api/deliveries/{delivery['id']}/retry").json()["status"] == "failed"
    assert client.post("/api/deliveries/987/retry").status_code == 404
    assert client.post("/api/events/987/deliveries", json={"behavior": "success"}).status_code == 404
    assert client.post(f"/api/events/{event_id}/deliveries", json={"behavior": "unknown"}).status_code == 422


def test_web_ui_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "HookLab" in response.text


def test_filter_events_by_exact_type_and_limit(client):
    first = create(client, "invoice.paid")
    create(client, "invoice.failed")
    third = create(client, "invoice.paid")

    response = client.get("/api/events", params={"event_type": "invoice.paid"})
    assert response.status_code == 200
    assert [event["id"] for event in response.json()] == [third["id"], first["id"]]
    limited = client.get("/api/events", params={"event_type": "invoice.paid", "limit": 1})
    assert [event["id"] for event in limited.json()] == [third["id"]]
    assert client.get("/api/events", params={"event_type": "missing"}).json() == []


def test_event_type_filter_validation_and_sql_injection(client):
    create(client, "invoice.paid")
    assert client.get("/api/events", params={"event_type": ""}).status_code == 422
    assert client.get("/api/events", params={"event_type": "x" * 101}).status_code == 422
    # A malicious string must be handled as plain data, not SQL.
    assert client.get("/api/events", params={"event_type": "' OR 1=1 --"}).json() == []
    assert len(client.get("/api/events").json()) == 1


def test_idempotency_key_replay_returns_existing_event(client):
    body = {"event_type": "invoice.paid", "payload": {"amount": 42, "id": "demo"}}
    first = client.post("/api/events", json=body, headers={"Idempotency-Key": "invoice-42"})
    second = client.post(
        "/api/events", json={"event_type": "invoice.paid", "payload": {"id": "demo", "amount": 42}},
        headers={"Idempotency-Key": "invoice-42"},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    assert len(client.get("/api/events").json()) == 1


def test_idempotency_key_conflicts_when_content_changes(client):
    headers = {"Idempotency-Key": "same-key"}
    create_body = {"event_type": "invoice.paid", "payload": {"amount": 42}}
    assert client.post("/api/events", json=create_body, headers=headers).status_code == 201
    changed_amount = {"event_type": "invoice.paid", "payload": {"amount": 99}}
    changed_type = {"event_type": "invoice.failed", "payload": {"amount": 42}}
    assert client.post("/api/events", json=changed_amount, headers=headers).status_code == 409
    assert client.post("/api/events", json=changed_type, headers=headers).status_code == 409
    assert len(client.get("/api/events").json()) == 1


def test_idempotency_key_is_optional_and_validated(client):
    body = {"event_type": "invoice.paid", "payload": {}}
    assert client.post("/api/events", json=body).status_code == 201
    assert client.post("/api/events", json=body).status_code == 201
    assert len(client.get("/api/events").json()) == 2
    for key in ("", "   ", "x" * 129):
        assert client.post("/api/events", json=body, headers={"Idempotency-Key": key}).status_code == 422


def test_idempotency_migrates_existing_database(client, monkeypatch, tmp_path):
    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO events (event_type, payload) VALUES ('legacy', '{}')")
    monkeypatch.setenv("HOOKLAB_DB", str(legacy_db))
    from hooklab.database import initialize
    initialize()
    initialize()  # Migration is safe on repeated startup.
    assert client.get("/api/events").json()[0]["event_type"] == "legacy"
    assert client.post("/api/events", json={"event_type": "new", "payload": {}}, headers={"Idempotency-Key": "new-key"}).status_code == 201


def test_parallel_requests_with_same_idempotency_key_create_one_event(client):
    body = {"event_type": "invoice.paid", "payload": {"id": "parallel"}}
    def send(_):
        return client.post("/api/events", json=body, headers={"Idempotency-Key": "parallel"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(send, range(8)))
    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(client.get("/api/events").json()) == 1
