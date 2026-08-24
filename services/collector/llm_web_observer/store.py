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
CREATE TABLE IF NOT EXISTS policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    match_type TEXT NOT NULL,
    action TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS client_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_ip TEXT,
    user_agent TEXT NOT NULL,
    browser TEXT,
    platform TEXT,
    language TEXT,
    screen TEXT,
    timezone TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_context_seen ON client_contexts(seen_at DESC);
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

    @staticmethod
    def _call_kind(attributes: dict[str, Any]) -> str:
        explicit = attributes.get("lwo.call.kind")
        if explicit:
            return str(explicit)
        message = str(attributes.get("lwo.user.message") or "")
        if message.startswith("# Recent activity"):
            return "internal.recommendations"
        if message.startswith("Generate a title for this conversation"):
            return "internal.title"
        return "user.chat" if message else "internal.unknown"

    def conversations(self, limit: int = 100, include_internal: bool = False) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, timestamp, trace_id, service, status, tenant_id, user_id_hash,
                          session_id, conversation_id, duration_ms, attributes_json
                   FROM events WHERE event_type='gen_ai.chat'
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            attributes = json.loads(item.pop("attributes_json"))
            call_kind = self._call_kind(attributes)
            if not include_internal and call_kind != "user.chat":
                continue
            item.update({
                "username": attributes.get("enduser.name"),
                "source_ip": attributes.get("client.address"),
                "message": attributes.get("lwo.user.message"),
                "response": attributes.get("lwo.assistant.message"),
                "model": attributes.get("gen_ai.request.model"),
                "input_tokens": attributes.get("gen_ai.usage.input_tokens"),
                "output_tokens": attributes.get("gen_ai.usage.output_tokens"),
                "policy_action": attributes.get("lwo.policy.action", "allow"),
                "policy_rule": attributes.get("lwo.policy.rule"),
                "context_messages": attributes.get("lwo.context.messages", []),
                "fingerprint": attributes.get("client.fingerprint"),
                "user_agent": attributes.get("user_agent.original"),
                "browser": attributes.get("client.browser"),
                "platform": attributes.get("client.platform"),
                "language": attributes.get("client.language"),
                "screen": attributes.get("client.screen"),
                "timezone": attributes.get("client.timezone"),
                "identity_confidence": attributes.get("client.identity.confidence"),
                "call_kind": call_kind,
            })
            result.append(item)
        return result

    def record_client(self, context: dict[str, Any], source_ip: str | None) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO client_contexts
                   (seen_at,fingerprint,session_id,source_ip,user_agent,browser,platform,language,screen,timezone)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (now, context["fingerprint"], context["session_id"], source_ip, context["user_agent"],
                 context.get("browser"), context.get("platform"), context.get("language"),
                 context.get("screen"), context.get("timezone")),
            )
        return {**context, "source_ip": source_ip, "seen_at": now}

    def latest_client(self, max_age_seconds: int = 300) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM client_contexts
                   WHERE seen_at >= datetime('now', ?)
                   ORDER BY seen_at DESC LIMIT 1""",
                (f"-{max_age_seconds} seconds",),
            ).fetchone()
        return dict(row) if row else None

    def policies(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM policies ORDER BY updated_at DESC, id DESC").fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def create_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO policies
                   (created_at,updated_at,name,pattern,match_type,action,enabled,description)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now, now, policy["name"], policy["pattern"], policy["match_type"], policy["action"],
                 int(policy["enabled"]), policy["description"]),
            )
            policy_id = cursor.lastrowid
        return next(item for item in self.policies() if item["id"] == policy_id)

    def update_policy(self, policy_id: int, policy: dict[str, Any]) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE policies SET updated_at=?,name=?,pattern=?,match_type=?,action=?,enabled=?,description=?
                   WHERE id=?""",
                (now, policy["name"], policy["pattern"], policy["match_type"], policy["action"],
                 int(policy["enabled"]), policy["description"], policy_id),
            )
        if not cursor.rowcount:
            return None
        return next(item for item in self.policies() if item["id"] == policy_id)

    def delete_policy(self, policy_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM policies WHERE id=?", (policy_id,))
        return bool(cursor.rowcount)
