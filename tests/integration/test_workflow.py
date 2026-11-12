"""The default demo workflow end to end through the services (the UI calls the same services)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.integration.helpers import complete_all_tasks, current, review_all_and_replan
from window36.db import session_scope
from window36.models import Approval, AuditEvent, Reservation, StockItem
from window36.services import approvals, planning, qa, scenario
from window36.services.common import DomainError, PolicyBlocked


def test_seeded_opening_state(db):
    plans = current(db)
    assert set(plans) == {
        "FULL-ORIGINAL",
        "FULL-DE-FR-ROAD",
        "FULL-IE-FR-AIR",
        "FULL-IE-FR-SEA",
        "REDUCED-SCOPE",
        "POSTPONE",
    }
    # Baseline plan was computed before the conflicting documents were reviewed.
    assert plans["FULL-ORIGINAL"].feasible
    assert plans["FULL-DE-FR-ROAD"].feasible and plans["FULL-IE-FR-AIR"].feasible
    sea = plans["FULL-IE-FR-SEA"]
    assert not sea.feasible
    assert any("after the shutdown window ends" in r for r in sea.reasons)
    # Each leg endpoint carries its own site time zone (Shannon is Irish time, CDG is French time).
    air_legs = plans["FULL-IE-FR-AIR"].route_detail["legs"]
    assert [(leg["from_tz"], leg["to_tz"]) for leg in air_legs] == [
        ("Europe/Dublin", "Europe/Dublin"),
        ("Europe/Dublin", "Europe/Paris"),
        ("Europe/Paris", "Europe/Paris"),
    ]


def test_review_quarantines_original_part(db, act):
    plans = review_all_and_replan(act, db)
    orig = plans["FULL-ORIGINAL"]
    assert not orig.feasible
    assert any("quarantined" in r for r in orig.reasons)
    with session_scope(db) as s:
        assert s.get(StockItem, "SP-FR-0001").status == "quarantine"


def test_default_workflow(db, act):
    plans = review_all_and_replan(act, db)
    de = plans["FULL-DE-FR-ROAD"]
    assert de.feasible and de.donor_impact["job_id"] == "GER-MJ-0226"

    # Approval without the donor-site release is blocked by policy.
    with pytest.raises(PolicyBlocked, match="POL-DONOR-002"):
        act("approver_fr", approvals.approve_plan, de.id, "Cheapest feasible option")

    act("approver_de", approvals.donor_release, de.id, "Press Line 2 job can slip ten days")
    act("approver_fr", approvals.approve_plan, de.id, "Cheapest feasible option")
    with session_scope(db) as s:
        res = s.scalar(
            select(Reservation).where(Reservation.stock_item_id == "SP-DE-0001", Reservation.status == "active")
        )
        assert res.holder_type == "plan" and res.plan_id == de.id

    # Inject the road pickup delay: the approval goes stale and the spare returns to the donor job.
    outcome, _ = act("planner_fr", scenario.apply_disturbance, "road_pickup_delay")
    assert outcome == "applied"
    with session_scope(db) as s:
        appr = s.scalar(select(Approval).where(Approval.plan_id == de.id, Approval.decision_type == "plan_approval"))
        assert appr.status == "stale"
        res = s.scalar(
            select(Reservation).where(Reservation.stock_item_id == "SP-DE-0001", Reservation.status == "active")
        )
        assert res.holder_type == "site_job"
    with pytest.raises(DomainError, match="stale"):
        act("approver_fr", approvals.approve_plan, de.id, "retry")

    act("planner_fr", planning.replan, "pickup delay")
    plans = current(db)
    assert not plans["FULL-DE-FR-ROAD"].feasible
    assert plans["FULL-DE-FR-ROAD"].revision == de.revision + 1
    assert plans["FULL-IE-FR-AIR"].feasible  # still before the air cutoff

    act("planner_fr", scenario.apply_disturbance, "missed_air_cutoff")
    act("planner_fr", planning.replan, "air cutoff missed")
    plans = current(db)
    air = plans["FULL-IE-FR-AIR"]
    assert not air.feasible and any("after cutoff" in r for r in air.reasons)
    assert not any(p.feasible for k, p in plans.items() if k.startswith("FULL-"))
    reduced = plans["REDUCED-SCOPE"]
    assert reduced.feasible

    act("approver_fr", approvals.approve_plan, reduced.id, "No full-scope option fits; defer coupling")
    with pytest.raises(PolicyBlocked, match="POL-QA-007"):
        act("approver_fr", qa.record_readiness, reduced.id, "Line ready")

    complete_all_tasks(act, reduced)
    with pytest.raises(PolicyBlocked, match="QA-RUN: evidence missing"):
        act("approver_fr", qa.record_readiness, reduced.id, "Line ready")
    act("operator_fr", qa.record_qa_evidence, reduced.id, "QA-RUN", "fail", "QA-RUN-0221-01")
    with pytest.raises(PolicyBlocked, match="FAILED"):
        act("approver_fr", qa.record_readiness, reduced.id, "Line ready")
    act("operator_fr", qa.record_qa_evidence, reduced.id, "QA-RUN", "pass", "QA-RUN-0221-02")
    act("approver_fr", qa.record_readiness, reduced.id, "Functional run passed")

    with session_scope(db) as s:
        kinds = {a.decision_type for a in s.scalars(select(Approval))}
        assert {"donor_release", "plan_approval", "readiness"} <= kinds
        actions = {a.action for a in s.scalars(select(AuditEvent))}
        assert {"donor.release", "plan.approve", "approval.stale", "readiness.record"} <= actions
        # Previous plans and decisions remain available for review.
        assert s.get(type(de), de.id).status == "stale"


def test_full_scope_readiness_requires_matching_part_revision(db, act):
    plans = review_all_and_replan(act, db)
    air = plans["FULL-IE-FR-AIR"]
    act("approver_ie", approvals.donor_release, air.id, "Spare not committed to a job")
    act("approver_fr", approvals.approve_plan, air.id, "Air transfer from Shannamore")
    complete_all_tasks(act, air)
    act("operator_fr", qa.record_qa_evidence, air.id, "QA-RCV", "pass", "RCV-01", "C", "SP-IE-0001")
    act("operator_fr", qa.record_qa_evidence, air.id, "QA-ALIGN", "pass", "ALN-01")
    act("operator_fr", qa.record_qa_evidence, air.id, "QA-RUN", "pass", "RUN-01", "D", "SP-IE-0001")
    with pytest.raises(PolicyBlocked, match="QA-RUN: evidence refers to rev D"):
        act("approver_fr", qa.record_readiness, air.id, "ready")
    act("operator_fr", qa.record_qa_evidence, air.id, "QA-RUN", "pass", "RUN-02", "C", "SP-IE-0001")
    act("approver_fr", qa.record_readiness, air.id, "All checks passed")
    with session_scope(db) as s:
        assert s.get(StockItem, "SP-IE-0001").status == "consumed"


def test_evidence_tied_to_older_plan_revision_is_stale(db, act):
    plans = review_all_and_replan(act, db)
    reduced = plans["REDUCED-SCOPE"]
    act("approver_fr", approvals.approve_plan, reduced.id, "reduced")
    act("operator_fr", qa.record_qa_evidence, reduced.id, "QA-RUN", "pass", "RUN-OLD")
    # A material change to the shutdown inputs invalidates the approval; replan gives r+1.
    from window36.services.common import bump_input

    act("planner_fr", bump_input, "tasks:SD-FR-L1-0220", "Task list changed by maintenance")
    act("planner_fr", planning.replan, "tasks changed")
    new = current(db)["REDUCED-SCOPE"]
    assert new.revision == reduced.revision + 1
    act("approver_fr", approvals.approve_plan, new.id, "re-approve")
    complete_all_tasks(act, new)
    with pytest.raises(PolicyBlocked, match="stale"):
        act("approver_fr", qa.record_readiness, new.id, "ready")


def test_no_feasible_plan_is_explicit(db, act):
    from datetime import timedelta

    from window36.models import Shutdown
    from window36.services import health

    with session_scope(db) as s:
        sd = s.get(Shutdown, "SD-FR-L1-0220")
        sd.window_end = sd.window_start + timedelta(hours=4)
        sd.next_window_start = None
    review_all_and_replan(act, db)
    plans = current(db)
    assert not any(p.feasible for p in plans.values())
    assert any("No later shutdown window" in r for r in plans["POSTPONE"].reasons)
    with session_scope(db) as s:
        texts = [t for _, t in health.alerts(s)]
    assert "No feasible plan at all — escalate" in texts


def test_original_part_verified_at_goods_in_becomes_cheapest(db, act):
    from window36.services import inventory

    review_all_and_replan(act, db)
    act("operator_fr", inventory.record_receiving_inspection, "SP-FR-0001", "C", "RCV-VAL-0220-02")
    act("planner_fr", planning.replan, "verified")
    orig = current(db)["FULL-ORIGINAL"]
    assert orig.feasible and orig.est_cost_eur == 0
    act("approver_fr", approvals.approve_plan, orig.id, "Original part verified rev C")


def test_wrong_revision_disturbance_rejects_original(db, act):
    review_all_and_replan(act, db)
    act("planner_fr", scenario.apply_disturbance, "wrong_revision")
    act("planner_fr", planning.replan, "rev B found")
    orig = current(db)["FULL-ORIGINAL"]
    assert not orig.feasible and any("rejected at receiving" in r for r in orig.reasons)


def test_reset_restores_opening_state(db, act):
    review_all_and_replan(act, db)
    act("planner_fr", scenario.advance_clock, 5)
    act("planner_fr", scenario.reset_scenario)
    with session_scope(db) as s:
        assert scenario.sim_now(s) == scenario.DEMO_START
    assert current(db)["FULL-ORIGINAL"].feasible
