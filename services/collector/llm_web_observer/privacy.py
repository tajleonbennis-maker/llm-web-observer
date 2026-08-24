from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"(?i)(^|[._-])(authorization|cookie|password|passwd|secret|token|api[_-]?key|private[_-]?key|prompt|completion|input[._-]?messages|output[._-]?messages|system[_-]?instructions)([._-]|$)"
)
SENSITIVE_VALUE = [
    re.compile(r"(?i)bearer\s+[a-z0-9._~-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        return {
            str(key)[:160]: REDACTED if SENSITIVE_KEY.search(str(key)) else sanitize(item, depth=depth + 1)
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, list):
        return [sanitize(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        result = value[:4000]
        for pattern in SENSITIVE_VALUE:
            result = pattern.sub(REDACTED, result)
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]
