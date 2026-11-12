"""Application/database health, operational counters and alerts."""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import func, select, text

from window36.domain.timeutil import utc_str
from window36.models import (
    Approval,
    AuditEvent,
    Document,
    ExternalEvent,
    FactConflict,
    QAEvidence,
    Reservation,
    RouteLeg,
)
from window36.services import planning, qa
from window36.services.common import now


def db_health(engine) -> dict:
    start = time.perf_counter()
    try:
        with engine.connect() as conn:
            version = conn.execute(text("select current_setting('server_version')")).scalar()
            conn.execute(text("select 1"))
        return {"ok": True, "latency_ms": round((time.perf_counter() - start) * 1000, 1), "server": version}
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        return {"ok": False, "latency_ms": None, "error": type(exc).__name__}


def _since_reset(session):
    last = session.scalar(select(func.max(AuditEvent.id)).where(AuditEvent.action == "scenario.reset"))
    return last or 0


def counters(session) -> dict:
    since = _since_reset(session)
    current = planning.current_plans(session)
    approved = [p for p in current if p.status == "approved"]
    pending_qa = 0
    for p in approved:
        for code in qa.required_checks(p):
            ok = session.scalar(
                select(QAEvidence.id)
                .where(QAEvidence.plan_id == p.id, QAEvidence.check_code == code, QAEvidence.result == "pass")
                .limit(1)
            )
            pending_qa += 0 if ok else 1
    return {
        "Ingestion failures": session.scalar(
            select(func.count()).select_from(Document).where(Document.status == "failed")
        ),
        "Documents awaiting review": session.scalar(
            select(func.count()).select_from(Document).where(Document.status == "staged")
        ),
        "Open data conflicts": session.scalar(
            select(func.count()).select_from(FactConflict).where(FactConflict.status == "open")
        ),
        "Stale plans": sum(1 for p in current if p.status == "stale"),
        "Stale approvals": session.scalar(select(func.count()).select_from(Approval).where(Approval.status == "stale")),
        "Blocked decisions": session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.outcome == "blocked", AuditEvent.id > since)
        ),
        "Pending QA checks": pending_qa,
        "Duplicate events ignored": session.scalar(select(func.coalesce(func.sum(ExternalEvent.duplicate_count), 0))),
        "Out-of-order events": session.scalar(
            select(func.count()).select_from(ExternalEvent).where(ExternalEvent.outcome == "out_of_order")
        ),
        "Active reservations": session.scalar(
            select(func.count()).select_from(Reservation).where(Reservation.status == "active")
        ),
    }


def alerts(session) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    t = now(session)
    c = counters(session)
    if c["Documents awaiting review"]:
        out.append(("warning", f"{c['Documents awaiting review']} document(s) staged and awaiting review"))
    if c["Open data conflicts"]:
        out.append(("error", f"{c['Open data conflicts']} unresolved conflict(s) between supplier evidence"))
    if c["Ingestion failures"]:
        out.append(("warning", f"{c['Ingestion failures']} document import(s) failed validation"))
    current = planning.current_plans(session)
    stale = [f"{p.plan_key} r{p.revision}" for p in current if p.status == "stale"]
    if stale:
        out.append(("error", "Stale plan(s) — replan required: " + ", ".join(stale)))
    feasible_full = [p for p in current if p.scope == "full" and p.feasible and p.status != "stale"]
    if current and not feasible_full:
        out.append(("error", "No full-scope plan fits the shutdown window"))
    if current and not any(p.feasible and p.status != "stale" for p in current):
        out.append(("error", "No feasible plan at all — escalate"))
    for leg in session.scalars(select(RouteLeg).where(RouteLeg.cutoff_at.is_not(None))).all():
        if t <= leg.cutoff_at <= t + timedelta(hours=3):
            out.append(
                ("warning", f"Cutoff soon: {leg.route_id} leg {leg.seq} ({leg.origin}) at " f"{utc_str(leg.cutoff_at)}")
            )
    if c["Pending QA checks"]:
        out.append(("info", f"{c['Pending QA checks']} required QA check(s) pending for the approved plan"))
    return out
