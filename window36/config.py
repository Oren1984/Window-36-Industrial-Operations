"""Environment configuration. Credentials never live in code; see .env.example."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    max_upload_bytes: int
    customs_policy_enabled: bool
    approver_spend_limit_eur: float
    log_level: str


def get_settings() -> Settings:
    url = os.environ.get("W36_DATABASE_URL")
    if not url:
        raise RuntimeError("W36_DATABASE_URL is not set (see .env.example)")
    return Settings(
        database_url=url,
        max_upload_bytes=int(os.environ.get("W36_MAX_UPLOAD_BYTES", str(64 * 1024))),
        customs_policy_enabled=_bool("W36_POLICY_CUSTOMS_ENABLED", False),
        approver_spend_limit_eur=float(os.environ.get("W36_APPROVER_SPEND_LIMIT_EUR", "15000")),
        log_level=os.environ.get("W36_LOG_LEVEL", "INFO"),
    )
