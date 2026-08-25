from __future__ import annotations

import asyncio
import json
import os
import queue
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .models import ClientContext, EventBatch, IngestResult, PolicyRule, PolicyRuleRecord
from .store import EventStore

STATIC = Path(__file__).with_name("static")


def create_app(database_path: str | None = None) -> FastAPI:
    app = FastAPI(title="LLM Web Observer", version="0.1.0")
    app.state.store = EventStore(database_path or os.environ.get("LWO_DATABASE", "data/observer.db"))
    api_key = os.environ.get("LWO_API_KEY", "")
    client_key = os.environ.get("LWO_CLIENT_KEY", "")

    def authorize(authorization: str = Header(default="")) -> None:
        if api_key and authorization != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/events", response_model=IngestResult, dependencies=[Depends(authorize)])
    def ingest(batch: EventBatch) -> IngestResult:
        accepted = app.state.store.ingest(batch.events)
        return IngestResult(accepted=accepted, trace_ids=sorted({event.trace_id for event in batch.events}))

    @app.get("/v1/traces")
    def traces(limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
        return app.state.store.traces(limit)

    @app.get("/v1/traces/{trace_id}")
    def trace(trace_id: str) -> dict:
        events = app.state.store.trace(trace_id)
        if not events:
            raise HTTPException(status_code=404, detail="Trace not found")
        return {"trace_id": trace_id, "events": events}

    @app.get("/v1/metrics")
    def metrics() -> dict:
        return app.state.store.metrics()

    @app.get("/v1/conversations")
    def conversations(limit: int = Query(default=100, ge=1, le=500), include_internal: bool = False,
                      q: str | None = Query(default=None, max_length=200),
                      user: str | None = Query(default=None, max_length=160),
                      model: str | None = Query(default=None, max_length=120),
                      action: str | None = Query(default=None, max_length=20),
                      call_kind: str | None = Query(default=None, max_length=40),
                      since: str | None = Query(default=None, max_length=40),
                      until: str | None = Query(default=None, max_length=40),
                      offset: int = Query(default=0, ge=0)) -> dict:
        return app.state.store.conversations(
            limit, include_internal, q=q, user=user, model=model, action=action,
            call_kind=call_kind, since=since, until=until, offset=offset,
        )

    @app.get("/v1/events/stream", dependencies=[Depends(authorize)])
    async def stream_events(request: Request) -> StreamingResponse:
        """Server-sent events feed of ingested gen_ai.chat conversations."""
        subscriber = app.state.store.subscribe()

        async def gen():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = subscriber.get_nowait()
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        continue
                    except queue.Empty:
                        pass
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)
            finally:
                app.state.store.unsubscribe(subscriber)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v1/client-context")
    def record_client(context: ClientContext, request: Request, authorization: str = Header(default="")) -> dict:
        if client_key and authorization != f"Bearer {client_key}":
            raise HTTPException(status_code=401, detail="Unauthorized")
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        source_ip = forwarded or (request.client.host if request.client else None)
        return app.state.store.record_client(context.model_dump(), source_ip)

    @app.get("/v1/client-context/latest", dependencies=[Depends(authorize)])
    def latest_client(max_age: int = Query(default=300, ge=10, le=3600)) -> dict:
        return app.state.store.latest_client(max_age) or {}

    @app.api_route("/v1/inbound", methods=["GET", "POST"])
    def record_inbound(request: Request, authorization: str = Header(default=""),
                       cookie: str = Header(default="")) -> dict:
        """Receive mirrored inbound API requests (nginx mirror). Body is never read.

        Headers X-LWO-Inbound-Path / X-LWO-Inbound-Method / X-LWO-Inbound-Kind carry the
        original request metadata; credentials (Authorization header, `token` query
        parameter used by websocket auth, dt_token cookie) are hashed to a pseudonymous
        user key. Raw credentials — including the query string — are never stored.
        """
        kind = request.headers.get("x-lwo-inbound-kind", "http")
        method = request.headers.get("x-lwo-inbound-method") or request.method
        raw_path = request.headers.get("x-lwo-inbound-path") or request.url.path
        path, _, query = raw_path.partition("?")
        query_token = None
        for pair in query.split("&"):
            name, _, value = pair.partition("=")
            if name == "token" and value:
                from urllib.parse import unquote_plus
                query_token = unquote_plus(value)
                break
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        source_ip = forwarded or (request.client.host if request.client else None)
        return app.state.store.record_inbound(
            method=method, path=path, kind=kind,
            authorization=authorization or None, cookie=cookie or None, source_ip=source_ip,
            query_token=query_token,
        )

    @app.get("/v1/identity/resolve", dependencies=[Depends(authorize)])
    def resolve_identity(conversation_id: str | None = Query(default=None, max_length=160),
                         before: float = Query(description="epoch seconds of the LLM call start"),
                         rest_lead: int = Query(default=30, ge=1, le=600),
                         ws_lead: int = Query(default=1800, ge=1, le=86400)) -> dict:
        return app.state.store.resolve_identity(conversation_id, before, rest_lead, ws_lead) or {}

    @app.get("/v1/policies", response_model=list[PolicyRuleRecord])
    def policies() -> list[dict]:
        return app.state.store.policies()

    @app.post("/v1/policies", response_model=PolicyRuleRecord, dependencies=[Depends(authorize)])
    def create_policy(policy: PolicyRule) -> dict:
        return app.state.store.create_policy(policy.model_dump())

    @app.put("/v1/policies/{policy_id}", response_model=PolicyRuleRecord, dependencies=[Depends(authorize)])
    def update_policy(policy_id: int, policy: PolicyRule) -> dict:
        updated = app.state.store.update_policy(policy_id, policy.model_dump())
        if not updated:
            raise HTTPException(status_code=404, detail="Policy not found")
        return updated

    @app.delete("/v1/policies/{policy_id}", dependencies=[Depends(authorize)])
    def delete_policy(policy_id: int) -> dict[str, bool]:
        if not app.state.store.delete_policy(policy_id):
            raise HTTPException(status_code=404, detail="Policy not found")
        return {"deleted": True}

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
