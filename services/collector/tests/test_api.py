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
