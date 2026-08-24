from __future__ import annotations

import contextvars
import hashlib
import json
import secrets
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_current: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("lwo_span", default=None)


def current_span() -> "Span | None":
    return _current.get()


def _id(bytes_count: int) -> str:
    return secrets.token_hex(bytes_count)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Span:
    observer: "Observer"
    event_type: str
    attributes: dict[str, Any] = field(default_factory=dict)
    span_kind: str = "internal"
    trace_id: str | None = None
    parent_span_id: str | None = None
    context: dict[str, str | None] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: _id(8))
    started_at: str = field(default_factory=_now)
    started_ns: int = field(default_factory=time.monotonic_ns)
    status: str = "unset"
    _token: contextvars.Token | None = field(default=None, init=False)

    def __enter__(self) -> "Span":
        parent = current_span()
        self.trace_id = self.trace_id or (parent.trace_id if parent else _id(16))
        self.parent_span_id = self.parent_span_id or (parent.span_id if parent else None)
        if parent:
            self.context = {**parent.context, **self.context}
        self._token = _current.set(self)
        return self

    def set(self, **attributes: Any) -> "Span":
        self.attributes.update(attributes)
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, _traceback: Any) -> None:
        if exc is not None:
            self.status = "error"
            self.attributes.setdefault("error.type", exc_type.__name__ if exc_type else "Exception")
            self.attributes.setdefault("error.message", str(exc)[:1000])
        elif self.status == "unset":
            self.status = "ok"
        ended_at = _now()
        duration_ms = (time.monotonic_ns() - self.started_ns) / 1_000_000
        self.observer.emit({
            "schema_version": "0.1", "timestamp": ended_at, "event_type": self.event_type,
            "trace_id": self.trace_id, "span_id": self.span_id, "parent_span_id": self.parent_span_id,
            "service": self.observer.service, "span_kind": self.span_kind, "start_time": self.started_at,
            "end_time": ended_at, "duration_ms": duration_ms, "status": self.status,
            **self.context, "attributes": self.attributes,
        })
        if self._token is not None:
            _current.reset(self._token)


class Observer:
    def __init__(self, endpoint: str, service: str, *, api_key: str = "", batch_size: int = 20):
        self.endpoint = endpoint.rstrip("/") + "/v1/events"
        self.service = service
        self.api_key = api_key
        self.batch_size = batch_size
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @staticmethod
    def hash_user(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def span(self, event_type: str, *, attributes: dict[str, Any] | None = None, span_kind: str = "internal", trace_id: str | None = None, parent_span_id: str | None = None, **context: str | None) -> Span:
        return Span(self, event_type, attributes or {}, span_kind, trace_id, parent_span_id, context)

    def llm(self, provider: str, model: str, **context: str | None) -> Span:
        return self.span("gen_ai.chat", attributes={"gen_ai.provider.name": provider, "gen_ai.request.model": model}, span_kind="client", **context)

    def tool(self, name: str, **context: str | None) -> Span:
        return self.span("tool.execute", attributes={"tool.name": name}, **context)

    def emit(self, event: dict[str, Any]) -> None:
        should_flush = False
        with self._lock:
            self._events.append(event)
            should_flush = len(self._events) >= self.batch_size
        if should_flush:
            self.flush()

    def flush(self) -> bool:
        with self._lock:
            if not self._events:
                return True
            events, self._events = self._events, []
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"events": events}, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return 200 <= response.status < 300
        except OSError:
            with self._lock:
                self._events = events + self._events
                self._events = self._events[-1000:]
            return False

