"""Shared service plumbing: actor context, permission checks, audit, clock and input versions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from window36.logging_setup import correlation_id, log, new_correlation_id
from window36.models import AppUser, AuditEvent, InputChange, InputVersion, ScenarioState

logger = logging.getLogger("window36.services")


class DomainError(Exception):
    """Business rule violation, shown to the user."""


class PermissionDenied(DomainError):
    pass


class ConflictError(DomainError):
    pass


class PolicyBlocked(DomainError):
    def __init__(self, message: str, policy_results: list[dict] | None = None):
        super().__init__(message)
        self.policy_results = policy_results or []


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "planner": {"document.import", "document.review", "plan.compute", "scenario.control", "event.ingest"},
    "site_operator": {"document.import", "receiving.record", "work.record", "qa.record", "event.ingest"},
    "approver": {"plan.compute", "plan.approve", "plan.reject", "donor.release", "readiness.record"},
    "viewer": set(),
}

ROLE_LABELS = {
    "planner": "Planner",
    "site_operator": "Site operator",
    "approver": "Approver",
    "viewer": "Read-only demo viewer",
}


@dataclass(frozen=True)
class Actor:
    username: str
    role: str
    site_id: str | None
    display_name: str = ""

    @classmethod
    def from_user(cls, user: AppUser) -> Actor:
        return cls(user.username, user.role, user.site_id, user.display_name)


SYSTEM = Actor("system", "planner", None, "Scenario engine")


@dataclass
class Ctx:
    session: Session
    actor: Actor
    correlation: str = field(
        default_factory=lambda: correlation_id.get() if correlation_id.get() != "-" else new_correlation_id()
    )


def load_actor(session: Session, username: str) -> Actor:
    user = session.scalar(select(AppUser).where(AppUser.username == username))
    if user is None:
        raise PermissionDenied(f"Unknown user {username}")
    return Actor.from_user(user)


def require(ctx: Ctx, permission: str, site_id: str | None = None, entity: str = "", entity_id: str = "") -> None:
    """Enforce role permission and site scope; blocked attempts are audited."""
    actor = ctx.actor
    reason = None
    if permission not in ROLE_PERMISSIONS.get(actor.role, set()):
        reason = f"Role '{actor.role}' lacks permission '{permission}'"
    elif site_id and actor.site_id and actor.site_id != site_id:
        reason = f"User scoped to site {actor.site_id} cannot act on site {site_id}"
    if reason:
        audit_blocked(ctx, permission, entity or "permission", entity_id or permission, reason, site_id=site_id)
        log(
            logger,
            logging.WARNING,
            "permission denied",
            actor=actor.username,
            permission=permission,
            site=site_id,
            status="blocked",
        )
        raise PermissionDenied(reason)


def now(session: Session) -> datetime:
    state = session.get(ScenarioState, 1)
    if state is None:
        raise DomainError("Scenario not seeded")
    return state.now_utc.astimezone(UTC)


def audit(
    ctx: Ctx,
    action: str,
    entity_type: str,
    entity_id: str,
    outcome: str,
    reason: str,
    *,
    site_id: str | None = None,
    policy_id: str | None = None,
    policy_version: str | None = None,
    evidence_refs: list[str] | None = None,
) -> AuditEvent:
    state = ctx.session.get(ScenarioState, 1)
    event = AuditEvent(
        sim_time=state.now_utc if state else datetime.now(UTC),
        recorded_at=datetime.now(UTC),
        actor=ctx.actor.username,
        actor_role=ctx.actor.role,
        site_id=site_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        outcome=outcome,
        reason=reason,
        policy_id=policy_id,
        policy_version=policy_version,
        evidence_refs=evidence_refs or [],
        correlation_id=ctx.correlation,
    )
    ctx.session.add(event)
    ctx.session.flush()
    return event


def audit_blocked(ctx: Ctx, action: str, entity_type: str, entity_id: str, reason: str, **kw) -> None:
    """Persist a 'blocked' audit event in its own transaction (the caller's will roll back)."""
    with Session(bind=ctx.session.get_bind()) as side:
        audit(Ctx(side, ctx.actor, ctx.correlation), action, entity_type, entity_id, "blocked", reason, **kw)
        side.commit()


def current_versions(session: Session, keys: list[str]) -> dict[str, int]:
    rows = session.scalars(select(InputVersion).where(InputVersion.key.in_(keys))).all()
    found = {r.key: r.version for r in rows}
    return {k: found.get(k, 1) for k in keys}


def bump_input(ctx: Ctx, key: str, reason: str) -> int:
    """Record a material input change. Approvals bound to the old version become stale."""
    row = ctx.session.get(InputVersion, key, with_for_update=True)
    if row is None:
        row = InputVersion(key=key, version=1)
        ctx.session.add(row)
    row.version += 1
    ctx.session.add(
        InputChange(key=key, version=row.version, reason=reason, actor=ctx.actor.username, sim_time=now(ctx.session))
    )
    ctx.session.flush()
    from window36.services import approvals  # local import avoids a cycle

    approvals.invalidate_for_input(ctx, key, reason)
    return row.version
