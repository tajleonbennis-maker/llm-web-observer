from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import UTC, datetime, timedelta

trace_id = "0123456789abcdef0123456789abcdef"
base = datetime.now(UTC)


def span(event_type, span_id, parent, offset, duration, service, attributes=None, status="ok"):
    start = base + timedelta(milliseconds=offset)
    return {
        "schema_version": "0.1", "timestamp": start.isoformat(), "event_type": event_type,
        "trace_id": trace_id, "span_id": span_id, "parent_span_id": parent, "service": service,
        "start_time": start.isoformat(), "end_time": (start + timedelta(milliseconds=duration)).isoformat(),
        "duration_ms": duration, "status": status, "conversation_id": "demo-conversation",
        "session_id": "demo-session", "attributes": attributes or {},
    }


events = [
    span("ui.message.submit", "1000000000000001", None, 0, 8, "deeptutor-web"),
    span("http.post", "1000000000000002", "1000000000000001", 12, 3850, "deeptutor-api"),
    span("agent.run", "1000000000000003", "1000000000000002", 45, 3720, "deeptutor-agent"),
    span("retrieval.search", "1000000000000004", "1000000000000003", 80, 310, "deeptutor-rag", {"retrieval.documents": 8}),
    span("gen_ai.chat", "1000000000000005", "1000000000000003", 420, 1280, "deeptutor-agent", {"gen_ai.provider.name": "openai", "gen_ai.request.model": "gpt-5", "gen_ai.usage.input_tokens": 1840, "gen_ai.usage.output_tokens": 210}),
    span("tool.execute", "1000000000000006", "1000000000000003", 1740, 430, "deeptutor-tools", {"tool.name": "search_knowledge"}),
    span("gen_ai.chat", "1000000000000007", "1000000000000003", 2210, 1490, "deeptutor-agent", {"gen_ai.provider.name": "openai", "gen_ai.request.model": "gpt-5", "gen_ai.usage.input_tokens": 2240, "gen_ai.usage.output_tokens": 488}),
]

request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/events", data=json.dumps({"events": events}).encode(),
    headers={
        "Content-Type": "application/json",
        **({"Authorization": f"Bearer {os.environ['LWO_API_KEY']}"} if os.environ.get("LWO_API_KEY") else {}),
    },
    method="POST",
)
with urllib.request.urlopen(request) as response:
    print(response.read().decode())
