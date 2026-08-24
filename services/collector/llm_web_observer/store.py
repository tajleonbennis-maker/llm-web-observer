from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import TelemetryEvent
from .privacy import sanitize

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    service TEXT NOT NULL,
    span_kind TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    duration_ms REAL,
    status TEXT NOT NULL,
    tenant_id TEXT,
    user_id_hash TEXT,
    session_id TEXT,
    conversation_id TEXT,
    interaction_id TEXT,
    attributes_json TEXT NOT NULL,
    UNIQUE(trace_id, span_id, event_type, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id, timestamp);
"""


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def ingest(self, events: Iterable[TelemetryEvent]) -> int:
        rows = []
        received_at = datetime.now(UTC).isoformat()
        for event in events:
            data = event.model_dump(mode="json")
            rows.append((
                received_at, data["timestamp"], data["event_type"], data["trace_id"], data["span_id"],
                data["parent_span_id"], data["service"], data["span_kind"], data["start_time"], data["end_time"],
                data["duration_ms"], data["status"], data["tenant_id"], data["user_id_hash"], data["session_id"],
                data["conversation_id"], data["interaction_id"], json.dumps(sanitize(data["attributes"]), separators=(",", ":")),
            ))
        with self._lock, self._connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """INSERT OR IGNORE INTO events (
                    received_at,timestamp,event_type,trace_id,span_id,parent_span_id,service,span_kind,
                    start_time,end_time,duration_ms,status,tenant_id,user_id_hash,session_id,conversation_id,
                    interaction_id,attributes_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            return connection.total_changes - before

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["attributes"] = json.loads(result.pop("attributes_json"))
        return result

    def traces(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT trace_id, MIN(timestamp) AS started_at, MAX(timestamp) AS last_event_at,
                          COUNT(*) AS event_count, MAX(CASE WHEN status='error' THEN 1 ELSE 0 END) AS has_error,
                          MAX(tenant_id) AS tenant_id, MAX(user_id_hash) AS user_id_hash,
                          MAX(session_id) AS session_id, MAX(conversation_id) AS conversation_id,
                          ROUND(SUM(COALESCE(duration_ms, 0)), 2) AS total_span_ms
                   FROM events GROUP BY trace_id ORDER BY last_event_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def trace(self, trace_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE trace_id=? ORDER BY COALESCE(start_time,timestamp), id", (trace_id,)
            ).fetchall()
        return [self._event(row) for row in rows]

    def metrics(self) -> dict[str, Any]:
        with self._connect() as connection:
            totals = connection.execute(
                """SELECT COUNT(DISTINCT trace_id) AS traces, COUNT(*) AS events,
                          SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                          AVG(CASE WHEN event_type LIKE 'gen_ai.%' THEN duration_ms END) AS avg_llm_ms
                   FROM events"""
            ).fetchone()
            models = connection.execute(
                """SELECT json_extract(attributes_json, '$."gen_ai.request.model"') AS model, COUNT(*) AS calls,
                          SUM(COALESCE(json_extract(attributes_json, '$."gen_ai.usage.input_tokens"'), 0)) AS input_tokens,
                          SUM(COALESCE(json_extract(attributes_json, '$."gen_ai.usage.output_tokens"'), 0)) AS output_tokens
                   FROM events WHERE event_type LIKE 'gen_ai.%' GROUP BY model ORDER BY calls DESC LIMIT 20"""
            ).fetchall()
        return {**dict(totals), "models": [dict(row) for row in models]}
