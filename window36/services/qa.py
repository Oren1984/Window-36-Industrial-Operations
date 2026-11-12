"""Simulated work completion, QA evidence and readiness to resume production."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from window36.models import Approval, Plan, QAEvidence, Reservation, Shutdown, StockItem, WorkRecord
from window36.services import approvals, policies
from window36.services.common import (
    ConflictError,
    Ctx,
    DomainError,
    PolicyBlocked,
    audit,
    audit_blocked,
    now,
    require,
)

CHECKS = {
    "QA-RCV": "Receiving inspection of installed part (revision + serial)",
    "QA-ALIGN": "Alignment and torque verification",
    "QA-RUN": "Functional run test before restart",
}


def required_checks(plan: Plan) -> list[str]:
    if plan.scope == "full":
        return ["QA-RCV", "QA-ALIGN", "QA-RUN"]
    if plan.scope == "reduced":
        return ["QA-RUN"]
    return []


def _approved_open_plan(ctx: Ctx, plan_id: int) -> Plan:
    plan = ctx.session.get(Plan, plan_id)
    if plan is None:
        raise DomainError(f"Plan {plan_id} not found")
    if plan.status != "approved":
        raise DomainError(f"Plan {plan.plan_key} r{plan.revision} is {plan.status}; work and QA need an approved plan")
    return plan


def completed_tasks(session, plan_id: int) -> set[str]:
    return set(session.scalars(select(WorkRecord.task_code).where(WorkRecord.plan_id == plan_id)).all())


def record_work(ctx: Ctx, plan_id: int, task_code: str) -> WorkRecord:
    plan = ctx.session.get(Plan, plan_id)
    if plan is None:
        raise DomainError(f"Plan {plan_id} not found")
    sd = ctx.session.get(Shutdown, plan.shutdown_id)
    require(ctx, "work.record", site_id=sd.site_id, entity="plan", entity_id=str(plan_id))
    plan = _approved_open_plan(ctx, plan_id)
    tasks = {t["code"]: t for t in plan.schedule}
    if task_code not in tasks:
        raise DomainError(f"Task {task_code} is not part of {plan.plan_key} r{plan.revision}")
    existing = ctx.session.scalar(
        select(WorkRecord).where(WorkRecord.plan_id == plan_id, WorkRecord.task_code == task_code)
    )
    if existing:
        return existing
    from window36.models import Task

    deps = ctx.session.scalar(select(Task.depends_on).where(Task.shutdown_id == sd.id, Task.code == task_code)) or []
    done = completed_tasks(ctx.session, plan_id)
    open_deps = [d for d in deps if d in tasks and d not in done]
    if open_deps:
        raise DomainError(f"Task {task_code} depends on unfinished task(s): {', '.join(open_deps)}")
    rec = WorkRecord(plan_id=plan_id, task_code=task_code, actor=ctx.actor.username, sim_time=now(ctx.session))
    nested = ctx.session.begin_nested()
    try:
        ctx.session.add(rec)
        ctx.session.flush()
        nested.commit()
    except IntegrityError as exc:
        nested.rollback()
        raise ConflictError("Task completion recorded concurrently") from exc
    audit(
        ctx,
        "work.complete",
        "plan",
        str(plan_id),
        "applied",
        f"Simulated completion of {task_code} ({tasks[task_code]['name']}) for {plan.plan_key} r{plan.revision}",
        site_id=sd.site_id,
    )
    return rec


def record_qa_evidence(
    ctx: Ctx,
    plan_id: int,
    check_code: str,
    result: str,
    evidence_ref: str,
    part_revision: str | None = None,
    stock_item_id: str | None = None,
    note: str = "",
) -> QAEvidence:
    plan = ctx.session.get(Plan, plan_id)
    if plan is None:
        raise DomainError(f"Plan {plan_id} not found")
    sd = ctx.session.get(Shutdown, plan.shutdown_id)
    require(ctx, "qa.record", site_id=sd.site_id, entity="plan", entity_id=str(plan_id))
    if check_code not in CHECKS:
        raise DomainError(f"Unknown QA check {check_code}")
    if result not in ("pass", "fail"):
        raise DomainError("QA result must be 'pass' or 'fail'")
    if not evidence_ref.strip():
        raise DomainError("An evidence reference is required")
    ev = QAEvidence(
        plan_id=plan_id,
        check_code=check_code,
        result=result,
        part_revision=(part_revision or None) and part_revision.strip().upper(),
        stock_item_id=stock_item_id or None,
        evidence_ref=evidence_ref.strip()[:80],
        note=note[:500] or None,
        actor=ctx.actor.username,
        sim_time=now(ctx.session),
    )
    ctx.session.add(ev)
    ctx.session.flush()
    audit(
        ctx,
        "qa.evidence",
        "plan",
        str(plan_id),
        "applied",
        f"{check_code} {result.upper()} for {plan.plan_key} r{plan.revision}"
        + (f", part rev {ev.part_revision} ({ev.stock_item_id})" if ev.part_revision else ""),
        site_id=sd.site_id,
        evidence_refs=[ev.evidence_ref],
    )
    return ev


def readiness_problems(session, plan: Plan) -> list[str]:
    problems: list[str] = []
    if plan.scope == "postpone":
        return ["Postponement plan: no shutdown work executed in this window"]
    if plan.status != "approved" or approvals.active_approval(session, plan.id, "plan_approval") is None:
        problems.append(f"Plan {plan.plan_key} r{plan.revision} has no active approval (status {plan.status})")
    ok, changed = approvals.inputs_current(session, plan)
    if not ok:
        problems.append("Plan inputs changed since approval: " + ", ".join(changed))
    done = completed_tasks(session, plan.id)
    missing_tasks = [t["code"] for t in plan.schedule if t["code"] not in done]
    if missing_tasks:
        problems.append("Tasks not completed: " + ", ".join(missing_tasks))
    for code in required_checks(plan):
        latest = session.scalar(
            select(QAEvidence)
            .where(QAEvidence.plan_id == plan.id, QAEvidence.check_code == code)
            .order_by(QAEvidence.id.desc())
            .limit(1)
        )
        if latest is None:
            older = session.scalar(
                select(Plan.revision)
                .join(QAEvidence, QAEvidence.plan_id == Plan.id)
                .where(Plan.plan_key == plan.plan_key, Plan.id != plan.id, QAEvidence.check_code == code)
                .limit(1)
            )
            if older is not None:
                problems.append(f"{code}: evidence is stale (tied to plan revision r{older}, not r{plan.revision})")
            else:
                problems.append(f"{code}: evidence missing")
            continue
        if latest.result != "pass":
            problems.append(f"{code}: latest evidence {latest.evidence_ref} FAILED")
            continue
        if plan.scope == "full" and code in ("QA-RCV", "QA-RUN"):
            if latest.part_revision != plan.part_revision or latest.stock_item_id != plan.source_stock_item_id:
                problems.append(
                    f"{code}: evidence refers to rev {latest.part_revision or '—'} / "
                    f"{latest.stock_item_id or '—'}, plan installs rev {plan.part_revision} / "
                    f"{plan.source_stock_item_id}"
                )
    return problems


def record_readiness(ctx: Ctx, plan_id: int, reason: str) -> Approval:
    plan = ctx.session.get(Plan, plan_id, with_for_update=True)
    if plan is None:
        raise DomainError(f"Plan {plan_id} not found")
    sd = ctx.session.get(Shutdown, plan.shutdown_id)
    require(ctx, "readiness.record", site_id=sd.site_id, entity="plan", entity_id=str(plan_id))
    existing = approvals.active_approval(ctx.session, plan.id, "readiness")
    if existing is not None:
        return existing
    facts = {
        "qa_problems": readiness_problems(ctx.session, plan),
        "needs_part": plan.scope == "full",
        "est_cost_eur": plan.est_cost_eur,
    }
    if plan.scope == "full" and plan.source_stock_item_id:
        item = ctx.session.get(StockItem, plan.source_stock_item_id)
        ok, note = approvals.inventory.compatibility(ctx.session, item.part_number, sd.line_code, item.revision)
        facts.update(
            stock_item_id=item.id,
            part_revision=item.revision,
            revision_compatible=ok,
            compatibility_note=note,
            revision_status=item.revision_status,
        )
    results = policies.evaluate("readiness", facts)
    failed = policies.first_failure(results)
    if failed:
        msg = f"Readiness blocked by {failed['policy_id']} v{failed['version']}: {failed['detail']}"
        audit_blocked(
            ctx,
            "readiness.record",
            "plan",
            str(plan.id),
            msg,
            site_id=sd.site_id,
            policy_id=failed["policy_id"],
            policy_version=failed["version"],
        )
        raise PolicyBlocked(msg, results)
    if not reason.strip():
        raise DomainError("A readiness note is required")
    evidence = [e.evidence_ref for e in ctx.session.scalars(select(QAEvidence).where(QAEvidence.plan_id == plan.id))]
    text = f"Ready to resume production on {sd.line_name} after {plan.plan_key} r{plan.revision}. {reason.strip()}"
    appr = approvals._record(ctx, plan, "readiness", sd.site_id, text, results, f"readiness:{plan.id}")
    for res in ctx.session.scalars(
        select(Reservation).where(Reservation.plan_id == plan.id, Reservation.status == "active")
    ).all():
        res.status = "consumed"
        item = ctx.session.get(StockItem, res.stock_item_id)
        item.status = "consumed"
        item.site_id = sd.site_id
    audit(
        ctx,
        "readiness.record",
        "plan",
        str(plan.id),
        "permitted",
        "POL-QA-007 QA release: all required evidence present and passed",
        site_id=sd.site_id,
        policy_id="POL-QA-007",
        policy_version="1.0",
        evidence_refs=evidence,
    )
    audit(ctx, "readiness.record", "plan", str(plan.id), "applied", text, site_id=sd.site_id, evidence_refs=evidence)
    return appr
