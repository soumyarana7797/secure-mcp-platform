"""What may be written to the audit trail from tool arguments.

Rule: a value is logged literally only when the tool's own schema says it is NOT free text
(enum, number, boolean, date). Free text, which may hold personal data, is logged as
{type, length} only. A SHA-256 of the full arguments lets identical calls be correlated.
"""

import hashlib
import json
import re
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authori[sz]ation|cookie|credential|private|ssn)",
    re.I,
)
SAFE_FORMATS = {"date", "date-time", "uuid"}
SAFE_TYPES = {"integer", "number", "boolean", "null"}
MAX_KEYS = 25


def args_hash(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _branches(prop: dict[str, Any]) -> list[dict[str, Any]]:
    return prop.get("anyOf") or prop.get("oneOf") or [prop]


def _schema_says_safe(prop: dict[str, Any] | None) -> bool:
    """Safe only if EVERY branch the schema allows is non-free-text."""
    if not prop:
        return False
    for branch in _branches(prop):
        if "enum" in branch or "const" in branch:
            continue
        if branch.get("type") in SAFE_TYPES or branch.get("format") in SAFE_FORMATS:
            continue
        return False
    return True


def _describe(value: Any) -> Any:
    if isinstance(value, str):
        return {"type": "string", "len": len(value)}
    if isinstance(value, list):
        return {"type": "array", "len": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": len(value)}
    return {"type": type(value).__name__}


def redact_args(arguments: dict[str, Any] | None, schema: dict[str, Any] | None) -> dict[str, Any]:
    properties = (schema or {}).get("properties", {})
    out: dict[str, Any] = {}
    for index, (key, value) in enumerate((arguments or {}).items()):
        if index >= MAX_KEYS:
            out["_truncated"] = len(arguments or {}) - MAX_KEYS
            break
        key = str(key)[:64]
        if SENSITIVE_KEY.search(key):
            out[key] = "[REDACTED]"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe = _schema_says_safe(properties.get(key))
            out[key] = value if safe else _describe(value)
        else:
            out[key] = _describe(value)
    return out
