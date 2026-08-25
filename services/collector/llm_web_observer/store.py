from __future__ import annotations

import base64
import hashlib
import json
import queue
import sqlite3
import threading
import time
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
    direction TEXT NOT NULL DEFAULT 'request',
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
CREATE TABLE IF NOT EXISTS inbound_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at REAL NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'http',
    user_key TEXT,
    identity_kind TEXT,
    source_ip TEXT,
    identity_user_id TEXT,
    identity_username TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbound_time ON inbound_requests(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_user ON inbound_requests(user_key, received_at DESC);
CREATE TABLE IF NOT EXISTS conversation_owners (
    conversation_id TEXT PRIMARY KEY,
    user_key TEXT NOT NULL,
    confidence TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    user_id TEXT,
    username TEXT
);
"""

REST_LEAD_SECONDS = 30
WS_LEAD_SECONDS = 1800

# Service-to-service / background polling paths that must never participate in
# user identity attribution. These endpoints fire every few seconds with a
# shared service credential and would otherwise drown out real users.
INBOUND_NOISE_PREFIXES = (
    "/api/worker/",
    "/api/internal/",
    "/api/v1/auth/status",
    "/health",
    "/healthz",
    "/metrics",
    "/api/v1/system",
)


def repair_mojibake(value: Any) -> Any:
    if isinstance(value, list):
        return [repair_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_mojibake(item) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    original_cjk = sum("\u3400" <= char <= "\u9fff" for char in value)
    repaired_cjk = sum("\u3400" <= char <= "\u9fff" for char in repaired)
    controls = sum("\u0080" <= char <= "\u009f" for char in value)
    return repaired if repaired_cjk > original_cjk and controls else value


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._subscribers_lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    def subscribe(self) -> queue.Queue:
        """Register a live event subscriber. Returns a queue that receives each
        ingested event dict (bounded, so a slow client never blocks ingestion)."""
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._subscribers_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, events: list[dict]) -> None:
        if not events:
            return
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            for event in events:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    break

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Add columns that post-date the original schema without disturbing existing data."""
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(inbound_requests)")}
        if "identity_user_id" not in existing:
            connection.execute("ALTER TABLE inbound_requests ADD COLUMN identity_user_id TEXT")
        if "identity_username" not in existing:
            connection.execute("ALTER TABLE inbound_requests ADD COLUMN identity_username TEXT")
        owners = {row["name"] for row in connection.execute("PRAGMA table_info(conversation_owners)")}
        if "user_id" not in owners:
            connection.execute("ALTER TABLE conversation_owners ADD COLUMN user_id TEXT")
        if "username" not in owners:
            connection.execute("ALTER TABLE conversation_owners ADD COLUMN username TEXT")
        policies = {row["name"] for row in connection.execute("PRAGMA table_info(policies)")}
        if "direction" not in policies:
            connection.execute("ALTER TABLE policies ADD COLUMN direction TEXT NOT NULL DEFAULT 'request'")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def ingest(self, events: Iterable[TelemetryEvent]) -> int:
        rows = []
        received_at = datetime.now(UTC).isoformat()
        chat_payloads: list[dict] = []
        for event in events:
            data = event.model_dump(mode="json")
            rows.append((
                received_at, data["timestamp"], data["event_type"], data["trace_id"], data["span_id"],
                data["parent_span_id"], data["service"], data["span_kind"], data["start_time"], data["end_time"],
                data["duration_ms"], data["status"], data["tenant_id"], data["user_id_hash"], data["session_id"],
                data["conversation_id"], data["interaction_id"], json.dumps(sanitize(data["attributes"]), separators=(",", ":")),
            ))
            if data["event_type"] == "gen_ai.chat":
                chat_payloads.append(self._expand_attributes(data["attributes"], {
                    "timestamp": data["timestamp"], "trace_id": data["trace_id"],
                    "conversation_id": data["conversation_id"], "session_id": data["session_id"],
                    "duration_ms": data["duration_ms"], "status": data["status"],
                    "user_id_hash": data["user_id_hash"],
                }))
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
            accepted = connection.total_changes - before
        self._broadcast(chat_payloads)
        return accepted

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

    @staticmethod
    def _expand_attributes(attributes: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Flatten gen_ai.chat attributes into readable conversation fields."""
        call_kind = EventStore._call_kind(attributes)
        response = repair_mojibake(attributes.get("lwo.assistant.message"))
        item = {
            "username": attributes.get("enduser.name"),
            "user_id": attributes.get("enduser.id"),
            "source_ip": attributes.get("client.address"),
            "message": repair_mojibake(attributes.get("lwo.user.message")),
            "response": response,
            "model": attributes.get("gen_ai.request.model"),
            "input_tokens": attributes.get("gen_ai.usage.input_tokens"),
            "output_tokens": attributes.get("gen_ai.usage.output_tokens"),
            "policy_action": attributes.get("lwo.policy.action", "allow"),
            "policy_rule": attributes.get("lwo.policy.rule"),
            "context_messages": repair_mojibake(attributes.get("lwo.context.messages", [])),
            "fingerprint": attributes.get("client.fingerprint"),
            "user_agent": attributes.get("user_agent.original"),
            "browser": attributes.get("client.browser"),
            "platform": attributes.get("client.platform"),
            "language": attributes.get("client.language"),
            "screen": attributes.get("client.screen"),
            "timezone": attributes.get("client.timezone"),
            "identity_confidence": attributes.get("client.identity.confidence"),
            "call_kind": call_kind,
        }
        if extra:
            item.update(extra)
        return item

    def conversations(self, limit: int = 100, include_internal: bool = False, q: str | None = None,
                      user: str | None = None, model: str | None = None, action: str | None = None,
                      call_kind: str | None = None, since: str | None = None, until: str | None = None,
                      offset: int = 0) -> dict[str, Any]:
        """Searchable, paginated conversation listing.

        Returns ``{"items": [...], "total": N, "limit": ..., "offset": ...}``. ``q`` performs a
        case-insensitive fuzzy match across user message, assistant response, username and model
        (applied after SQL filtering, alongside the internal-call visibility rule).
        """
        where = ["event_type='gen_ai.chat'"]
        params: list[Any] = []
        if q and q.strip():
            like = f"%{q.strip()}%"
            where.append("(json_extract(attributes_json, '$.\"lwo.user.message\"') LIKE ? "
                         "OR json_extract(attributes_json, '$.\"lwo.assistant.message\"') LIKE ? "
                         "OR json_extract(attributes_json, '$.\"enduser.name\"') LIKE ? "
                         "OR json_extract(attributes_json, '$.\"gen_ai.request.model\"') LIKE ? "
                         "OR user_id_hash LIKE ?)")
            params.extend([like, like, like, like, like])
        if model:
            where.append("json_extract(attributes_json, '$.\"gen_ai.request.model\"') LIKE ?")
            params.append(f"%{model}%")
        if action:
            where.append("json_extract(attributes_json, '$.\"lwo.policy.action\"') = ?")
            params.append(action)
        if call_kind:
            where.append("json_extract(attributes_json, '$.\"lwo.call.kind\"') = ?")
            params.append(call_kind)
        if user:
            where.append("(user_id_hash LIKE ? OR json_extract(attributes_json, '$.\"enduser.id\"') LIKE ? "
                         "OR json_extract(attributes_json, '$.\"enduser.name\"') LIKE ?)")
            params.extend([f"%{user}%", f"%{user}%", f"%{user}%"])
        if since:
            where.append("timestamp >= ?")
            params.append(since)
        if until:
            where.append("timestamp <= ?")
            params.append(until)
        where_sql = " AND ".join(where)
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM events WHERE {where_sql}", params).fetchone()[0]
            rows = connection.execute(
                f"""SELECT id, timestamp, trace_id, service, status, tenant_id, user_id_hash,
                           session_id, conversation_id, duration_ms, attributes_json
                    FROM events WHERE {where_sql}
                    ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            attributes = json.loads(item.pop("attributes_json"))
            item.update(self._expand_attributes(attributes, {
                "user_id_hash": item.get("user_id_hash"),
                "trace_id": item.get("trace_id"),
                "conversation_id": item.get("conversation_id"),
                "session_id": item.get("session_id"),
                "duration_ms": item.get("duration_ms"),
                "status": item.get("status"),
            }))
            if not include_internal and item["call_kind"] != "user.chat":
                continue
            if not include_internal and not item.get("response") and item.get("policy_action") != "block":
                continue
            result.append(item)
        return {"items": result, "total": total, "limit": limit, "offset": offset}

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

    @staticmethod
    def _user_key(identity_kind: str, token: str) -> str:
        """Pseudonymous stable identity: salted hash, raw token never stored."""
        digest = hashlib.sha256(f"{identity_kind}:{token}".encode()).hexdigest()[:16]
        return f"u-{digest}"

    @staticmethod
    def _decode_jwt_identity(token: str) -> tuple[str | None, str | None]:
        """Best-effort extract of the *stable* user identity from a DeepTutor JWT.

        DeepTutor signs a JWT whose payload is ``{sub: username, role, uid: user_id, exp, iat}``.
        We only base64-decode the payload segment (no signature verification — this is for
        attribution, not authorization) to recover ``uid`` and ``sub``. This gives a stable
        user id that survives token refresh, unlike the credential hash (``user_key``).
        Returns ``(user_id, username)`` or ``(None, None)`` when the token isn't a decodable JWT.
        """
        token = (token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            return None, None
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None, None
            payload_b64 = parts[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None, None
        user_id = payload.get("uid")
        username = payload.get("sub")
        return (str(user_id) if user_id else None), (str(username) if username else None)

    def record_inbound(self, method: str, path: str, kind: str, authorization: str | None,
                       cookie: str | None, source_ip: str | None,
                       query_token: str | None = None) -> dict[str, Any]:
        """Store one mirrored inbound request. `path` must already have its query
        string stripped (raw tokens must never reach the database); the WS auth
        token travels separately via `query_token` and is only stored hashed."""
        user_key = identity_kind = None
        raw_token = None
        if authorization and authorization.strip():
            identity_kind = "authorization"
            raw_token = authorization.strip()
            user_key = self._user_key(identity_kind, raw_token)
        elif query_token:
            identity_kind = "query_token"
            raw_token = query_token
            user_key = self._user_key(identity_kind, raw_token)
        elif cookie:
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "dt_token" and value:
                    identity_kind = "cookie"
                    raw_token = value
                    user_key = self._user_key(identity_kind, value)
                    break
        user_id, username = self._decode_jwt_identity(raw_token) if raw_token else (None, None)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO inbound_requests
                   (received_at,method,path,kind,user_key,identity_kind,source_ip,identity_user_id,identity_username)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (time.time(), method, path[:500], kind, user_key, identity_kind, source_ip, user_id, username),
            )
        return {"recorded": True, "user_key": user_key, "identity_kind": identity_kind,
                "user_id": user_id, "username": username}

    def resolve_identity(self, conversation_id: str | None, before_epoch: float,
                         rest_lead: int = REST_LEAD_SECONDS, ws_lead: int = WS_LEAD_SECONDS) -> dict[str, Any] | None:
        """Attribute an LLM call to an inbound caller without touching the business system.

        Priority: conversation stickiness > recent REST request > open websocket session.
        Never claims ownership from ambiguous evidence.
        """
        with self._connect() as connection:
            if conversation_id:
                owner = connection.execute(
                    "SELECT user_key, user_id, username, confidence, claimed_at FROM conversation_owners WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
                if owner:
                    return {"user_key": owner["user_key"], "user_id": owner["user_id"],
                            "username": owner["username"], "confidence": "sticky",
                            "source": "inbound", "claimed_at": owner["claimed_at"]}
            for kind, lead, confidence in (("http", rest_lead, "inferred"),
                                            ("ws", ws_lead, "inferred-ws")):
                noise_sql = " OR ".join("path LIKE ?" for _ in INBOUND_NOISE_PREFIXES)
                noise_params = [f"{prefix}%" for prefix in INBOUND_NOISE_PREFIXES]
                rows = connection.execute(
                    f"""SELECT user_key, identity_user_id, identity_username, received_at, source_ip
                       FROM inbound_requests
                       WHERE kind=? AND user_key IS NOT NULL AND received_at<=? AND received_at>=?
                       AND NOT ({noise_sql})
                       ORDER BY received_at DESC LIMIT 50""",
                    (kind, before_epoch, before_epoch - lead, *noise_params),
                ).fetchall()
                if not rows:
                    continue
                distinct = {row["user_key"] for row in rows}
                best = rows[0]
                result = {"user_key": best["user_key"], "user_id": best["identity_user_id"],
                          "username": best["identity_username"], "confidence": confidence,
                          "source": "inbound", "candidates": len(rows),
                          "source_ip": best["source_ip"]}
                if len(distinct) == 1:
                    if conversation_id:
                        with self._lock:
                            connection.execute(
                                """INSERT OR IGNORE INTO conversation_owners
                                   (conversation_id,user_key,user_id,username,confidence,claimed_at)
                                   VALUES (?,?,?,?,?,?)""",
                                (conversation_id, best["user_key"], best["identity_user_id"],
                                 best["identity_username"], confidence, time.time()),
                            )
                    return result
                result["confidence"] = f"{confidence}-ambiguous"
                result["distinct_users"] = len(distinct)
                return result
        return None

    def policies(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM policies ORDER BY updated_at DESC, id DESC").fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def create_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO policies
                   (created_at,updated_at,name,pattern,match_type,action,direction,enabled,description)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, now, policy["name"], policy["pattern"], policy["match_type"], policy["action"],
                 policy.get("direction", "request"), int(policy["enabled"]), policy["description"]),
            )
            policy_id = cursor.lastrowid
        return next(item for item in self.policies() if item["id"] == policy_id)

    def update_policy(self, policy_id: int, policy: dict[str, Any]) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE policies SET updated_at=?,name=?,pattern=?,match_type=?,action=?,direction=?,enabled=?,description=?
                   WHERE id=?""",
                (now, policy["name"], policy["pattern"], policy["match_type"], policy["action"],
                 policy.get("direction", "request"), int(policy["enabled"]), policy["description"], policy_id),
            )
        if not cursor.rowcount:
            return None
        return next(item for item in self.policies() if item["id"] == policy_id)

    def delete_policy(self, policy_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM policies WHERE id=?", (policy_id,))
        return bool(cursor.rowcount)
