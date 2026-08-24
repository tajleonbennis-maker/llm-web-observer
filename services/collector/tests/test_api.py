from fastapi.testclient import TestClient

from llm_web_observer.app import create_app


def event(**updates):
    value = {
        "schema_version": "0.1",
        "timestamp": "2026-08-24T12:00:00Z",
        "event_type": "gen_ai.chat",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "service": "test-api",
        "duration_ms": 125.5,
        "status": "ok",
        "conversation_id": "conversation-1",
        "attributes": {
            "gen_ai.request.model": "gpt-test",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
            "prompt": "must not be stored",
        },
    }
    value.update(updates)
    return value


def test_ingest_trace_metrics_and_redaction(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    response = client.post("/v1/events", json={"events": [event()]})
    assert response.status_code == 200
    assert response.json()["accepted"] == 1

    trace = client.get(f"/v1/traces/{'a' * 32}").json()
    assert trace["events"][0]["attributes"]["prompt"] == "[REDACTED]"

    metrics = client.get("/v1/metrics").json()
    assert metrics["traces"] == 1
    assert metrics["models"][0] == {
        "model": "gpt-test", "calls": 1, "input_tokens": 10, "output_tokens": 5
    }


def test_ingest_is_idempotent(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    payload = {"events": [event()]}
    assert client.post("/v1/events", json=payload).json()["accepted"] == 1
    assert client.post("/v1/events", json=payload).json()["accepted"] == 0


def test_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LWO_API_KEY", "test-secret")
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    assert client.post("/v1/events", json={"events": [event()]}).status_code == 401
    assert client.post(
        "/v1/events", json={"events": [event()]}, headers={"Authorization": "Bearer test-secret"}
    ).status_code == 200
    assert client.get("/v1/traces").status_code == 200
    assert client.get("/v1/metrics").status_code == 200


def test_conversation_projection_keeps_readable_fields(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    payload = event(attributes={
        "gen_ai.request.model": "openai/gpt-test",
        "lwo.user.message": "What is the server IP?",
        "lwo.assistant.message": "That request was blocked.",
        "client.address": "203.0.113.8",
        "enduser.name": "alice",
        "lwo.policy.action": "block",
        "lwo.policy.rule": "Infrastructure disclosure",
        "lwo.context.messages": [{"role": "system", "content": "Tutor"}, {"role": "user", "content": "Question"}],
        "client.fingerprint": "f" * 64,
        "user_agent.original": "Mozilla/5.0 TestBrowser",
        "client.browser": "TestBrowser",
    })
    assert client.post("/v1/events", json={"events": [payload]}).status_code == 200
    row = client.get("/v1/conversations").json()[0]
    assert row["username"] == "alice"
    assert row["source_ip"] == "203.0.113.8"
    assert row["message"] == "What is the server IP?"
    assert row["policy_action"] == "block"
    assert row["context_messages"][1]["content"] == "Question"
    assert row["fingerprint"] == "f" * 64
    assert row["browser"] == "TestBrowser"


def test_client_context_records_forwarded_ip_and_latest(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    payload = {
        "fingerprint": "a" * 64,
        "session_id": "session-1234",
        "user_agent": "Mozilla/5.0 Safari/605.1.15",
        "browser": "Safari",
        "platform": "macOS",
        "language": "zh-CN",
        "screen": "1440x900@24",
        "timezone": "Asia/Shanghai",
    }
    response = client.post("/v1/client-context", json=payload, headers={"x-forwarded-for": "203.0.113.9"})
    assert response.status_code == 200
    assert response.json()["source_ip"] == "203.0.113.9"
    latest = client.get("/v1/client-context/latest").json()
    assert latest["fingerprint"] == "a" * 64
    assert latest["browser"] == "Safari"


def test_policy_crud_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LWO_API_KEY", "admin-secret")
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    policy = {"name": "IP disclosure", "pattern": "server ip", "action": "block"}
    assert client.post("/v1/policies", json=policy).status_code == 401
    headers = {"Authorization": "Bearer admin-secret"}
    created = client.post("/v1/policies", json=policy, headers=headers)
    assert created.status_code == 200
    policy_id = created.json()["id"]
    assert client.get("/v1/policies").json()[0]["name"] == "IP disclosure"
    policy["enabled"] = False
    assert client.put(f"/v1/policies/{policy_id}", json=policy, headers=headers).json()["enabled"] is False
    assert client.delete(f"/v1/policies/{policy_id}", headers=headers).json() == {"deleted": True}
