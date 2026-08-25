import json
import hashlib
import os
import re
import threading
import time
import urllib.request
import urllib.error
import uuid

from mitmproxy import http

OBSERVER = os.environ.get("LWO_OBSERVER_URL", "http://llm-web-observer:8080").rstrip("/")
if OBSERVER.endswith("/v1/events"):
    OBSERVER = OBSERVER.removesuffix("/v1/events")
API_KEY = os.environ.get("LWO_API_KEY", "")
IDENTITY_PREFIX = "x-lwo-"
started: dict[str, float] = {}
decisions: dict[str, dict] = {}


def _identity_headers(flow: http.HTTPFlow) -> dict[str, str]:
    """Extract X-LWO-* identity headers attached by the calling backend."""
    identity: dict[str, str] = {}
    for name, value in flow.request.headers.items():
        key = name.lower()
        if key.startswith(IDENTITY_PREFIX):
            identity[key[len(IDENTITY_PREFIX):]] = value.strip()[:160]
    return identity


def _strip_identity_headers(flow: http.HTTPFlow) -> None:
    """Remove internal X-LWO-* headers before forwarding upstream."""
    for name in list(flow.request.headers.keys()):
        if name.lower().startswith(IDENTITY_PREFIX):
            del flow.request.headers[name]


def _utf8_content(message) -> str:
    content = message.content or b""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return message.get_text(strict=False) or ""


def _context_messages(body: dict) -> list[dict]:
    result = []
    for message in (body.get("messages") or [])[-100:]:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        result.append({"role": str(message.get("role", "unknown")), "content": str(content)})
    return result


def _call_kind(context: list[dict], message: str) -> str:
    system = "\n".join(item["content"] for item in context if item["role"] == "system").lower()
    if "propose the three things a learner should explore next" in system or message.startswith("# Recent activity"):
        return "internal.recommendations"
    if message.startswith("Generate a title for this conversation"):
        return "internal.title"
    if not message:
        return "internal.unknown"
    return "user.chat"


def _json(url: str, *, payload: dict | None = None) -> object:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def _last_user_message(body: dict) -> tuple[int | None, str]:
    messages = body.get("messages") or []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return index, str(content)
    return None, ""


def _match(message: str, policy: dict) -> bool:
    try:
        if policy["match_type"] == "regex":
            return bool(re.search(policy["pattern"], message, re.IGNORECASE))
        return policy["pattern"].lower() in message.lower()
    except re.error:
        return False


