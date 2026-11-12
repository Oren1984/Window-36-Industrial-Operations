"""Insufficient privileges and stale decisions are rejected in the service layer."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.integration.helpers import complete_all_tasks, current, review_all_and_replan, staged_docs
from window36.db import session_scope
from window36.models import AuditEvent
from window36.services import approvals, documents, events, inventory, planning, qa, scenario
from window36.services.common import DomainError, PermissionDenied, PolicyBlocked


def viewer_write_attempts(plan_id: int, doc_id: int):
    return [
        (planning.replan, ("x",)),
        (approvals.approve_plan, (plan_id, "x")),
        (approvals.reject_plan, (plan_id, "x")),
        (approvals.donor_release, (plan_id, "x")),
        (documents.import_document, ("a.txt", b"DOCUMENT_TYPE: supplier_notice")),
        (documents.review_document, (doc_id, True, "x")),
        (inventory.record_receiving_inspection, ("SP-FR-0001", "C", "x")),
        (qa.record_work, (plan_id, "T1")),
        (qa.record_qa_evidence, (plan_id, "QA-RUN", "pass", "x")),
        (qa.record_readiness, (plan_id, "x")),
        (scenario.advance_clock, (1,)),
        (scenario.apply_disturbance, ("road_pickup_delay",)),
        (scenario.reset_scenario, ()),
        (events.ingest_event, ("E1", "s", "route:DE-FR-ROAD", 9, "carrier.status", {})),
    ]


def test_viewer_cannot_write_anything(db, act):
    plan = current(db)["FULL-DE-FR-ROAD"]
    doc_id = staged_docs(db)[0]
    for fn, args in viewer_write_attempts(plan.id, doc_id):
        with pytest.raises(PermissionDenied):
            act("viewer", fn, *args)
    with session_scope(db) as s:
        blocked = s.scalars(
            select(AuditEvent).where(AuditEvent.actor == "viewer", AuditEvent.outcome == "blocked")
        ).all()
        assert len(blocked) >= 12


def test_site_scoping(db, act):
    plans = review_all_and_replan(act, db)
    de = plans["FULL-DE-FR-ROAD"]
    with pytest.raises(PermissionDenied, match="scoped to site DE-HSF"):
        act("approver_de", approvals.approve_plan, de.id, "not my site")
    with pytest.raises(PermissionDenied, match="scoped to site FR-VAL"):
        act("approver_fr", approvals.donor_release, de.id, "not my spare")
    with pytest.raises(PermissionDenied, match="scoped to site IE-SHM"):
        act("approver_ie", approvals.donor_release, de.id, "not my spare either")
    with pytest.raises(PermissionDenied):
        act("operator_de", inventory.record_receiving_inspection, "SP-FR-0001", "C", "x")
    with pytest.raises(PermissionDenied, match="lacks permission 'plan.approve'"):
        act("planner_fr", approvals.approve_plan, de.id, "planner cannot approve")


def test_approval_of_stale_plan_is_blocked(db, act):
    plans = review_all_and_replan(act, db)
    air = plans["FULL-IE-FR-AIR"]
    act("planner_fr", scenario.apply_disturbance, "missed_air_cutoff")  # material change, no replan yet
    with pytest.raises(DomainError, match="stale"):
        act("approver_ie", approvals.donor_release, air.id, "late")
    with pytest.raises(DomainError, match="stale"):
        act("approver_fr", approvals.approve_plan, air.id, "late")


def test_superseded_and_infeasible_plans_cannot_be_approved(db, act):
    old = current(db)["FULL-ORIGINAL"]
    review_all_and_replan(act, db)
    with pytest.raises(DomainError, match="stale|superseded"):
        act("approver_fr", approvals.approve_plan, old.id, "old revision")
    sea = current(db)["FULL-IE-FR-SEA"]
    with pytest.raises(DomainError, match="not feasible"):
        act("approver_fr", approvals.approve_plan, sea.id, "too late")


def test_readiness_blocked_by_failed_qa(db, act):
    plans = review_all_and_replan(act, db)
    reduced = plans["REDUCED-SCOPE"]
    act("approver_fr", approvals.approve_plan, reduced.id, "reduced")
    complete_all_tasks(act, reduced)
    act("operator_fr", qa.record_qa_evidence, reduced.id, "QA-RUN", "fail", "RUN-FAIL")
    with pytest.raises(PolicyBlocked) as exc:
        act("approver_fr", qa.record_readiness, reduced.id, "ready?")
    assert "POL-QA-007" in str(exc.value)
    with pytest.raises(PermissionDenied):
        act("operator_fr", qa.record_readiness, reduced.id, "operator cannot")


def test_task_dependencies_enforced_for_work_records(db, act):
    plans = review_all_and_replan(act, db)
    reduced = plans["REDUCED-SCOPE"]
    act("approver_fr", approvals.approve_plan, reduced.id, "reduced")
    with pytest.raises(DomainError, match="depends on unfinished"):
        act("operator_fr", qa.record_work, reduced.id, "T8")


def test_only_one_approved_plan(db, act):
    plans = review_all_and_replan(act, db)
    act("approver_fr", approvals.approve_plan, plans["REDUCED-SCOPE"].id, "reduced")
    with pytest.raises(DomainError, match="already approved"):
        act("approver_fr", approvals.approve_plan, plans["POSTPONE"].id, "also postpone")


def test_customs_policy_is_configurable(db, act, monkeypatch):
    plans = review_all_and_replan(act, db)
    act("approver_ie", approvals.donor_release, plans["FULL-IE-FR-AIR"].id, "ok")
    monkeypatch.setenv("W36_POLICY_CUSTOMS_ENABLED", "true")
    with pytest.raises(PolicyBlocked, match="POL-CUSTOMS-005"):
        act("approver_fr", approvals.approve_plan, plans["FULL-IE-FR-AIR"].id, "air")
    monkeypatch.setenv("W36_POLICY_CUSTOMS_ENABLED", "false")
    act("approver_fr", approvals.approve_plan, plans["FULL-IE-FR-AIR"].id, "air")


def test_spend_authority_policy(db, act, monkeypatch):
    plans = review_all_and_replan(act, db)
    act("approver_ie", approvals.donor_release, plans["FULL-IE-FR-AIR"].id, "ok")
    monkeypatch.setenv("W36_APPROVER_SPEND_LIMIT_EUR", "5000")
    with pytest.raises(PolicyBlocked, match="POL-PROC-006"):
        act("approver_fr", approvals.approve_plan, plans["FULL-IE-FR-AIR"].id, "air")
