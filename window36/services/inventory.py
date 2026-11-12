"""Inventory, compatibility and atomic reservations."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from window36.domain.compatibility import Rule, check
from window36.logging_setup import log
from window36.models import CompatibilityRule, FactConflict, Reservation, SiteJob, StockItem
from window36.services.common import ConflictError, Ctx, DomainError, audit, bump_input, now, require

logger = logging.getLogger("window36.inventory")


def rules(session) -> list[Rule]:
    return [
        Rule(r.part_number, r.line_code, r.revision, r.compatible, r.note)
        for r in session.scalars(select(CompatibilityRule)).all()
    ]


def compatibility(session, part_number: str, line_code: str, revision: str) -> tuple[bool, str]:
    return check(rules(session), part_number, line_code, revision)


def active_reservation(session, stock_item_id: str, lock: bool = False) -> Reservation | None:
    q = select(Reservation).where(Reservation.stock_item_id == stock_item_id, Reservation.status == "active")
    if lock:
        q = q.with_for_update()
    return session.scalar(q)


def reserve_for_plan(
    ctx: Ctx, stock_item_id: str, plan_id: int, plan_ref: str, release_site_job: str | None = None
) -> Reservation:
    """Atomically claim a spare for a plan.

    The stock row is locked (SELECT ... FOR UPDATE) so concurrent claims serialize; the partial
    unique index `uq_reservation_one_active_claim` is the database backstop. If the spare is held
    by the named donor site job, that claim is released in the same transaction.
    """
    item = ctx.session.get(StockItem, stock_item_id, with_for_update=True)
    if item is None:
        raise DomainError(f"Stock item {stock_item_id} not found")
    if item.status != "available":
        raise ConflictError(f"Stock item {stock_item_id} is {item.status}, not available")
    current = active_reservation(ctx.session, stock_item_id, lock=True)
    t = now(ctx.session)
    if current is not None:
        if current.holder_type == "plan" and current.plan_id == plan_id:
            return current  # idempotent
        if current.holder_type == "site_job" and current.holder_ref == release_site_job:
            current.status = "released"
            current.released_at = t
            current.reason = f"Released to {plan_ref} by donor-site release"
            ctx.session.flush()
        else:
            raise ConflictError(f"Spare {stock_item_id} already claimed by {current.holder_type} {current.holder_ref}")
    res = Reservation(
        stock_item_id=stock_item_id,
        holder_type="plan",
        holder_ref=plan_ref,
        plan_id=plan_id,
        status="active",
        reason="Authorized simulated transfer",
        created_by=ctx.actor.username,
        created_at=t,
    )
    ctx.session.add(res)
    try:
        ctx.session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"Spare {stock_item_id} was claimed concurrently") from exc
    log(logger, logging.INFO, "spare reserved", stock_item=stock_item_id, plan=plan_ref, status="reserved")
    return res


def release_plan_reservations(ctx: Ctx, plan_id: int, reason: str) -> list[str]:
    """Release plan claims; a donor spare goes back to its site job."""
    released = []
    t = now(ctx.session)
    rows = ctx.session.scalars(
        select(Reservation).where(Reservation.plan_id == plan_id, Reservation.status == "active").with_for_update()
    ).all()
    for r in rows:
        r.status = "released"
        r.released_at = t
        r.reason = reason
        ctx.session.flush()
        job = ctx.session.scalar(select(SiteJob).where(SiteJob.stock_item_id == r.stock_item_id))
        if job is not None:
            ctx.session.add(
                Reservation(
                    stock_item_id=r.stock_item_id,
                    holder_type="site_job",
                    holder_ref=job.id,
                    status="active",
                    reason=f"Claim restored: {reason}",
                    created_by=ctx.actor.username,
                    created_at=t,
                )
            )
            ctx.session.flush()
        released.append(r.stock_item_id)
        audit(
            ctx,
            "reservation.release",
            "stock_item",
            r.stock_item_id,
            "applied",
            f"Reservation for plan {plan_id} released: {reason}" + (f"; claim restored to {job.id}" if job else ""),
        )
    return released


def record_receiving_inspection(
    ctx: Ctx, stock_item_id: str, observed_revision: str, evidence_ref: str, line_code: str = "L1"
) -> StockItem:
    """Site operator records the physical revision found at goods-in."""
    item = ctx.session.get(StockItem, stock_item_id)
    if item is None:
        raise DomainError(f"Stock item {stock_item_id} not found")
    require(ctx, "receiving.record", site_id=item.site_id, entity="stock_item", entity_id=stock_item_id)
    return record_receiving_inspection_internal(ctx, stock_item_id, observed_revision, evidence_ref, line_code)


def record_receiving_inspection_internal(
    ctx: Ctx, stock_item_id: str, observed_revision: str, evidence_ref: str, line_code: str = "L1"
) -> StockItem:
    """Shared by the operator action and the simulated receiving-system event (already authorized)."""
    item = ctx.session.get(StockItem, stock_item_id, with_for_update=True)
    if item is None:
        raise DomainError(f"Stock item {stock_item_id} not found")
    observed_revision = observed_revision.strip().upper()
    if len(observed_revision) != 1 or not observed_revision.isalpha():
        raise DomainError("Observed revision must be a single letter")
    if not evidence_ref.strip():
        raise DomainError("An evidence reference is required")
    if item.status not in ("inbound", "quarantine"):
        raise DomainError(f"{stock_item_id} is {item.status}; receiving inspection not applicable")
    ok, note = compatibility(ctx.session, item.part_number, line_code, observed_revision)
    item.revision = observed_revision
    if ok:
        item.status, item.revision_status = "available", "verified"
        outcome = f"Receiving inspection verified rev {observed_revision} ({note}); part released for planning"
    else:
        item.status, item.revision_status = "rejected", "rejected"
        outcome = f"Receiving inspection found rev {observed_revision}: {note}; part rejected"
    for c in ctx.session.scalars(
        select(FactConflict).where(FactConflict.delivery_ref == item.delivery_ref, FactConflict.status == "open")
    ).all():
        c.status = "resolved"
        c.resolution = f"Physical inspection ({evidence_ref}) observed rev {observed_revision}"
    ctx.session.flush()
    audit(
        ctx,
        "receiving.inspection",
        "stock_item",
        item.id,
        "applied",
        outcome,
        site_id=item.site_id,
        policy_id="POL-REV-001",
        policy_version="1.2",
        evidence_refs=[evidence_ref],
    )
    bump_input(ctx, f"stock:{item.id}", outcome)
    return item


def transfer_manifest(item: StockItem, origin: str, destination: str, carrier: str) -> dict:
    return {
        "part_number": item.part_number,
        "revision": item.revision,
        "serial": item.serial,
        "origin": origin,
        "destination": destination,
        "carrier": carrier,
    }
