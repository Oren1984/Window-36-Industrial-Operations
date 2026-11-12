"""Structured JSON diagnostic logs with correlation IDs and secret redaction.

Diagnostic logs go to stdout; business audit events go to the audit_event table.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

_SENSITIVE_KEYS = re.compile(r"(password|passwd|secret|token|api[_-]?key|credential)", re.I)
_DSN_PASSWORD = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")
_KV_SECRET = re.compile(r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)([^\s,;]+)", re.I)


def redact(value: str) -> str:
    value = _DSN_PASSWORD.sub(r"\1***\3", value)
    return _KV_SECRET.sub(r"\1***", value)


def redact_mapping(data: dict) -> dict:
    out = {}
    for key, val in data.items():
        if _SENSITIVE_KEYS.search(str(key)):
            out[key] = "***"
        elif isinstance(val, dict):
            out[key] = redact_mapping(val)
        elif isinstance(val, str):
            out[key] = redact(val)
        else:
            out[key] = val
    return out


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:16]
    correlation_id.set(cid)
    return cid


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": correlation_id.get(),
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(redact_mapping(extra))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("window36")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    _configured = True


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
