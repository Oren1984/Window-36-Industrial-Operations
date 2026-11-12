"""Shutdown planning: deterministic candidate generation and immutable plan revisions."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from window36.domain.routes import LegSpec, RouteResult, evaluate_route
from window36.domain.scheduling import CrewSpec, ScheduleResult, TaskSpec, schedule
from window36.domain.timeutil import hours, local_str
from window36.logging_setup import log
from window36.models import (
    InputChange,
    Plan,
    PlanningRun,
    Reservation,
    Route,
    RouteLeg,
    Shutdown,
    Site,
    SiteJob,
    StaffCalendar,
    StockItem,
    Task,
)
from window36.services import inventory
from window36.services.common import Ctx, DomainError, audit, current_versions, now, require

logger = logging.getLogger("window36.planning")

RECEIVING_INSPECTION_H = 1.0  # goods-in inspection before a delivered part can be used
DISPATCH_LEAD_H = 1.0  # earliest dispatch after authorization


@dataclass
class Candidate:
    plan_key: str
    title: str
    scope: str
    input_keys: list[str]
    source_stock_item_id: str | None = None
    source_site_id: str | None = None
    route_id: str | None = None
    part_revision: str | None = None
    feasible: bool = True
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    expected_arrival: datetime | None = None
    part_available_at: datetime | None = None
    expected_finish: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    slack_h: float | None = None
    cost_breakdown: dict = field(default_factory=dict)
    donor_impact: dict | None = None
    route_detail: dict | None = None
    schedule: list = field(default_factory=list)
    critical_path: list = field(default_factory=list)

    @property
    def est_cost(self) -> float:
        return round(sum(self.cost_breakdown.values()), 2)


def main_shutdown(session) -> Shutdown:
    sd = session.scalar(select(Shutdown).order_by(Shutdown.id))
    if sd is None:
        raise DomainError("No shutdown seeded")
    return sd


def _task_specs(tasks: list[Task], scope: str) -> list[TaskSpec]:
    chosen = [t for t in tasks if t.in_reduced_scope] if scope == "reduced" else list(tasks)
    codes = {t.code for t in chosen}
    return [
        TaskSpec(t.code, t.name, t.duration_h, t.skill, t.requires_part, tuple(d for d in t.depends_on if d in codes))
        for t in chosen
    ]


def _apply_schedule(c: Candidate, res: ScheduleResult, window_start: datetime, window_end: datetime, tz: str):
    c.window_start, c.window_end = window_start, window_end
    if res.errors:
        c.feasible = False
        c.reasons.extend(res.errors)
        return
    c.schedule = [
        {
            "code": s.code,
            "name": s.name,
            "skill": s.skill,
            "start": s.start.isoformat(),
            "finish": s.finish.isoformat(),
            "crew": s.crew,
            "binding": s.binding,
            "requires_part": s.requires_part,
        }
        for s in res.tasks
    ]
    c.critical_path = res.critical_path
    c.expected_finish = res.finish
    c.slack_h = round((window_end - res.finish).total_seconds() / 3600, 2)
    if res.finish > window_end:
        c.feasible = False
        c.reasons.append(
            f"Expected finish {local_str(res.finish, tz)} is {-c.slack_h:.1f} h after the window ends "
            f"({local_str(window_end, tz)})"
        )


def _route_eval(session, route: Route, t_now: datetime, dest: Site) -> tuple[RouteResult, dict]:
    legs = session.scalars(select(RouteLeg).where(RouteLeg.route_id == route.id).order_by(RouteLeg.seq)).all()
    specs = [
        LegSpec(
            leg.seq,
            leg.mode,
            leg.origin,
            leg.destination,
            leg.duration_h,
            leg.cost_eur,
            leg.handoff_buffer_h,
            leg.scheduled_departure,
            leg.cutoff_at,
            leg.delay_h,
        )
        for leg in legs
    ]
    tz = {leg.seq: (leg.origin_tz, leg.destination_tz) for leg in legs}
    return evaluate_route(
        specs, t_now + hours(DISPATCH_LEAD_H), dest.timezone, dest.receiving_open, dest.receiving_close
    ), tz


def _route_detail(rr: RouteResult, route: Route, tz_origin: str, tz_dest: str, leg_tz: dict) -> dict:
    return {
        "route_id": route.id,
        "name": route.name,
        "mode": route.mode_label,
        "carrier": route.carrier,
        "feasible": rr.feasible,
        "arrival": rr.arrival.isoformat() if rr.arrival else None,
        "delivered_at": rr.delivered_at.isoformat() if rr.delivered_at else None,
        "legs": [
            {
                "seq": lr.seq,
                "mode": lr.mode,
                "from": lr.origin,
                "to": lr.destination,
                "ready": lr.ready_at.isoformat(),
                "departs": lr.departs_at.isoformat() if lr.departs_at else None,
                "arrives": lr.arrives_at.isoformat() if lr.arrives_at else None,
                "cutoff": lr.cutoff_at.isoformat() if lr.cutoff_at else None,
                "delay_h": lr.delay_h,
                "status": lr.status,
                "reason": lr.reason,
                "from_tz": leg_tz[lr.seq][0],
                "to_tz": leg_tz[lr.seq][1],
            }
            for lr in rr.legs
        ],
        "origin_tz": tz_origin,
        "dest_tz": tz_dest,
    }


def compute_candidates(session, t_now: datetime) -> list[Candidate]:
    sd = main_shutdown(session)
    main = session.get(Site, sd.site_id)
    tasks = session.scalars(select(Task).where(Task.shutdown_id == sd.id).order_by(Task.code)).all()
    crews = [
        CrewSpec(c.skill, c.headcount, c.daily_start, c.daily_end)
        for c in session.scalars(select(StaffCalendar).where(StaffCalendar.site_id == main.id)).all()
    ]
    base_keys = [f"shutdown:{sd.id}", f"tasks:{sd.id}", f"staff:{main.id}"]
    full_specs, reduced_specs = _task_specs(tasks, "full"), _task_specs(tasks, "reduced")
    out: list[Candidate] = []

    def sched(specs, start, part_at):
        return schedule(specs, crews, main.timezone, start, part_at)

    # 1. The originally planned component (inbound supplier delivery to the main plant).
    for item in session.scalars(
        select(StockItem)
        .where(StockItem.site_id == main.id, StockItem.part_number == sd.part_number)
        .order_by(StockItem.id)
    ).all():
        c = Candidate(
            "FULL-ORIGINAL",
            f"Full scope with original delivery {item.delivery_ref or item.id}",
            "full",
            base_keys + [f"stock:{item.id}"],
            item.id,
            main.id,
            None,
            item.revision,
        )
        c.cost_breakdown = {"Transport": 0.0}
        part_at = None
        if item.status == "quarantine":
            c.feasible = False
            c.reasons.append(f"{item.id} quarantined: revision discrepancy unresolved (needs receiving + QA evidence)")
        elif item.status == "rejected":
            c.feasible = False
            c.reasons.append(f"{item.id} rejected at receiving: rev {item.revision} not compatible with {sd.line_code}")
        elif item.status in ("consumed",):
            c.feasible = False
            c.reasons.append(f"{item.id} already consumed")
        else:
            ok, note = inventory.compatibility(session, item.part_number, sd.line_code, item.revision)
            if not ok:
                c.feasible = False
                c.reasons.append(f"Rev {item.revision} incompatible: {note}")
            arrival = item.expected_arrival or t_now
            c.expected_arrival = arrival
            part_at = (
                max(arrival, t_now)
                if item.revision_status == "verified"
                else max(arrival, t_now) + hours(RECEIVING_INSPECTION_H)
            )
            c.part_available_at = part_at
            if item.revision_status != "verified":
                c.notes.append(
                    f"Revision {item.revision} is '{item.revision_status}': receiving inspection "
                    "evidence required before approval (POL-REV-001)"
                )
            holder = inventory.active_reservation(session, item.id)
            if holder is not None and holder.holder_type != "plan":
                c.feasible = False
                c.reasons.append(f"Claimed by {holder.holder_type} {holder.holder_ref}")
        if c.feasible:
            _apply_schedule(
                c, sched(full_specs, sd.window_start, part_at), sd.window_start, sd.window_end, main.timezone
            )
        else:
            c.window_start, c.window_end = sd.window_start, sd.window_end
        out.append(c)

    # 2. Cross-site transfers of compatible spares, one candidate per route.
    routes = session.scalars(select(Route).where(Route.destination_site_id == main.id).order_by(Route.id)).all()
    for route in routes:
        origin = session.get(Site, route.origin_site_id)
        item = session.scalar(
            select(StockItem)
            .where(StockItem.site_id == origin.id, StockItem.part_number == sd.part_number)
            .order_by(StockItem.id)
        )
        if item is None:
            continue
        job = session.scalar(select(SiteJob).where(SiteJob.stock_item_id == item.id))
        keys = base_keys + [f"stock:{item.id}", f"route:{route.id}"] + ([f"sitejob:{job.id}"] if job else [])
        c = Candidate(
            f"FULL-{route.id}",
            f"Full scope, rev {item.revision} spare from " f"{origin.name} via {route.mode_label}",
            "full",
            keys,
            item.id,
            origin.id,
            route.id,
            item.revision,
        )
        rr, leg_tz = _route_eval(session, route, t_now, main)
        c.route_detail = _route_detail(rr, route, origin.timezone, main.timezone, leg_tz)
        c.cost_breakdown = {"Transport": rr.cost_eur}
        ok, note = inventory.compatibility(session, item.part_number, sd.line_code, item.revision)
        if not ok:
            c.feasible = False
            c.reasons.append(f"Rev {item.revision} incompatible: {note}")
        if item.status != "available":
            c.feasible = False
            c.reasons.append(f"Spare {item.id} is {item.status}")
        holder = inventory.active_reservation(session, item.id)
        if holder is not None and holder.holder_type == "plan":
            other = session.get(Plan, holder.plan_id)
            if other is None or other.plan_key != c.plan_key:
                c.feasible = False
                c.reasons.append(f"Spare {item.id} already claimed by {holder.holder_ref}")
        if job is not None:
            eta = t_now + timedelta(days=job.replacement_lead_days)
            delay_days = max(0.0, round((eta - job.planned_start).total_seconds() / 86400, 1))
            c.donor_impact = {
                "site_id": origin.id,
                "site": origin.name,
                "job_id": job.id,
                "job": job.title,
                "planned_start": job.planned_start.isoformat(),
                "replacement_eta": eta.isoformat(),
                "delay_days": delay_days,
                "cost_eur": job.impact_cost_eur,
                "summary": f"{job.id} ({job.title}) loses its spare; replacement in {job.replacement_lead_days} d "
                f"→ job delayed ≈{delay_days:.0f} d; est. impact €{job.impact_cost_eur:,.0f}",
            }
            c.cost_breakdown["Donor-site impact"] = job.impact_cost_eur
            c.notes.append("Requires donor-site release by an approver at " + origin.name + " (POL-DONOR-002)")
        if not rr.feasible:
            c.feasible = False
            c.reasons.extend(rr.reasons)
        else:
            c.expected_arrival = rr.arrival
            c.part_available_at = rr.delivered_at + hours(RECEIVING_INSPECTION_H)
            if rr.delivered_at > rr.arrival:
                c.notes.append(
                    f"Arrives outside receiving hours; held until {local_str(rr.delivered_at, main.timezone)}"
                )
            if c.part_available_at > sd.window_end:
                c.feasible = False
                c.reasons.append(
                    f"Part available {local_str(c.part_available_at, main.timezone)}, after the "
                    f"shutdown window ends ({local_str(sd.window_end, main.timezone)})"
                )
        if c.feasible:
            _apply_schedule(
                c,
                sched(full_specs, sd.window_start, c.part_available_at),
                sd.window_start,
                sd.window_end,
                main.timezone,
            )
        else:
            c.window_start, c.window_end = sd.window_start, sd.window_end
        out.append(c)

    # 3. Reduced scope: independent tasks only; coupling replacement deferred.
    c = Candidate(
        "REDUCED-SCOPE",
        "Reduced scope: independent tasks now, coupling replacement deferred",
        "reduced",
        list(base_keys),
    )
    c.cost_breakdown = {"Deferred-replacement risk (est.)": sd.reduced_scope_risk_cost_eur}
    deferred = [t.code for t in tasks if not t.in_reduced_scope]
    c.notes.append("Deferred to a later window: " + ", ".join(deferred))
    _apply_schedule(c, sched(reduced_specs, sd.window_start, None), sd.window_start, sd.window_end, main.timezone)
    out.append(c)

    # 4. Postponement to the next window with standard sea supply of a compatible spare.
    c = Candidate("POSTPONE", "Postpone the shutdown to the next window", "postpone", list(base_keys))
    c.cost_breakdown = {"Postponement (est.)": sd.postponement_cost_eur}
    if sd.next_window_start is None:
        c.feasible = False
        c.reasons.append("No later shutdown window is available")
        c.window_start, c.window_end = sd.window_start, sd.window_end
    else:
        sea = session.scalar(select(Route).where(Route.destination_site_id == main.id, Route.mode_label.like("%sea%")))
        sea_item = sea and session.scalar(
            select(StockItem).where(StockItem.site_id == sea.origin_site_id, StockItem.part_number == sd.part_number)
        )
        if sea is None or sea_item is None:
            c.feasible = False
            c.reasons.append("No standard supply route for the next window")
        else:
            c.input_keys.append(f"stock:{sea_item.id}")
            c.source_stock_item_id, c.source_site_id, c.part_revision = (
                sea_item.id,
                sea.origin_site_id,
                sea_item.revision,
            )
            sea_cost = sum(leg.cost_eur for leg in session.scalars(select(RouteLeg).where(RouteLeg.route_id == sea.id)))
            c.cost_breakdown["Standard sea supply"] = sea_cost
            c.notes.append(
                f"Spare {sea_item.id} shipped on the next scheduled sailing; arrives before "
                f"{local_str(sd.next_window_start, main.timezone)}"
            )
        if c.feasible:
            dur = sd.window_end - sd.window_start
            _apply_schedule(
                c,
                sched(full_specs, sd.next_window_start, sd.next_window_start),
                sd.next_window_start,
                sd.next_window_start + dur,
                main.timezone,
            )
            c.notes.append("Production continues on the worn coupling until the next window")
    out.append(c)
    return out


def _fingerprint(c: Candidate, versions: dict) -> str:
    body = {k: v for k, v in c.__dict__.items() if k != "input_keys"}
    body["input_versions"] = versions
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def latest_revision(session, plan_key: str) -> Plan | None:
    return session.scalar(select(Plan).where(Plan.plan_key == plan_key).order_by(Plan.revision.desc()).limit(1))


def current_plans(session) -> list[Plan]:
    sub = select(Plan.plan_key, func.max(Plan.revision).label("rev")).group_by(Plan.plan_key).subquery()
    rows = session.scalars(
        select(Plan).join(sub, (Plan.plan_key == sub.c.plan_key) & (Plan.revision == sub.c.rev))
    ).all()
    order = {"FULL-ORIGINAL": 0, "REDUCED-SCOPE": 8, "POSTPONE": 9}
    return sorted(rows, key=lambda p: (order.get(p.plan_key, 1), p.plan_key))


def last_run(session) -> PlanningRun | None:
    return session.scalar(select(PlanningRun).order_by(PlanningRun.id.desc()).limit(1))


def replan(ctx: Ctx, trigger: str) -> list[Plan]:
    """Recompute all candidates; persist a new immutable revision only where something changed."""
    sd = main_shutdown(ctx.session)
    require(ctx, "plan.compute", site_id=sd.site_id, entity="shutdown", entity_id=sd.id)
    from window36.services import approvals

    t_now = now(ctx.session)
    watermark = ctx.session.scalar(select(func.coalesce(func.max(InputChange.id), 0)))
    run = PlanningRun(
        shutdown_id=sd.id,
        sim_time=t_now,
        actor=ctx.actor.username,
        trigger=trigger[:200],
        last_input_change_id=watermark,
    )
    ctx.session.add(run)
    ctx.session.flush()
    created = []
    for c in compute_candidates(ctx.session, t_now):
        versions = current_versions(ctx.session, c.input_keys)
        fp = _fingerprint(c, versions)
        prev = latest_revision(ctx.session, c.plan_key)
        if prev is not None and prev.fingerprint == fp and prev.status not in ("stale", "superseded"):
            continue
        plan = Plan(
            shutdown_id=sd.id,
            planning_run_id=run.id,
            plan_key=c.plan_key,
            revision=(prev.revision + 1) if prev else 1,
            title=c.title,
            scope=c.scope,
            source_stock_item_id=c.source_stock_item_id,
            source_site_id=c.source_site_id,
            route_id=c.route_id,
            part_revision=c.part_revision,
            feasible=c.feasible,
            reasons=c.reasons,
            notes=c.notes,
            expected_arrival=c.expected_arrival,
            part_available_at=c.part_available_at,
            expected_finish=c.expected_finish,
            window_start=c.window_start,
            window_end=c.window_end,
            slack_h=c.slack_h,
            est_cost_eur=c.est_cost,
            cost_breakdown=c.cost_breakdown,
            donor_impact=c.donor_impact,
            route_detail=c.route_detail,
            schedule=c.schedule,
            critical_path=c.critical_path,
            input_versions=versions,
            fingerprint=fp,
            created_at=t_now,
            status="proposed",
        )
        ctx.session.add(plan)
        ctx.session.flush()
        if prev is not None:
            if prev.status == "approved":
                approvals.mark_plan_stale(ctx, prev, f"Recomputed as revision {plan.revision}: outcome changed")
            elif prev.status in ("proposed", "rejected") or (
                prev.status == "stale" and approvals.active_or_stale_approval(ctx.session, prev.id) is None
            ):
                prev.status = "superseded"
                prev.status_reason = f"Superseded by revision {plan.revision}"
        created.append(plan)
    ctx.session.flush()
    audit(
        ctx,
        "plan.compute",
        "planning_run",
        str(run.id),
        "applied",
        f"Replanned ({trigger}); {len(created)} new plan revision(s)",
        site_id=sd.site_id,
    )
    log(logger, logging.INFO, "replanned", run=run.id, new_revisions=len(created), status="ok")
    return current_plans(ctx.session)


def reservations_view(session) -> list[Reservation]:
    return session.scalars(select(Reservation).order_by(Reservation.id)).all()


def changes_since_last_plan(session) -> list[InputChange]:
    run = last_run(session)
    mark = run.last_input_change_id if run else 0
    return session.scalars(select(InputChange).where(InputChange.id > mark).order_by(InputChange.id)).all()
