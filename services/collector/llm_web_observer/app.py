from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import EventBatch, IngestResult, PolicyRule, PolicyRuleRecord
from .store import EventStore

STATIC = Path(__file__).with_name("static")


def create_app(database_path: str | None = None) -> FastAPI:
    app = FastAPI(title="LLM Web Observer", version="0.1.0")
    app.state.store = EventStore(database_path or os.environ.get("LWO_DATABASE", "data/observer.db"))
    api_key = os.environ.get("LWO_API_KEY", "")

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
    def conversations(limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
        return app.state.store.conversations(limit)

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
