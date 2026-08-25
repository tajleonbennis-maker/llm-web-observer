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
    row = client.get("/v1/conversations").json()["items"][0]
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


def test_conversations_hide_internal_llm_tasks_by_default(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    internal = event(
        trace_id="c" * 32,
        span_id="d" * 16,
        attributes={
            "lwo.user.message": "# Recent activity\nPropose the three things worth exploring next.",
            "lwo.assistant.message": "[]",
            "lwo.call.kind": "internal.recommendations",
        },
    )
    assert client.post("/v1/events", json={"events": [internal]}).status_code == 200
    assert client.get("/v1/conversations").json()["items"] == []
    rows = client.get("/v1/conversations?include_internal=true").json()["items"]
    assert rows[0]["call_kind"] == "internal.recommendations"


def test_conversations_hide_empty_agent_steps(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    step = event(attributes={
        "lwo.user.message": "Weather in Shanghai",
        "lwo.assistant.message": "",
        "lwo.call.kind": "user.chat",
        "lwo.policy.action": "allow",
    })
    assert client.post("/v1/events", json={"events": [step]}).status_code == 200
    assert client.get("/v1/conversations").json()["items"] == []
    assert len(client.get("/v1/conversations?include_internal=true").json()["items"]) == 1


def test_conversation_projection_repairs_historical_utf8_mojibake(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    broken = "上海当前天气如下：".encode("utf-8").decode("latin-1")
    payload = event(attributes={
        "lwo.user.message": "上海天气",
        "lwo.assistant.message": broken,
        "lwo.call.kind": "user.chat",
    })
    assert client.post("/v1/events", json={"events": [payload]}).status_code == 200
    assert client.get("/v1/conversations").json()["items"][0]["response"] == "上海当前天气如下："


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


def test_client_context_key_protects_beacon_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("LWO_CLIENT_KEY", "beacon-secret")
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    payload = {
        "fingerprint": "b" * 64,
        "session_id": "session-5678",
        "user_agent": "Mozilla/5.0 Test",
    }
    assert client.post("/v1/client-context", json=payload).status_code == 401
    assert client.post(
        "/v1/client-context", json=payload, headers={"Authorization": "Bearer beacon-secret"}
    ).status_code == 200


def test_exact_header_identity_projects_to_conversation(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    payload = event(
        user_id_hash="user-42",
        session_id="session-android-1",
        attributes={
            "gen_ai.request.model": "openai/gpt-4.1-mini",
            "lwo.user.message": "您好",
            "lwo.assistant.message": "你好！",
            "lwo.call.kind": "user.chat",
            "lwo.policy.action": "allow",
            "enduser.id": "user-42",
            "enduser.name": "alice",
            "client.platform": "android",
            "client.identity.confidence": "exact",
            "lwo.identity.source": "header",
        },
    )
    assert client.post("/v1/events", json={"events": [payload]}).status_code == 200
    row = client.get("/v1/conversations").json()["items"][0]
    assert row["username"] == "alice"
    assert row["platform"] == "android"
    assert row["identity_confidence"] == "exact"
    assert row["session_id"] == "session-android-1"
    assert row["user_id_hash"] == "user-42"


def _inbound(client, *, method="POST", path="/api/v1/chat", kind="http", token=None, cookie=None, ip="198.51.100.7"):
    headers = {"x-lwo-inbound-kind": kind, "x-lwo-inbound-method": method,
               "x-lwo-inbound-path": path, "x-forwarded-for": ip}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    return client.request(method, "/v1/inbound", headers=headers)


def test_inbound_mirror_hashes_credentials(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    result = _inbound(client, token="secret-token-a").json()
    assert result["recorded"] is True
    assert result["identity_kind"] == "authorization"
    assert result["user_key"].startswith("u-")
    assert "secret-token-a" not in result["user_key"]
    # same token -> same pseudonymous key
    assert _inbound(client, token="secret-token-a").json()["user_key"] == result["user_key"]
    # cookie fallback
    cookie_result = _inbound(client, method="GET", path="/api/v1/ws", kind="ws",
                             cookie="other=1; dt_token=cookie-token-z").json()
    assert cookie_result["identity_kind"] == "cookie"
    assert cookie_result["user_key"] != result["user_key"]
    # no credentials -> recorded without identity
    assert _inbound(client, token=None, cookie=None).json()["user_key"] is None


def test_inbound_ws_query_token_hashed_not_stored(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    result = _inbound(client, method="GET", path="/api/v1/ws?token=ws-secret-token",
                      kind="ws", token=None, cookie=None).json()
    assert result["recorded"] is True
    assert result["identity_kind"] == "query_token"
    assert result["user_key"].startswith("u-")
    assert "ws-secret-token" not in result["user_key"]
    # same ws token -> same key; raw token never lands in the path column
    again = _inbound(client, method="GET", path="/api/v1/ws?token=ws-secret-token",
                     kind="ws", token=None, cookie=None).json()
    assert again["user_key"] == result["user_key"]
    import sqlite3
    rows = sqlite3.connect(str(tmp_path / "observer.db")).execute(
        "SELECT path FROM inbound_requests").fetchall()
    assert all("ws-secret-token" not in r[0] for r in rows)
    assert rows[0][0] == "/api/v1/ws"


def _jwt(payload: dict) -> str:
    import base64
    import json as _json

    def _b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(_json.dumps(obj).encode()).rstrip(b"=").decode()

    return ".".join([_b64({"alg": "HS256", "typ": "JWT"}), _b64(payload), "sig"])


def test_inbound_decodes_jwt_identity(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    token = _jwt({"sub": "alice@example.com", "role": "admin", "uid": "u_12345",
                  "exp": 9999999999, "iat": 0})
    result = _inbound(client, token=token).json()
    assert result["user_id"] == "u_12345"
    assert result["username"] == "alice@example.com"
    # a non-JWT credential yields no stable identity
    assert _inbound(client, token="plain-secret").json()["user_id"] is None


def test_identity_resolve_returns_stable_user_id(tmp_path):
    import time as _time

    client = TestClient(create_app(str(tmp_path / "observer.db")))
    _inbound(client, token=_jwt({"sub": "bob@example.com", "uid": "u_999"}))
    resolved = client.get("/v1/identity/resolve",
                          params={"conversation_id": "conv-jwt", "before": _time.time() + 1}).json()
    assert resolved["confidence"] == "inferred"
    assert resolved["user_id"] == "u_999"
    assert resolved["username"] == "bob@example.com"


def test_identity_resolve_ignores_service_noise_paths(tmp_path):
    import time as _time

    client = TestClient(create_app(str(tmp_path / "observer.db")))
    # service credential polls /api/worker/claim every few seconds
    for _ in range(20):
        _inbound(client, path="/api/worker/claim", token="service-worker-token")
    _inbound(client, token="token-alice")
    resolved = client.get("/v1/identity/resolve",
                          params={"conversation_id": "conv-noise", "before": _time.time() + 1}).json()
    alice_key = _inbound(client, token="token-alice").json()["user_key"]
    assert resolved["confidence"] == "inferred"
    assert resolved["user_key"] == alice_key
    assert resolved["candidates"] == 1  # only alice's call, zero worker noise


def test_identity_resolve_single_user_then_sticky(tmp_path):
    import time as _time

    client = TestClient(create_app(str(tmp_path / "observer.db")))
    _inbound(client, token="token-alice")
    before = _time.time() + 1
    resolved = client.get("/v1/identity/resolve",
                          params={"conversation_id": "conv-1", "before": before}).json()
    assert resolved["confidence"] == "inferred"
    assert resolved["user_key"].startswith("u-")
    # second call inherits via stickiness even without fresh inbound traffic
    later = before + 300
    sticky = client.get("/v1/identity/resolve",
                        params={"conversation_id": "conv-1", "before": later}).json()
    assert sticky["confidence"] == "sticky"
    assert sticky["user_key"] == resolved["user_key"]


def test_identity_resolve_marks_ambiguous_users(tmp_path):
    import time as _time

    client = TestClient(create_app(str(tmp_path / "observer.db")))
    _inbound(client, token="token-alice")
    _inbound(client, token="token-bob")
    resolved = client.get("/v1/identity/resolve",
                          params={"conversation_id": "conv-2", "before": _time.time() + 1}).json()
    assert resolved["confidence"] == "inferred-ambiguous"
    assert resolved["distinct_users"] == 2
    # ambiguous evidence must not lock ownership
    sticky = client.get("/v1/identity/resolve",
                        params={"conversation_id": "conv-2", "before": _time.time() + 2}).json()
    assert sticky.get("confidence") != "sticky"


def test_identity_resolve_requires_api_key(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("LWO_API_KEY", "observer-secret")
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    params = {"conversation_id": "conv-x", "before": _time.time()}
    assert client.get("/v1/identity/resolve", params=params).status_code == 401
    headers = {"Authorization": "Bearer observer-secret"}
    assert client.get("/v1/identity/resolve", params=params, headers=headers).status_code == 200


def test_inferred_identity_projects_to_conversation(tmp_path):
    import time as _time

    client = TestClient(create_app(str(tmp_path / "observer.db")))
    inbound = _inbound(client, token="token-carol", ip="198.51.100.9").json()
    payload = event(
        user_id_hash=inbound["user_key"],
        attributes={
            "gen_ai.request.model": "deepseek/deepseek-v4-flash",
            "lwo.user.message": "您好",
            "lwo.assistant.message": "你好！",
            "lwo.call.kind": "user.chat",
            "lwo.policy.action": "allow",
            "enduser.id": inbound["user_key"],
            "client.address": "198.51.100.9",
            "client.identity.confidence": "inferred",
            "lwo.identity.source": "inbound",
        },
    )
    assert client.post("/v1/events", json={"events": [payload]}).status_code == 200
    row = client.get("/v1/conversations").json()["items"][0]
    assert row["user_id_hash"] == inbound["user_key"]
    assert row["identity_confidence"] == "inferred"
    assert row["source_ip"] == "198.51.100.9"


def test_conversation_search_filters(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    a = event(conversation_id="c-1", attributes={
        "lwo.user.message": "银行卡号是多少", "lwo.assistant.message": "抱歉无法提供",
        "lwo.call.kind": "user.chat", "lwo.policy.action": "block",
        "gen_ai.request.model": "deepseek-v4", "enduser.name": "alice",
        "enduser.id": "u_alice",
    })
    b = event(trace_id="b" * 32, span_id="c" * 16, conversation_id="c-2", attributes={
        "lwo.user.message": "今天天气", "lwo.assistant.message": "上海晴天",
        "lwo.call.kind": "user.chat", "lwo.policy.action": "allow",
        "gen_ai.request.model": "gpt-test", "enduser.name": "bob", "enduser.id": "u_bob",
    })
    assert client.post("/v1/events", json={"events": [a, b]}).status_code == 200
    # keyword search across message/response/username
    assert client.get("/v1/conversations", params={"q": "天气"}).json()["items"][0]["username"] == "bob"
    assert client.get("/v1/conversations", params={"q": "银行卡"}).json()["items"][0]["username"] == "alice"
    # user filter
    assert client.get("/v1/conversations", params={"user": "alice"}).json()["items"][0]["user_id"] == "u_alice"
    # model filter
    assert client.get("/v1/conversations", params={"model": "deepseek"}).json()["items"][0]["model"] == "deepseek-v4"
    # action filter
    assert client.get("/v1/conversations", params={"action": "block"}).json()["items"][0]["policy_action"] == "block"
    # total is exposed
    assert client.get("/v1/conversations", params={"action": "block"}).json()["total"] == 1


def test_policy_direction_persisted(tmp_path):
    client = TestClient(create_app(str(tmp_path / "observer.db")))
    policy = {"name": "block leak in reply", "pattern": "secret", "action": "block", "direction": "response"}
    created = client.post("/v1/policies", json=policy).json()
    assert created["direction"] == "response"
    assert client.get("/v1/policies").json()[0]["direction"] == "response"
    # default direction is request when omitted
    defaulted = client.post("/v1/policies", json={"name": "x", "pattern": "y", "action": "log"}).json()
    assert defaulted["direction"] == "request"
