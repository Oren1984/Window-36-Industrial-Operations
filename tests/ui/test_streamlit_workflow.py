"""Drive the default demo workflow through the real Streamlit pages (headless AppTest)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from tests.integration.helpers import current
from window36.db import session_scope
from window36.models import Approval, AuditEvent, StockItem

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

VIEWS = Path(__file__).resolve().parents[2] / "app" / "views"


def page(name: str, user: str | None) -> AppTest:
    at = AppTest.from_file(str(VIEWS / f"{name}.py"), default_timeout=60)
    if user:
        at.session_state["w36_user"] = user
    at.run()
    assert not at.exception, at.exception
    return at


def click(at: AppTest, key: str) -> AppTest:
    at.button(key=key).click().run()
    assert not at.exception, at.exception
    return at


def errors(at: AppTest) -> list[str]:
    return [e.value for e in at.error]


def pick_plan(at: AppTest, plan_key: str) -> AppTest:
    box = at.selectbox(key="plan_pick")
    label = next(o for o in box.options if o.startswith(plan_key + " "))
    box.select(label).run()
    return at


def test_login_form_and_every_page_renders(db):
    at = page("help_center", None)
    at.text_input(key="login_user").input("viewer")
    at.text_input(key="login_pass").input("test-demo-password")
    at.button[0].click().run()
    assert at.session_state["w36_user"] == "viewer"
    for name in ("control_room", "procurement_logistics", "plans_qa", "operations_audit", "help_center"):
        at = page(name, "viewer")
        assert any("Developed and built by Oren Salami V1.0" in c.value for c in at.caption)


def test_wrong_password_is_rejected(db):
    at = page("help_center", None)
    at.text_input(key="login_user").input("approver_fr")
    at.text_input(key="login_pass").input("nope")
    at.button[0].click().run()
    assert "w36_user" not in at.session_state
    assert "Invalid username or password" in errors(at)


def test_viewer_controls_are_disabled(db):
    at = page("plans_qa", "viewer")
    assert at.button(key="approve").disabled and at.button(key="reject").disabled
    at = page("operations_audit", "viewer")
    assert at.button(key="advance").disabled and at.button(key="dist_road_pickup_delay").disabled


def test_default_workflow_through_the_ui(db):
    # 1. Planner reviews the supplier discrepancy.
    at = page("procurement_logistics", "planner_fr")
    assert any("Conflict on **REVISION**" in e for e in errors(at))
    for doc_id in (1, 2):
        at.text_input(key=f"note_{doc_id}").input("Compared with goods-in paperwork")
        click(at, f"accept_{doc_id}")
    with session_scope(db) as s:
        assert s.get(StockItem, "SP-FR-0001").status == "quarantine"

    # 2. Replan from the control room; compare routes.
    at = page("control_room", "planner_fr")
    assert any("stock:SP-FR-0001" in w.value for w in at.warning)
    click(at, "replan")
    at = page("procurement_logistics", "planner_fr")
    assert any("after the shutdown window ends" in e for e in errors(at))  # sea misses the deadline

    # 3. Donor-site release by Hessenfeld, approval by Valmont.
    at = pick_plan(page("plans_qa", "approver_de"), "FULL-DE-FR-ROAD")
    at.text_input(key="decision_reason").input("Press Line 2 job can slip")
    click(at, "donor_release")
    at = pick_plan(page("plans_qa", "approver_fr"), "FULL-DE-FR-ROAD")
    at.text_input(key="decision_reason").input("Cheapest feasible transfer")
    click(at, "approve")
    assert current(db)["FULL-DE-FR-ROAD"].status == "approved"

    # 4. Inject the road pickup delay: approval becomes stale.
    at = page("operations_audit", "planner_fr")
    click(at, "dist_road_pickup_delay")
    assert current(db)["FULL-DE-FR-ROAD"].status == "stale"
    at = page("control_room", "planner_fr")
    assert any("stale" in m.value.lower() for m in at.markdown)
    at = page("operations_audit", "planner_fr")
    click(at, "dist_missed_air_cutoff")
    at = page("control_room", "planner_fr")
    click(at, "replan")
    plans = current(db)
    assert not any(p.feasible for k, p in plans.items() if k.startswith("FULL-"))

    # 5. Approve the revised reduced-scope plan.
    at = pick_plan(page("plans_qa", "approver_fr"), "REDUCED-SCOPE")
    at.text_input(key="decision_reason").input("No full-scope option fits the window")
    click(at, "approve")
    reduced = current(db)["REDUCED-SCOPE"]
    assert reduced.status == "approved"

    # 6. Readiness stays blocked until the work and QA evidence are complete and passing.
    at = page("plans_qa", "approver_fr")
    at.text_input(key="ready_note").input("Line ready")
    click(at, "readiness")
    assert any("blocked" in e and "POL-QA-007" in e for e in errors(at))

    at = page("plans_qa", "operator_fr")
    for task in sorted(reduced.schedule, key=lambda t: t["start"]):
        click(at, f"done_{task['code']}")
    form_inputs = [ti for ti in at.text_input if ti.label == "Evidence reference"]
    form_inputs[0].input("QA-RUN-0221-01")
    at.button[[b.label for b in at.button].index("Record evidence")].click().run()
    assert not at.exception

    at = page("plans_qa", "approver_fr")
    at.text_input(key="ready_note").input("Functional run passed")
    click(at, "readiness")
    with session_scope(db) as s:
        assert approvals_of(s, reduced.id, "readiness")
        blocked = s.scalars(
            select(AuditEvent).where(AuditEvent.action == "readiness.record", AuditEvent.outcome == "blocked")
        ).all()
        assert blocked


def approvals_of(s, plan_id, kind):
    return s.scalars(
        select(Approval).where(Approval.plan_id == plan_id, Approval.decision_type == kind, Approval.status == "active")
    ).all()
