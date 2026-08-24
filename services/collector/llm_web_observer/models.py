from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str = Field(min_length=1, max_length=120)
    trace_id: str = Field(min_length=16, max_length=64)
    span_id: str = Field(min_length=8, max_length=32)
    parent_span_id: str | None = Field(default=None, max_length=32)
    service: str = Field(min_length=1, max_length=120)
    span_kind: Literal["internal", "server", "client", "producer", "consumer"] = "internal"
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    status: Literal["unset", "ok", "error"] = "unset"
    tenant_id: str | None = Field(default=None, max_length=160)
    user_id_hash: str | None = Field(default=None, max_length=160)
    session_id: str | None = Field(default=None, max_length=160)
    conversation_id: str | None = Field(default=None, max_length=160)
    interaction_id: str | None = Field(default=None, max_length=160)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type", "service")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("control characters are not allowed")
        return value


class EventBatch(BaseModel):
    events: list[TelemetryEvent] = Field(min_length=1, max_length=500)


class IngestResult(BaseModel):
    accepted: int
    trace_ids: list[str]


class PolicyRule(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    pattern: str = Field(min_length=1, max_length=500)
    match_type: Literal["contains", "regex"] = "contains"
    action: Literal["log", "redact", "block"] = "log"
    enabled: bool = True
    description: str = Field(default="", max_length=500)


class PolicyRuleRecord(PolicyRule):
    id: int
    created_at: datetime
    updated_at: datetime
