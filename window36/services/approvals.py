"""Approval decisions bound to immutable plan revisions and input versions."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from window36.logging_setup import log
from window36.models import Approval, Plan, Route, Shutdown, SiteJob, StockItem
from window36.services import inventory, policies
from window36.services.common import (
    ConflictError,
    Ctx,
    DomainError,
    PolicyBlocked,
    audit,
    audit_blocked,
    current_versions,
    now,
    require,
)

logger = logging.getLogger("window36.approvals")


def active_approval(session, plan_id: int, decision_type: str) -> Approval | None:
    return session.scalar(
        select(Approval).where(
            Approval.plan_id == plan_id, Approval.decision_type == decision_type, Approval.status == "active"
        )
    )


def active_or_stale_approval(session, plan_id: int) -> Approval | None:
    return session.scalar(
        select(Approval).where(Approval.plan_id == plan_id, Approval.decision_type == "plan_approval").limit(1)
    )


def approved_plan(session) -> Plan | None:
    return session.scalar(select(Plan).where(Plan.status == "approved").order_by(Plan.id.desc()).limit(1))


def inputs_current(session, plan: Plan) -> tuple[bool, list[str]]:
    cur = current_versions(session, list(plan.input_versions))
    changed = [k for k, v in plan.input_versions.items() if cur.get(k) != v]
    return not changed, changed


def _is_latest(session, plan: Plan) -> bool:
    newer = session.scalar(select(Plan.id).where(Plan.plan_key == plan.plan_key, Plan.revision > plan.revision))
    return newer is None


def _validate_open(ctx: Ctx, plan: Plan, action: str) -> None:
    def block(msg: str):
        audit_blocked(ctx, action, "plan", str(plan.id), msg, site_id=plan.source_site_id)
        raise DomainError(msg)

    if plan.status == "stale":
        block(f"Plan {plan.plan_key} r{plan.revision} is stale ({plan.status_reason}); replan required")
    if plan.status == "superseded" or not _is_latest(ctx.session, plan):
        block(f"Plan {plan.plan_key} r{plan.revision} is superseded by a newer revision")
    ok, changed = inputs_current(ctx.session, plan)
    if not ok:
        block(
            f"Plan {plan.plan_key} r{plan.revision} is stale: inputs changed since it was computed "
            f"({', '.join(changed)}); replan required"
        )
    if plan.status != "proposed":
        block(f"Plan {plan.plan_key} r{plan.revision} is {plan.status}")
    if not plan.feasible:
        block(f"Plan {plan.plan_key} r{plan.revision} is not feasible: " + "; ".join(plan.reasons))


def approval_facts(session, plan: Plan) -> dict:
    sd = session.get(Shutdown, plan.shutdown_id)
    facts: dict = {"est_cost_eur": plan.est_cost_eur, "needs_part": plan.scope == "full"}
    item = session.get(StockItem, plan.source_stock_item_id) if plan.source_stock_item_id else None
    if item is not None and plan.scope == "full":
        ok, note = inventory.compatibility(session, item.part_number, sd.line_code, item.revision)
        facts.update(
            stock_item_id=item.id,
            part_revision=item.revision,
            revision_status=item.revision_status,
            revision_compatible=ok,
            compatibility_note=note,
        )
    if plan.route_id and plan.scope == "full":
        route = session.get(Route, plan.route_id)
        facts.update(
            route_id=route.id,
            carrier_confirmed=route.carrier_confirmed,
            cross_site=route.origin_site_id != sd.site_id,
            cross_border=route.origin_site_id[:2] != sd.site_id[:2],
            customs_ref=route.customs_ref,
            source_site_id=route.origin_site_id,
            manifest=inventory.transfer_manifest(item, route.origin_site_id, route.destination_site_id, route.carrier),
        )
        rel = active_approval(session, plan.id, "donor_release")
        facts["donor_release"] = {"actor": rel.actor, "site_id": rel.site_id, "plan_id": rel.plan_id} if rel else None
    return facts


def _record(
    ctx: Ctx, plan: Plan, decision_type: str, site_id: str, reason: str, results: list[dict], key: str
) -> Approval:
    appr = Approval(
        plan_id=plan.id,
        decision_type=decision_type,
        site_id=site_id,
        actor=ctx.actor.username,
        actor_role=ctx.actor.role,
        reason=reason,
        policy_results=results,
        input_versions=dict(plan.input_versions),
        status="active",
        idempotency_key=key,
        sim_time=now(ctx.session),
    )
    nested = ctx.session.begin_nested()
    try:
        ctx.session.add(appr)
        ctx.session.flush()
        nested.commit()
    except IntegrityError as exc:
        nested.rollback()
        raise ConflictError(f"A concurrent {decision_type} for plan {plan.id} was recorded first") from exc
    return appr


def _audit_policies(
    ctx: Ctx, action: str, plan: Plan, results: list[dict], site_id: str, reason: str, evidence: list[str]
):
    for r in results:
        if r["applies"]:
            audit(
                ctx,
                action,
                "plan",
                str(plan.id),
                "permitted",
                f"{r['title']}: {r['detail']}",
                site_id=site_id,
                policy_id=r["policy_id"],
                policy_version=r["version"],
                evidence_refs=evidence,
            )
    audit(ctx, action, "plan", str(plan.id), "applied", reason, site_id=site_id, evidence_refs=evidence)


def _check_policies(ctx: Ctx, trigger: str, action: str, plan: Plan, facts: dict, site_id: str) -> list[dict]:
    results = policies.evaluate(trigger, facts)
    failed = policies.first_failure(results)
    if failed:
        msg = f"Blocked by {failed['policy_id']} v{failed['version']} ({failed['title']}): {failed['detail']}"
        audit_blocked(
            ctx,
            action,
            "plan",
            str(plan.id),
            msg,
            site_id=site_id,
            policy_id=failed["policy_id"],
            policy_version=failed["version"],
        )
        log(logger, logging.WARNING, "decision blocked", plan=plan.id, policy=failed["policy_id"], status="blocked")
        raise PolicyBlocked(msg, results)
    return results


def donor_release(ctx: Ctx, plan_id: int, reason: str) -> Approval:
    """Approver at the donor site releases its reserved spare for this exact plan revision."""
    plan = ctx.session.get(Plan, plan_id, with_for_update=True)
    if plan is None:
        raise DomainError(f"Plan {plan_id} not found")
    sd = ctx.session.get(Shutdown, plan.shutdown_id)
    if not plan.source_site_id or plan.source_site_id == sd.site_id or plan.scope != "full":
        raise DomainError("Donor-site release only applies to cross-site transfer plans")
    require(ctx, "donor.release", site_id=plan.source_site_id, entity="plan", entity_id=str(plan_id))
    existing = active_approval(ctx.session, plan.id, "donor_release")
    if existing is not None:
        return existing  # idempotent
    if not reason.strip():
        raise DomainError("A release reason is required")
    _validate_open(ctx, plan, "donor.release")
    facts = approval_facts(ctx.session, plan)
    results = _check_policies(ctx, "donor_release", "donor.release", plan, facts, plan.source_site_id)
    impact = (plan.donor_impact or {}).get("summary", "no donor job affected")
    text = (
        f"Donor-site release of {plan.source_stock_item_id} for {plan.plan_key} r{plan.revision}. "
        f"Impact acknowledged: {impact}. Reason: {reason.strip()}"
    )
    appr = _record(ctx, plan, "donor_release", plan.source_site_id, text, results, f"donor_release:{plan.id}")
    _audit_policies(
        ctx, "donor.release", plan, results, plan.source_site_id, text, [f"plan:{plan.plan_key}/r{plan.revision}"]
    )
    return appr


def approve_plan(ctx: Ctx, plan_id: int, reason: str) -> Approval:
    plan = ctx.session.get(Plan, plan_id, with_for_update=True)
    if plan is None:
        raise DomainError(f"Plan {plan_id} not found")
    sd = ctx.session.get(Shutdown, plan.shutdown_id, with_for_update=True)  # serializes approvals per shutdown
    require(ctx, "plan.approve", site_id=sd.site_id, entity="plan", entity_id=str(plan_id))
    existing = active_approval(ctx.session, plan.id, "plan_approval")
    if existing is not None:
        return existing  # idempotent: a repeated request never creates a second approval
    if not reason.strip():
        raise DomainError("An approval reason is required")
    _validate_open(ctx, plan, "plan.approve")
    other = approved_plan(ctx.session)
    if other is not None and other.id != plan.id:
        msg = f"Plan {other.plan_key} r{other.revision} is already approved; it must be stale or replaced first"
        audit_blocked(ctx, "plan.approve", "plan", str(plan.id), msg, site_id=sd.site_id)
        raise DomainError(msg)
    facts = approval_facts(ctx.session, plan)
    results = _check_policies(ctx, "plan_approval", "plan.approve", plan, facts, sd.site_id)
    if plan.scope == "full" and plan.source_stock_item_id:
        job = ctx.session.scalar(select(SiteJob).where(SiteJob.stock_item_id == plan.source_stock_item_id))
        inventory.reserve_for_plan(
            ctx,
            plan.source_stock_item_id,
            plan.id,
            f"{plan.plan_key}/r{plan.revision}",
            release_site_job=job.id if job else None,
        )
    text = (
        f"Approved {plan.plan_key} r{plan.revision} ({plan.title}); est. €{plan.est_cost_eur:,.0f}. "
        f"Reason: {reason.strip()}"
    )
    appr = _record(ctx, plan, "plan_approval", sd.site_id, text, results, f"plan_approval:{plan.id}")
    plan.status = "approved"
    plan.status_reason = f"Approved by {ctx.actor.username}"
    ctx.session.flush()
    _audit_policies(
        ctx,
        "plan.approve",
        plan,
        results,
        sd.site_id,
        text,
        [f"plan:{plan.plan_key}/r{plan.revision}"] + [f"{k}=v{v}" for k, v in plan.input_versions.items()],
    )
    log(logger, logging.INFO, "plan approved", plan=plan.id, key=plan.plan_key, status="approved")
    return appr


def reject_plan(ctx: Ctx, plan_id: int, reason: str) -> Approval:
    plan = ctx.session.get(Plan, plan_id, with_for_update=True)
    if plan is None:
        raise DomainError(f"Plan {plan_id} not found")
    sd = ctx.session.get(Shutdown, plan.shutdown_id)
    require(ctx, "plan.reject", site_id=sd.site_id, entity="plan", entity_id=str(plan_id))
    existing = active_approval(ctx.session, plan.id, "plan_rejection")
    if existing is not None:
        return existing
    if plan.status != "proposed":
        raise DomainError(f"Only proposed plans can be rejected (plan is {plan.status})")
    if not reason.strip():
        raise DomainError("A rejection reason is required")
    text = f"Rejected {plan.plan_key} r{plan.revision}. Reason: {reason.strip()}"
    appr = _record(ctx, plan, "plan_rejection", sd.site_id, text, [], f"plan_rejection:{plan.id}")
    plan.status = "rejected"
    plan.status_reason = text
    audit(ctx, "plan.reject", "plan", str(plan.id), "applied", text, site_id=sd.site_id)
    return appr


def mark_plan_stale(ctx: Ctx, plan: Plan, reason: str) -> None:
    was_approved = plan.status == "approved"
    plan.status = "stale"
    plan.status_reason = reason
    for appr in ctx.session.scalars(
        select(Approval).where(Approval.plan_id == plan.id, Approval.status == "active")
    ).all():
        appr.status = "stale"
        appr.status_reason = reason
        audit(
            ctx,
            "approval.stale",
            "approval",
            str(appr.id),
            "applied",
            f"{appr.decision_type} for {plan.plan_key} r{plan.revision} invalidated: {reason}",
            site_id=appr.site_id,
        )
    ctx.session.flush()
    if was_approved:
        inventory.release_plan_reservations(ctx, plan.id, f"approval of {plan.plan_key} r{plan.revision} stale")
        log(logger, logging.WARNING, "approval stale", plan=plan.id, reason=reason, status="stale")


def invalidate_for_input(ctx: Ctx, key: str, reason: str) -> list[int]:
    """A material input change makes affected open plans and their approvals stale."""
    affected = []
    plans = ctx.session.scalars(select(Plan).where(Plan.status.in_(["proposed", "approved"]))).all()
    for plan in plans:
        if key in plan.input_versions:
            mark_plan_stale(ctx, plan, f"Input {key} changed: {reason}")
            affected.append(plan.id)
    return affected
