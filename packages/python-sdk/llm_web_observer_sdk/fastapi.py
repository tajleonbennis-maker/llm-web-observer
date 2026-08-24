from __future__ import annotations

import re
from typing import Awaitable, Callable

from .client import Observer

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def middleware(observer: Observer) -> Callable:
    async def observe(request, call_next: Callable[..., Awaitable]):
        header = request.headers.get("traceparent", "")
        match = TRACEPARENT.fullmatch(header)
        trace_id, parent_id = match.groups() if match else (None, None)
        with observer.span(
            f"http.{request.method.lower()}", span_kind="server", trace_id=trace_id, parent_span_id=parent_id,
            attributes={"http.request.method": request.method, "url.path": request.url.path},
        ) as span:
            response = await call_next(request)
            span.set(**{"http.response.status_code": response.status_code})
            if response.status_code >= 500:
                span.status = "error"
            response.headers["traceparent"] = f"00-{span.trace_id}-{span.span_id}-01"
            return response
    return observe

