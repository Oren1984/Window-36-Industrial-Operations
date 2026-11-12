"""Relational model. PostgreSQL is the system of record; all timestamps are timestamptz (UTC)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "site"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(60))
    timezone: Mapped[str] = mapped_column(String(64))
    site_role: Mapped[str] = mapped_column(String(16))  # main | secondary
    receiving_open: Mapped[str] = mapped_column(String(5))  # local HH:MM
    receiving_close: Mapped[str] = mapped_column(String(5))


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # planner | site_operator | approver | viewer
    site_id: Mapped[str | None] = mapped_column(ForeignKey("site.id"), nullable=True)


class ScenarioState(Base):
    __tablename__ = "scenario_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_name: Mapped[str] = mapped_column(String(120))
    now_utc: Mapped[datetime] = mapped_column(TS)
    scenario_version: Mapped[int] = mapped_column(Integer, default=1)
    seeded_at: Mapped[datetime] = mapped_column(TS)


class InputVersion(Base):
    """Version counter per planning input; approvals bind to these versions."""

    __tablename__ = "input_version"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class InputChange(Base):
    __tablename__ = "input_change"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(60))
    sim_time: Mapped[datetime] = mapped_column(TS)


class CompatibilityRule(Base):
    __tablename__ = "compatibility_rule"
    __table_args__ = (UniqueConstraint("part_number", "line_code", "revision"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_number: Mapped[str] = mapped_column(String(40))
    line_code: Mapped[str] = mapped_column(String(40))
    revision: Mapped[str] = mapped_column(String(4))
    compatible: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str] = mapped_column(Text)


class StockItem(Base):
    __tablename__ = "stock_item"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    part_number: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(120))
    revision: Mapped[str] = mapped_column(String(4))
    # verified | expected | disputed | rejected
    revision_status: Mapped[str] = mapped_column(String(16))
    serial: Mapped[str | None] = mapped_column(String(40), nullable=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("site.id"))
    # available | inbound | quarantine | rejected | consumed
    status: Mapped[str] = mapped_column(String(16))
    delivery_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expected_arrival: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    unit_value_eur: Mapped[float] = mapped_column(Float, default=0)


class SiteJob(Base):
    """Maintenance job at a secondary site that currently holds a spare."""

    __tablename__ = "site_job"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("site.id"))
    title: Mapped[str] = mapped_column(String(160))
    planned_start: Mapped[datetime] = mapped_column(TS)
    stock_item_id: Mapped[str] = mapped_column(ForeignKey("stock_item.id"))
    replacement_lead_days: Mapped[int] = mapped_column(Integer)
    impact_cost_eur: Mapped[float] = mapped_column(Float)


class Reservation(Base):
    __tablename__ = "reservation"
    __table_args__ = (
        # One active claim per spare, enforced by the database.
        Index(
            "uq_reservation_one_active_claim",
            "stock_item_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_item_id: Mapped[str] = mapped_column(ForeignKey("stock_item.id"))
    holder_type: Mapped[str] = mapped_column(String(16))  # site_job | plan
    holder_ref: Mapped[str] = mapped_column(String(60))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plan.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # active | released | consumed
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(TS)
    released_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class Shutdown(Base):
    __tablename__ = "shutdown"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("site.id"))
    line_code: Mapped[str] = mapped_column(String(40))
    line_name: Mapped[str] = mapped_column(String(120))
    part_number: Mapped[str] = mapped_column(String(40))
    window_start: Mapped[datetime] = mapped_column(TS)
    window_end: Mapped[datetime] = mapped_column(TS)
    next_window_start: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    postponement_cost_eur: Mapped[float] = mapped_column(Float)
    reduced_scope_risk_cost_eur: Mapped[float] = mapped_column(Float)


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (UniqueConstraint("shutdown_id", "code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shutdown_id: Mapped[str] = mapped_column(ForeignKey("shutdown.id"))
    code: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(160))
    duration_h: Mapped[float] = mapped_column(Float)
    skill: Mapped[str] = mapped_column(String(20))
    requires_part: Mapped[bool] = mapped_column(Boolean)
    in_reduced_scope: Mapped[bool] = mapped_column(Boolean)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)


class StaffCalendar(Base):
    __tablename__ = "staff_calendar"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("site.id"))
    skill: Mapped[str] = mapped_column(String(20))
    headcount: Mapped[int] = mapped_column(Integer)
    daily_start: Mapped[str] = mapped_column(String(5))  # local HH:MM
    daily_end: Mapped[str] = mapped_column(String(5))  # local HH:MM, "24:00" allowed


class Route(Base):
    __tablename__ = "route"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    origin_site_id: Mapped[str] = mapped_column(ForeignKey("site.id"))
    destination_site_id: Mapped[str] = mapped_column(ForeignKey("site.id"))
    mode_label: Mapped[str] = mapped_column(String(32))
    carrier: Mapped[str] = mapped_column(String(80))
    carrier_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    customs_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)


class RouteLeg(Base):
    __tablename__ = "route_leg"
    __table_args__ = (UniqueConstraint("route_id", "seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("route.id"))
    seq: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(8))  # road | air | sea
    origin: Mapped[str] = mapped_column(String(80))
    destination: Mapped[str] = mapped_column(String(80))
    origin_tz: Mapped[str] = mapped_column(String(64))  # time zone of the leg's start location
    destination_tz: Mapped[str] = mapped_column(String(64))
    handoff_buffer_h: Mapped[float] = mapped_column(Float, default=0)
    cutoff_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    scheduled_departure: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    duration_h: Mapped[float] = mapped_column(Float)
    delay_h: Mapped[float] = mapped_column(Float, default=0)
    cost_eur: Mapped[float] = mapped_column(Float)


class Document(Base):
    __tablename__ = "document"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # supplier_notice | shipping_document | unknown
    filename: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    delivery_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # staged | accepted | rejected | failed
    extractor: Mapped[str] = mapped_column(String(60))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(60))
    uploaded_at: Mapped[datetime] = mapped_column(TS)
    reviewed_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExtractedFact(Base):
    __tablename__ = "extracted_fact"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    field: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(String(200))
    provenance: Mapped[str] = mapped_column(String(80))
    valid: Mapped[bool] = mapped_column(Boolean)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FactConflict(Base):
    __tablename__ = "fact_conflict"
    __table_args__ = (UniqueConstraint("delivery_ref", "field"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_ref: Mapped[str] = mapped_column(String(40))
    field: Mapped[str] = mapped_column(String(40))
    values: Mapped[dict] = mapped_column(JSON)  # {"doc:<id>": value}
    status: Mapped[str] = mapped_column(String(16))  # open | resolved
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(TS)


class PlanningRun(Base):
    __tablename__ = "planning_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shutdown_id: Mapped[str] = mapped_column(ForeignKey("shutdown.id"))
    sim_time: Mapped[datetime] = mapped_column(TS)
    actor: Mapped[str] = mapped_column(String(60))
    trigger: Mapped[str] = mapped_column(String(200))
    last_input_change_id: Mapped[int] = mapped_column(Integer, default=0)  # watermark for "changes since"


class Plan(Base):
    """An immutable plan revision. Only `status` changes after creation."""

    __tablename__ = "plan"
    __table_args__ = (UniqueConstraint("plan_key", "revision"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shutdown_id: Mapped[str] = mapped_column(ForeignKey("shutdown.id"))
    planning_run_id: Mapped[int] = mapped_column(ForeignKey("planning_run.id"))
    plan_key: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(16))  # full | reduced | postpone
    source_stock_item_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source_site_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    route_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    part_revision: Mapped[str | None] = mapped_column(String(4), nullable=True)
    feasible: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list] = mapped_column(JSON, default=list)  # blockers
    notes: Mapped[list] = mapped_column(JSON, default=list)  # non-blocking observations
    expected_arrival: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    part_available_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    expected_finish: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    window_start: Mapped[datetime] = mapped_column(TS)
    window_end: Mapped[datetime] = mapped_column(TS)
    slack_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    est_cost_eur: Mapped[float] = mapped_column(Float)
    cost_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    donor_impact: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    route_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    schedule: Mapped[list] = mapped_column(JSON, default=list)
    critical_path: Mapped[list] = mapped_column(JSON, default=list)
    input_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)
    # proposed | approved | rejected | stale | superseded
    status: Mapped[str] = mapped_column(String(16))
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Approval(Base):
    __tablename__ = "approval"
    __table_args__ = (
        Index(
            "uq_approval_one_active_per_type",
            "plan_id",
            "decision_type",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plan.id"))
    # donor_release | plan_approval | plan_rejection | readiness
    decision_type: Mapped[str] = mapped_column(String(20))
    site_id: Mapped[str] = mapped_column(ForeignKey("site.id"))
    actor: Mapped[str] = mapped_column(String(60))
    actor_role: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    policy_results: Mapped[list] = mapped_column(JSON, default=list)
    input_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16))  # active | stale | superseded
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    sim_time: Mapped[datetime] = mapped_column(TS)


class ExternalEvent(Base):
    __tablename__ = "external_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    source: Mapped[str] = mapped_column(String(60))
    entity_key: Mapped[str] = mapped_column(String(80))
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(16))  # applied | out_of_order | rejected
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    received_sim_time: Mapped[datetime] = mapped_column(TS)


class EntitySequence(Base):
    __tablename__ = "entity_sequence"
    entity_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(Integer)


class WorkRecord(Base):
    __tablename__ = "work_record"
    __table_args__ = (UniqueConstraint("plan_id", "task_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plan.id"))
    task_code: Mapped[str] = mapped_column(String(12))
    actor: Mapped[str] = mapped_column(String(60))
    sim_time: Mapped[datetime] = mapped_column(TS)


class QAEvidence(Base):
    __tablename__ = "qa_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plan.id"))
    check_code: Mapped[str] = mapped_column(String(20))
    result: Mapped[str] = mapped_column(String(8))  # pass | fail
    part_revision: Mapped[str | None] = mapped_column(String(4), nullable=True)
    stock_item_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    evidence_ref: Mapped[str] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(60))
    sim_time: Mapped[datetime] = mapped_column(TS)


class AuditEvent(Base):
    """Business audit trail (distinct from diagnostic logs)."""

    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sim_time: Mapped[datetime] = mapped_column(TS)
    recorded_at: Mapped[datetime] = mapped_column(TS)
    actor: Mapped[str] = mapped_column(String(60))
    actor_role: Mapped[str] = mapped_column(String(20))
    site_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(16))  # permitted | blocked | applied | info
    reason: Mapped[str] = mapped_column(Text)
    policy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(12), nullable=True)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    correlation_id: Mapped[str] = mapped_column(String(40))
