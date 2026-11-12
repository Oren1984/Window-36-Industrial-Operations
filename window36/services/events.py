"""Idempotent, order-aware handling of simulated external events (carrier and receiving systems).

* A repeated event_id is a duplicate: counted, never re-applied.
* Events carry a per-entity sequence; an event older than the last applied sequence is stored as
  `out_of_order` and not applied, so it cannot silently overwrite newer state.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from window36.logging_setup import log
from window36.models import EntitySequence, ExternalEvent, RouteLeg
from window36.services import inventory
from window36.services.common import Ctx, DomainError, audit, bump_input, now, require

logger = logging.getLogger("window36.events")

KINDS = {"carrier.status", "carrier.pickup_delay", "receiving.inspection"}


def ingest_event(
    ctx: Ctx, event_id: str, source: str, entity_key: str, sequence: int, kind: str, payload: dict
) -> tuple[str, ExternalEvent]:
    require(ctx, "event.ingest", entity="event", entity_id=event_id)
    if kind not in KINDS:
        raise DomainError(f"Unsupported event kind {kind}")
    t = now(ctx.session)
    # Claim the event_id first; ON CONFLICT DO NOTHING makes concurrent duplicates safe.
    inserted = ctx.session.execute(
        pg_insert(ExternalEvent)
        .values(
            event_id=event_id,
            source=source,
            entity_key=entity_key,
            sequence=sequence,
            kind=kind,
            payload=payload,
            outcome="pending",
            duplicate_count=0,
            received_sim_time=t,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(ExternalEvent.id)
    ).scalar()
    if inserted is None:
        ev = ctx.session.scalar(select(ExternalEvent).where(ExternalEvent.event_id == event_id).with_for_update())
        ev.duplicate_count += 1
        audit(
            ctx,
            "event.duplicate",
            "event",
            event_id,
            "info",
            f"Duplicate delivery of {event_id} ignored (seen {ev.duplicate_count + 1}×)",
        )
        log(logger, logging.INFO, "duplicate event ignored", event_id=event_id, status="duplicate")
        return "duplicate", ev
    ev = ctx.session.get(ExternalEvent, inserted)
    ctx.session.execute(
        pg_insert(EntitySequence)
        .values(entity_key=entity_key, last_sequence=0)
        .on_conflict_do_nothing(index_elements=["entity_key"])
    )
    seq = ctx.session.get(EntitySequence, entity_key, with_for_update=True, populate_existing=True)
    if sequence <= seq.last_sequence:
        ev.outcome = "out_of_order"
        ev.note = f"Sequence {sequence} ≤ last applied {seq.last_sequence}; not applied"
        audit(ctx, "event.out_of_order", "event", event_id, "blocked", ev.note)
        log(logger, logging.WARNING, "out-of-order event not applied", event_id=event_id, status="out_of_order")
        return "out_of_order", ev
    try:
        ev.note = _apply(ctx, entity_key, kind, payload)
    except DomainError as exc:
        ev.outcome, ev.note = "rejected", str(exc)
        audit(ctx, "event.rejected", "event", event_id, "blocked", str(exc))
        return "rejected", ev
    seq.last_sequence = sequence
    ev.outcome = "applied"
    audit(
        ctx,
        "event.applied",
        "event",
        event_id,
        "applied",
        f"{kind} on {entity_key}: {ev.note}",
        evidence_refs=[f"{source}:{event_id}"],
    )
    log(logger, logging.INFO, "event applied", event_id=event_id, kind=kind, status="applied")
    return "applied", ev


def _apply(ctx: Ctx, entity_key: str, kind: str, payload: dict) -> str:
    etype, _, eid = entity_key.partition(":")
    if kind == "carrier.status":
        return str(payload.get("status", "status update"))
    if kind == "carrier.pickup_delay":
        if etype != "route":
            raise DomainError("pickup_delay must target a route")
        leg = ctx.session.scalar(
            select(RouteLeg)
            .where(RouteLeg.route_id == eid, RouteLeg.seq == int(payload.get("leg", 1)))
            .with_for_update()
        )
        if leg is None:
            raise DomainError(f"Route {eid} leg {payload.get('leg')} not found")
        delay = float(payload["delay_h"])
        if not 0 <= delay <= 240:
            raise DomainError("delay_h out of range")
        leg.delay_h = delay  # absolute value: replay-safe
        reason = f"Leg {leg.seq} ({leg.mode} {leg.origin}) delayed {delay:g} h: {payload.get('reason', '')}".strip()
        bump_input(ctx, entity_key, reason)
        return reason
    if kind == "receiving.inspection":
        if etype != "stock":
            raise DomainError("receiving.inspection must target a stock item")
        item = inventory.record_receiving_inspection_internal(
            ctx, eid, str(payload["observed_revision"]), str(payload["evidence_ref"])
        )
        return f"{item.id} observed rev {item.revision} → {item.status}"
    raise DomainError(f"Unsupported event kind {kind}")
