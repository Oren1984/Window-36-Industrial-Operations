"""Seeded, resettable demo scenario with a fixed simulation clock and reproducible disturbances.

All names, stock, cutoffs, travel times, costs and procedures are fictional.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select

from window36.logging_setup import log
from window36.models import (
    Approval,
    AppUser,
    CompatibilityRule,
    Document,
    EntitySequence,
    ExternalEvent,
    ExtractedFact,
    FactConflict,
    InputChange,
    InputVersion,
    Plan,
    PlanningRun,
    QAEvidence,
    Reservation,
    Route,
    RouteLeg,
    ScenarioState,
    Shutdown,
    Site,
    SiteJob,
    StaffCalendar,
    StockItem,
    Task,
    WorkRecord,
)
from window36.services import documents, events, planning
from window36.services.auth import hash_password
from window36.services.common import Ctx, DomainError, audit, load_actor, now, require

logger = logging.getLogger("window36.scenario")

SCENARIO_NAME = "Valmont Line 1 — 36 h shutdown, coupling revision discrepancy"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
OPENING_DOCUMENTS = ("supplier_notice_NVC-SN-2026-0412.txt", "shipping_document_NVC-DN-2026-0877.txt")


def T(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 2, day, hh, mm, tzinfo=UTC)


DEMO_START = T(20, 2)  # four hours before the shutdown window opens

SITES = [
    Site(
        id="FR-VAL",
        name="Valmont Plant (fictional)",
        country="France",
        timezone="Europe/Paris",
        site_role="main",
        receiving_open="06:00",
        receiving_close="22:00",
    ),
    Site(
        id="DE-HSF",
        name="Hessenfeld Plant (fictional)",
        country="Germany",
        timezone="Europe/Berlin",
        site_role="secondary",
        receiving_open="06:00",
        receiving_close="20:00",
    ),
    Site(
        id="IE-SHM",
        name="Shannamore Plant (fictional)",
        country="Ireland",
        timezone="Europe/Dublin",
        site_role="secondary",
        receiving_open="07:00",
        receiving_close="19:00",
    ),
]

USERS = [
    ("planner_fr", "Planner — Valmont", "planner", "FR-VAL"),
    ("operator_fr", "Site operator — Valmont", "site_operator", "FR-VAL"),
    ("approver_fr", "Plant manager / approver — Valmont", "approver", "FR-VAL"),
    ("operator_de", "Site operator — Hessenfeld", "site_operator", "DE-HSF"),
    ("approver_de", "Maintenance manager / approver — Hessenfeld", "approver", "DE-HSF"),
    ("operator_ie", "Site operator — Shannamore", "site_operator", "IE-SHM"),
    ("approver_ie", "Maintenance manager / approver — Shannamore", "approver", "IE-SHM"),
    ("viewer", "Read-only demo viewer", "viewer", None),
]

DISTURBANCES = {
    "wrong_revision": {
        "label": "Wrong part revision at goods-in",
        "description": "Valmont receiving inspection finds the delivered coupling is rev B (incompatible).",
        "event": dict(
            event_id="EVT-RCV-SP-FR-0001-01",
            source="valmont-goods-in (simulated)",
            entity_key="stock:SP-FR-0001",
            sequence=1,
            kind="receiving.inspection",
            payload={"observed_revision": "B", "evidence_ref": "RCV-VAL-0220-01"},
        ),
    },
    "road_pickup_delay": {
        "label": "Road pickup delay (Hessenfeld → Valmont)",
        "description": "Carrier reports a 16 h pickup delay: tractor unit breakdown at the depot.",
        "event": dict(
            event_id="EVT-DE-FR-ROAD-0002",
            source="rheinfracht-tms (simulated)",
            entity_key="route:DE-FR-ROAD",
            sequence=2,
            kind="carrier.pickup_delay",
            payload={"leg": 1, "delay_h": 16, "reason": "Tractor unit breakdown at carrier depot"},
        ),
    },
    "missed_air_cutoff": {
        "label": "Missed air cargo cutoff (Shannamore → Shannon)",
        "description": "Feeder truck held 2.5 h at depot; it reaches Shannon after the 10:00 UTC cargo cutoff.",
        "event": dict(
            event_id="EVT-IE-FR-AIR-0002",
            source="skybridge-cargo (simulated)",
            entity_key="route:IE-FR-AIR",
            sequence=2,
            kind="carrier.pickup_delay",
            payload={"leg": 1, "delay_h": 2.5, "reason": "Feeder truck held at depot"},
        ),
    },
}


def seed_reference(session) -> None:
    for s in SITES:
        if session.get(Site, s.id) is None:
            session.add(Site(**{c.name: getattr(s, c.name) for c in Site.__table__.columns}))
    session.flush()


def seed_users(session, password: str | None = None) -> int:
    password = password or os.environ.get("W36_DEMO_PASSWORD")
    if not password:
        raise RuntimeError("W36_DEMO_PASSWORD is not set (see .env.example)")
    created = 0
    for username, display, role, site in USERS:
        if session.scalar(select(AppUser).where(AppUser.username == username)) is None:
            session.add(
                AppUser(
                    username=username,
                    display_name=display,
                    role=role,
                    site_id=site,
                    password_hash=hash_password(password),
                )
            )
            created += 1
    session.flush()
    return created


def _seed_static(session) -> None:
    session.add(
        ScenarioState(
            id=1, scenario_name=SCENARIO_NAME, now_utc=T(20, 1), scenario_version=1, seeded_at=datetime.now(UTC)
        )
    )
    pn = "W36-CPL-4471"
    session.add_all(
        [
            CompatibilityRule(
                part_number=pn,
                line_code="L1",
                revision="B",
                compatible=False,
                note="Rev B bore is 48 mm; Line 1 output shaft needs 52 mm",
            ),
            CompatibilityRule(
                part_number=pn, line_code="L1", revision="C", compatible=True, note="Original specification for Line 1"
            ),
            CompatibilityRule(
                part_number=pn,
                line_code="L1",
                revision="D",
                compatible=True,
                note="Rev D supersedes C; identical interface, reinforced hub",
            ),
        ]
    )
    desc = "Gearbox output coupling assembly (fictional)"
    session.add_all(
        [
            StockItem(
                id="SP-FR-0001",
                part_number=pn,
                description=desc,
                revision="C",
                revision_status="expected",
                serial="NVC-4471-00918",
                site_id="FR-VAL",
                status="inbound",
                delivery_ref="DEL-FR-0220-01",
                expected_arrival=T(20, 1, 30),
                unit_value_eur=6400,
            ),
            StockItem(
                id="SP-DE-0001",
                part_number=pn,
                description=desc,
                revision="D",
                revision_status="verified",
                serial="HSF-4471-D-0031",
                site_id="DE-HSF",
                status="available",
                unit_value_eur=6900,
            ),
            StockItem(
                id="SP-IE-0001",
                part_number=pn,
                description=desc,
                revision="C",
                revision_status="verified",
                serial="SHM-4471-C-0107",
                site_id="IE-SHM",
                status="available",
                unit_value_eur=6400,
            ),
        ]
    )
    session.flush()
    session.add(
        SiteJob(
            id="GER-MJ-0226",
            site_id="DE-HSF",
            title="Press Line 2 gearbox coupling exchange",
            planned_start=T(26, 5),
            stock_item_id="SP-DE-0001",
            replacement_lead_days=10,
            impact_cost_eur=4200,
        )
    )
    session.flush()
    session.add(
        Reservation(
            stock_item_id="SP-DE-0001",
            holder_type="site_job",
            holder_ref="GER-MJ-0226",
            status="active",
            reason="Reserved for planned job GER-MJ-0226",
            created_by="seed",
            created_at=T(10, 9),
        )
    )
    session.add(
        Shutdown(
            id="SD-FR-L1-0220",
            site_id="FR-VAL",
            line_code="L1",
            line_name="Extrusion Line 1",
            part_number=pn,
            window_start=T(20, 6),
            window_end=T(21, 18),
            next_window_start=T(27, 6),
            postponement_cost_eur=18000,
            reduced_scope_risk_cost_eur=6500,
        )
    )
    session.flush()
    tasks = [
        ("T1", "Lock-out / tag-out and isolation", 2, "mech", False, True, []),
        ("T2", "Drain and clean gearbox housing", 4, "mech", False, False, ["T1"]),
        ("T3", "Inspect conveyor, replace worn rollers", 6, "mech", False, True, ["T1"]),
        ("T4", "Calibrate line sensors", 4, "elec", False, True, ["T1"]),
        ("T5", "Remove worn coupling", 3, "mech", True, False, ["T2"]),
        ("T6", "Install replacement coupling", 4, "mech", True, False, ["T5"]),
        ("T7", "Alignment and torque check", 3, "mech", True, False, ["T6"]),
        ("T8", "QA functional run test", 3, "qa", False, True, ["T3", "T4", "T7"]),
        ("T9", "Remove LOTO, hand line back to production", 1, "mech", False, True, ["T8"]),
    ]
    for code, name, dur, skill, part, reduced, deps in tasks:
        session.add(
            Task(
                shutdown_id="SD-FR-L1-0220",
                code=code,
                name=name,
                duration_h=dur,
                skill=skill,
                requires_part=part,
                in_reduced_scope=reduced,
                depends_on=deps,
            )
        )
    session.add_all(
        [
            StaffCalendar(site_id="FR-VAL", skill="mech", headcount=2, daily_start="00:00", daily_end="24:00"),
            StaffCalendar(site_id="FR-VAL", skill="elec", headcount=1, daily_start="00:00", daily_end="24:00"),
            StaffCalendar(site_id="FR-VAL", skill="qa", headcount=1, daily_start="07:00", daily_end="23:00"),
        ]
    )
    session.add_all(
        [
            Route(
                id="DE-FR-ROAD",
                name="Hessenfeld → Valmont, direct road",
                origin_site_id="DE-HSF",
                destination_site_id="FR-VAL",
                mode_label="road",
                carrier="Rheinfracht Demo Logistics (fictional)",
            ),
            Route(
                id="IE-FR-AIR",
                name="Shannamore → Shannon → Paris CDG → Valmont",
                origin_site_id="IE-SHM",
                destination_site_id="FR-VAL",
                mode_label="road–air–road",
                carrier="Atlantic Skybridge Cargo (fictional)",
            ),
            Route(
                id="IE-FR-SEA",
                name="Shannamore → Rosslare → Cherbourg → Valmont",
                origin_site_id="IE-SHM",
                destination_site_id="FR-VAL",
                mode_label="road–sea–road",
                carrier="Celtic Ro-Ro Demo Lines (fictional)",
            ),
        ]
    )
    session.flush()
    session.add_all(
        _with_tz(
            [
                RouteLeg(
                    route_id="DE-FR-ROAD",
                    seq=1,
                    mode="road",
                    origin="Hessenfeld plant",
                    destination="Valmont plant",
                    scheduled_departure=T(20, 9),
                    duration_h=10,
                    cost_eur=3100,
                ),
                RouteLeg(
                    route_id="IE-FR-AIR",
                    seq=1,
                    mode="road",
                    origin="Shannamore plant",
                    destination="Shannon air cargo terminal",
                    scheduled_departure=T(20, 7, 30),
                    duration_h=1.5,
                    cost_eur=600,
                ),
                RouteLeg(
                    route_id="IE-FR-AIR",
                    seq=2,
                    mode="air",
                    origin="Shannon (SNN)",
                    destination="Paris CDG cargo",
                    handoff_buffer_h=0.5,
                    cutoff_at=T(20, 10),
                    scheduled_departure=T(20, 11, 30),
                    duration_h=2,
                    cost_eur=7300,
                ),
                RouteLeg(
                    route_id="IE-FR-AIR",
                    seq=3,
                    mode="road",
                    origin="Paris CDG cargo",
                    destination="Valmont plant",
                    handoff_buffer_h=2,
                    duration_h=3,
                    cost_eur=1000,
                ),
                RouteLeg(
                    route_id="IE-FR-SEA",
                    seq=1,
                    mode="road",
                    origin="Shannamore plant",
                    destination="Rosslare port",
                    scheduled_departure=T(20, 8),
                    duration_h=3.5,
                    cost_eur=450,
                ),
                RouteLeg(
                    route_id="IE-FR-SEA",
                    seq=2,
                    mode="sea",
                    origin="Rosslare",
                    destination="Cherbourg",
                    handoff_buffer_h=1,
                    cutoff_at=T(20, 14),
                    scheduled_departure=T(20, 16),
                    duration_h=19,
                    cost_eur=1400,
                ),
                RouteLeg(
                    route_id="IE-FR-SEA",
                    seq=3,
                    mode="road",
                    origin="Cherbourg",
                    destination="Valmont plant",
                    handoff_buffer_h=1.5,
                    duration_h=6,
                    cost_eur=500,
                ),
            ]
        )
    )
    session.flush()


LOCATION_TZ = {
    "Hessenfeld plant": "Europe/Berlin",
    "Valmont plant": "Europe/Paris",
    "Shannamore plant": "Europe/Dublin",
    "Shannon air cargo terminal": "Europe/Dublin",
    "Shannon (SNN)": "Europe/Dublin",
    "Paris CDG cargo": "Europe/Paris",
    "Rosslare port": "Europe/Dublin",
    "Rosslare": "Europe/Dublin",
    "Cherbourg": "Europe/Paris",
}


def _with_tz(legs: list[RouteLeg]) -> list[RouteLeg]:
    for leg in legs:
        leg.origin_tz, leg.destination_tz = LOCATION_TZ[leg.origin], LOCATION_TZ[leg.destination]
    return legs


def _set_clock(session, value: datetime) -> None:
    session.get(ScenarioState, 1).now_utc = value
    session.flush()


def seed_scenario(session) -> None:
    """Static data, then the opening story through the real services."""
    seed_reference(session)
    _seed_static(session)
    planner = Ctx(session, load_actor(session, "planner_fr"))
    operator = Ctx(session, load_actor(session, "operator_fr"))
    for key in ("DE-FR-ROAD", "IE-FR-AIR"):
        events.ingest_event(
            planner,
            f"EVT-{key}-0001",
            "carrier (simulated)",
            f"route:{key}",
            1,
            "carrier.status",
            {"status": "Pickup slot confirmed"},
        )
    planning.replan(planner, "Baseline plan for the shutdown window")
    _set_clock(session, T(20, 1, 40))
    for name in OPENING_DOCUMENTS:
        documents.import_document(operator, name, (FIXTURES / name).read_bytes())
    _set_clock(session, DEMO_START)
    log(logger, logging.INFO, "scenario seeded", now=DEMO_START.isoformat(), status="ok")


def is_seeded(session) -> bool:
    return session.get(ScenarioState, 1) is not None


SCENARIO_TABLES = [
    QAEvidence,
    WorkRecord,
    Approval,
    Reservation,
    Plan,
    PlanningRun,
    ExtractedFact,
    FactConflict,
    Document,
    ExternalEvent,
    EntitySequence,
    InputChange,
    InputVersion,
    SiteJob,
    StockItem,
    RouteLeg,
    Route,
    Task,
    StaffCalendar,
    Shutdown,
    CompatibilityRule,
    ScenarioState,
]


def reset_scenario(ctx: Ctx) -> None:
    require(ctx, "scenario.control", site_id="FR-VAL", entity="scenario", entity_id="reset")
    old = ctx.session.get(ScenarioState, 1)
    version = (old.scenario_version + 1) if old else 1
    for model in SCENARIO_TABLES:
        ctx.session.execute(delete(model))
    ctx.session.flush()
    seed_scenario(ctx.session)
    ctx.session.get(ScenarioState, 1).scenario_version = version
    audit(
        ctx,
        "scenario.reset",
        "scenario",
        str(version),
        "applied",
        f"Scenario reset to {DEMO_START.isoformat()} (version {version}). Audit history is retained.",
    )


def advance_clock(ctx: Ctx, hours_: float) -> datetime:
    require(ctx, "scenario.control", site_id="FR-VAL", entity="scenario", entity_id="clock")
    if not 0 < hours_ <= 48:
        raise DomainError("Advance between 0 and 48 hours")
    state = ctx.session.get(ScenarioState, 1, with_for_update=True)
    state.now_utc = state.now_utc + timedelta(hours=hours_)
    audit(
        ctx,
        "scenario.advance",
        "scenario",
        "clock",
        "applied",
        f"Simulated clock advanced {hours_:g} h to {state.now_utc.isoformat()}",
    )
    return state.now_utc


def apply_disturbance(ctx: Ctx, code: str) -> tuple[str, str]:
    require(ctx, "scenario.control", site_id="FR-VAL", entity="scenario", entity_id=code)
    if code not in DISTURBANCES:
        raise DomainError(f"Unknown disturbance {code}")
    outcome, ev = events.ingest_event(ctx, **DISTURBANCES[code]["event"])
    return outcome, ev.note or ""


def sim_now(session) -> datetime:
    return now(session)