def request(flow: http.HTTPFlow) -> None:
    if flow.request.pretty_host != "openrouter.ai":
        return
    started[flow.id] = time.monotonic()
    try:
        body = json.loads(_utf8_content(flow.request) or "{}")
    except (ValueError, TypeError):
        body = {}
    index, message = _last_user_message(body)
    context = _context_messages(body)
    call_kind = _call_kind(context, message)
    identity = _identity_headers(flow)
    if identity:
        _strip_identity_headers(flow)
    seed = next((item["content"] for item in context if item["role"] == "user"), message)
    conversation_id = hashlib.sha256(f"deeptutor:{seed}".encode()).hexdigest()[:32]
    decision = {"action": "allow", "rule": None, "message": message, "model": body.get("model"),
                "context": context, "conversation_id": conversation_id, "call_kind": call_kind,
                "identity": identity, "wall_started": time.time()}
    try:
        policies = _json(f"{OBSERVER}/v1/policies")
    except Exception:
        policies = []
    decision["policies"] = policies
    for policy in policies if call_kind == "user.chat" else []:
        if policy.get("direction", "request") not in ("request", "both"):
            continue
        if not policy.get("enabled") or not _match(message, policy):
            continue
        decision.update(action=policy["action"], rule=policy["name"])
        if policy["action"] == "redact" and index is not None:
            body["messages"][index]["content"] = "[REDACTED BY GATEWAY POLICY]"
            flow.request.set_text(json.dumps(body))
        elif policy["action"] == "block":
            text = "This message was blocked by an administrator gateway policy."
            if body.get("stream"):
                chunk = {"id": f"blocked-{uuid.uuid4().hex[:8]}", "object": "chat.completion.chunk",
                         "model": body.get("model", "policy-gateway"),
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
                content = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                headers = {"Content-Type": "text/event-stream"}
            else:
                content = json.dumps({"id": f"blocked-{uuid.uuid4().hex[:8]}", "object": "chat.completion",
                    "model": body.get("model", "policy-gateway"), "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                headers = {"Content-Type": "application/json"}
            flow.response = http.Response.make(200, content, headers)
        break
    print(f"policy evaluation: policies={len(policies)} message_chars={len(message)} "
          f"action={decision['action']} identity={'exact' if identity else 'temporal'}")
    decisions[flow.id] = decision


def _response_data(flow: http.HTTPFlow) -> tuple[str, dict]:
    text = _utf8_content(flow.response)
    usage: dict = {}
    answer = ""
    if "text/event-stream" in flow.response.headers.get("content-type", ""):
        for line in text.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[6:])
                answer += str((chunk.get("choices") or [{}])[0].get("delta", {}).get("content") or "")
                usage.update(chunk.get("usage") or {})
            except (ValueError, TypeError, IndexError):
                pass
    else:
        try:
            payload = json.loads(text)
            answer = str((payload.get("choices") or [{}])[0].get("message", {}).get("content") or "")
            usage = payload.get("usage") or {}
        except (ValueError, TypeError, IndexError):
            pass
    return answer, usage


def _resolve_inbound(conversation_id: str | None, before: float) -> dict:
    """Attribute to an inbound caller (nginx-mirrored API request) without business changes."""
    query = f"conversation_id={conversation_id or ''}&before={before:.3f}"
    try:
        resolved = _json(f"{OBSERVER}/v1/identity/resolve?{query}")
        return resolved if resolved.get("user_key") else {}
    except Exception:
        return {}


def _block_response(flow: http.HTTPFlow, model: str, text: str) -> None:
    """Replace the model's response with a gateway block message (both stream and non-stream)."""
    is_stream = "text/event-stream" in flow.response.headers.get("content-type", "")
    if is_stream:
        chunk = {"id": f"blocked-{uuid.uuid4().hex[:8]}", "object": "chat.completion.chunk",
                 "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                             "finish_reason": "stop"}]}
        content = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        headers = {"Content-Type": "text/event-stream"}
    else:
        content = json.dumps({"id": f"blocked-{uuid.uuid4().hex[:8]}", "object": "chat.completion",
            "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
        headers = {"Content-Type": "application/json"}
    flow.response = http.Response.make(200, content, headers)


def _redact_response(flow: http.HTTPFlow, text: str) -> None:
    """Rewrite the model's response content to a redaction placeholder."""
    is_stream = "text/event-stream" in flow.response.headers.get("content-type", "")
    if is_stream:
        chunk = {"id": f"redacted-{uuid.uuid4().hex[:8]}", "object": "chat.completion.chunk",
                 "model": "", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                           "finish_reason": "stop"}]}
        flow.response = http.Response.make(200, f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                            {"Content-Type": "text/event-stream"})
        return
    try:
        payload = json.loads(_utf8_content(flow.response) or "{}")
    except (ValueError, TypeError):
        return
    choices = payload.get("choices") or []
    if choices:
        choices[0].setdefault("message", {})["content"] = text
    flow.response.set_text(json.dumps(payload))


def response(flow: http.HTTPFlow) -> None:
    if flow.request.pretty_host != "openrouter.ai":
        return
    decision = decisions.pop(flow.id, {})
    answer, usage = _response_data(flow)
    resp_action, resp_rule = "allow", None
    for policy in decision.get("policies", []):
        if policy.get("direction", "request") not in ("response", "both"):
            continue
        if not policy.get("enabled") or not _match(answer, policy):
            continue
        resp_action, resp_rule = policy["action"], policy["name"]
        if policy["action"] == "redact":
            answer = "[REDACTED BY GATEWAY POLICY]"
            _redact_response(flow, answer)
        elif policy["action"] == "block":
            answer = "This response was blocked by an administrator gateway policy."
            _block_response(flow, decision.get("model") or "", answer)
        break
    identity = decision.get("identity") or {}
    resolved: dict = {}
    if not identity:
        resolved = _resolve_inbound(decision.get("conversation_id"), decision.get("wall_started", time.time()))
    if identity:
        client = {"session_id": identity.get("session"), "source_ip": None}
    elif resolved:
        # Inbound attribution succeeded — do NOT fall back to client-context/latest:
        # a stale beacon from an unrelated client would pollute display fields.
        client = {"source_ip": resolved.get("source_ip")}
    else:
        try:
            client = _json(f"{OBSERVER}/v1/client-context/latest?max_age=300")
        except Exception:
            client = {}
    if resolved and not client.get("source_ip"):
        client["source_ip"] = resolved.get("source_ip")
    attributes = {"gen_ai.provider.name": "openrouter", "gen_ai.request.model": decision.get("model"),
        "http.response.status_code": flow.response.status_code, "lwo.user.message": decision.get("message"),
        "lwo.assistant.message": answer, "lwo.policy.action": decision.get("action", "allow"),
        "lwo.policy.rule": decision.get("rule"), "lwo.call.kind": decision.get("call_kind", "user.chat"),
        "lwo.policy.response_action": resp_action, "lwo.policy.response_rule": resp_rule,
        "lwo.context.messages": decision.get("context", []),
        "client.address": client.get("source_ip"), "client.fingerprint": client.get("fingerprint"),
        "user_agent.original": client.get("user_agent"), "client.browser": client.get("browser"),
        "client.platform": client.get("platform"), "client.language": client.get("language"),
        "client.screen": client.get("screen"), "client.timezone": client.get("timezone"),
        "client.identity.confidence": (identity and "exact") or resolved.get("confidence")
            or ("temporal" if client.get("fingerprint") else None)}
    if identity:
        attributes["lwo.identity.source"] = "header"
    elif resolved:
        attributes["lwo.identity.source"] = "inbound"
    if identity.get("user"):
        attributes["enduser.id"] = identity["user"]
    elif resolved.get("user_id"):
        attributes["enduser.id"] = resolved["user_id"]
    elif resolved.get("user_key"):
        attributes["enduser.id"] = resolved["user_key"]
    if identity.get("username"):
        attributes["enduser.name"] = identity["username"]
    elif resolved.get("username"):
        attributes["enduser.name"] = resolved["username"]
    if identity.get("platform"):
        attributes["client.platform"] = identity["platform"]
    attributes = {key: value for key, value in attributes.items() if value is not None}
    for source, target in (("prompt_tokens", "gen_ai.usage.input_tokens"),
                           ("completion_tokens", "gen_ai.usage.output_tokens"),
                           ("total_tokens", "gen_ai.usage.total_tokens")):
        if usage.get(source) is not None:
            attributes[target] = usage[source]
    session_id = identity.get("session") or client.get("session_id")
    user_id = identity.get("user") or resolved.get("user_key")
    event = {"schema_version": "0.1", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": "gen_ai.chat", "trace_id": uuid.uuid4().hex, "span_id": uuid.uuid4().hex[:16],
        "service": "deeptutor", "conversation_id": decision.get("conversation_id"),
        "user_id_hash": user_id,
        "session_id": session_id,
        "duration_ms": round((time.monotonic()-started.pop(flow.id,time.monotonic()))*1000,2),
        "status": "ok" if flow.response.status_code < 400 else "error", "attributes": attributes}
    threading.Thread(target=_send, args=(event,), daemon=True).start()


def _send(event: dict) -> None:
    try:
        _json(f"{OBSERVER}/v1/events", payload={"events": [event]})
    except urllib.error.HTTPError as exc:
        print(f"observer export failed: HTTP {exc.code} {exc.read().decode()[:300]}")
    except Exception as exc:
        print(f"observer export failed: {type(exc).__name__}")
